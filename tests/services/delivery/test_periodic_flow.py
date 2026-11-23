"""Periodic assessments use fixed Git evidence, not the latest moving target."""

from contextlib import nullcontext
from threading import Event
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch

from homebrew_harness.runtime.errors import boundary_error
from homebrew_harness.services.delivery import periodic
from homebrew_harness.services.delivery.spec import validate_periodic
from tests.runtime.test_interaction import eventually
from tests.services.delivery import test_merge_flow, test_target_refresh


class PeriodicSettingsTests(unittest.TestCase):
    def test_defaults_and_invalid_schedules(self):
        self.assertEqual(validate_periodic({}), {
            "assessment_seconds": 21600, "min_integration_seconds": 21600,
            "max_deferral_seconds": 86400,
        })
        for raw in ({"max_deferral_seconds": 1}, {"assessment_seconds": True},
                    {"assessment_seconds": 0}, {"typo": 30}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                validate_periodic(raw)

    def test_early_assessment_does_not_bypass_cooldown_or_prior_deferral(self):
        signals = {"ci_ready": True, "review_ready": True,
                   "feedback_pending": False, "ci_baseline_block": False}
        observed = {"mergeable_state": "dirty", "draft": False}
        policy = validate_periodic({"min_integration_seconds": 30})
        schedule = {"last_integration_at": 100, "next_assessment_at": 150}
        self.assertFalse(periodic._due(schedule, signals, observed, policy, 129))
        self.assertTrue(periodic._due(schedule, signals, observed, policy, 130))
        schedule["last_decision"] = {"action": "defer"}
        self.assertFalse(periodic._due(schedule, signals, observed, policy, 130))
        self.assertTrue(periodic._due(schedule, signals, observed, policy, 150))

    def test_baseline_ci_has_its_own_risk_signal_on_a_git_mergeable_pr(self):
        head, base, target = "a" * 40, "b" * 40, "c" * 40
        spec = {"base_update": "periodic", "check_app_ids": [1], "required_checks": ["CI"],
                "reviewer_ids": [], "required_approvals": 0}
        ci = {"head": head, "base": target, "state": "open", "merged": False,
              "draft": False, "source_branch": "harness/example", "base_branch": "main",
              "source_project": "example/project", "base_project": "example/project",
              "mergeable": True, "mergeable_state": "blocked", "reviews": [], "checks": [
                  {"id": 8, "head": head, "name": "CI", "app_id": 1, "status": "completed", "conclusion": "failure"},
              ]}
        row = {"candidate": {"commit": head, "target_base": base}, "change_request": {"id": "1"},
               "review_commit": head, "ci": ci, "observed": ci}
        self.assertFalse(periodic.has_risk(spec, row))
        self.assertTrue(periodic.ci_integration_eligible(spec, row))
        row["integration_ci_basis"] = periodic.ci_failure_basis(spec, ci)
        self.assertTrue(periodic.has_risk(spec, row))
        self.assertFalse(periodic.has_risk(spec, {**row, "ci": {**ci, "checks": []}}))
        self.assertFalse(periodic.ci_integration_eligible(spec, {**row, "review_commit": "d" * 40}))

    def test_ci_waiting_cannot_disable_the_assessment_timer(self):
        self.assertTrue(periodic.waiting_for_delivery({"state": "diagnosis_waiting"}))
        self.assertTrue(periodic.waiting_for_delivery({"state": "blocked", "error": "ci_wait_expired"}))
        self.assertFalse(periodic.waiting_for_delivery({"state": "blocked", "error": "model_stop"}))
        self.assertFalse(periodic.waiting_for_delivery({"state": "repair_waiting"}))

    def test_rewritten_target_during_probe_requests_fresh_observation(self):
        schedule = {"state": "waiting", "risk_since": 10, "deferral_deadline_at": 150}
        row = {"integration_schedule": schedule, "next_merge": 200}
        store = SimpleNamespace(guard=lambda *_: None)
        delivery = SimpleNamespace(stop=Event(), spec={"ci_poll_seconds": 5},
                                   runtime=SimpleNamespace(store=store, header={}),
                                   ms=SimpleNamespace(db=SimpleNamespace(write=nullcontext)))
        issue = boundary_error(ValueError, "integration_target_rewritten", "Target no longer contains the old commit.")
        with (patch.object(periodic, "_read", return_value=(row, None)),
              patch.object(periodic, "_save") as save, patch.object(periodic, "now", return_value=100)):
            periodic._retry(delivery, "changeset", schedule, issue)
        updated = save.call_args.args[-1]
        self.assertEqual(row["next_merge"], 0)
        self.assertEqual(updated["next_assessment_at"], 100)
        self.assertEqual(updated["deferral_deadline_at"], 150)
        self.assertEqual(updated["risk_since"], 10)
        self.assertIsNone(updated["selection"])


class PeriodicDeliveryTests(unittest.TestCase):
    setUp = test_merge_flow.MergeFlowTests.setUp
    start = test_merge_flow.MergeFlowTests.start
    run_git = test_merge_flow.MergeFlowTests.run_git
    ready = test_merge_flow.MergeFlowTests.ready
    manual = test_merge_flow.MergeFlowTests.manual
    advance_target = test_target_refresh.TargetRefreshTests.advance_target
    row = test_target_refresh.TargetRefreshTests.row
    conflict_request = test_target_refresh.TargetRefreshTests.conflict_request
    pending_target = test_merge_flow.MergeFlowTests.pending_target

    def test_unknown_mergeability_gets_fresh_authority_when_readiness_recovers(self):
        service = self.start(base_update="periodic", merge={"mode": "service_approved", "poll_seconds": .1})
        work = service.work.submit("Implement the bounded value.")

        def observed_target():
            row = self.row(service, work)
            # Readiness can become unknown without any target drift.
            return row if row and row["state"] == "merge_ready" and row["ci"]["base"] == row["candidate"]["target_base"] else None

        before = eventually(observed_target, 25)
        actions = service.runtime.maintenance.delivery.store
        first = next(a for a in service.runtime.maintenance.delivery.rows("actions")
                     if a["kind"] == "merge_change_request" and a["plan"]["target_base"] == before["candidate"]["target_base"]
                     and a["plan"]["readiness_revision"] == before["merge_readiness_revision"])
        self.peer.pulls[0].update(mergeable=None, mergeable_state="unknown")
        eventually(lambda: actions.get("actions", first["id"])["state"] == "superseded", 25)
        self.peer.pulls[0].update(mergeable=True, mergeable_state="clean")
        after = eventually(lambda: row if (row := observed_target()) and
                           row["merge_readiness_revision"] > before["merge_readiness_revision"] else None, 25)
        fresh = next(a for a in service.runtime.maintenance.delivery.rows("actions")
                     if a["kind"] == "merge_change_request" and
                     a["plan"]["readiness_revision"] == after["merge_readiness_revision"])
        self.assertNotEqual(fresh["id"], first["id"])
        self.assertEqual(fresh["state"], "awaiting_approval")
        self.assertEqual(fresh["plan"]["gate_hash"], first["plan"]["gate_hash"])
        self.assertEqual(fresh["plan"]["commit"], before["candidate"]["commit"])
        self.assertFalse(any(kind == "merge" for kind, _ in self.peer.writes))

    def test_periodic_deferral_survives_target_churn_and_integration_pins_assessed_base(self):
        clock = [time.time()]
        origin = clock[0]
        observed_inputs = []
        assessing, choose = Event(), Event()
        self.addCleanup(choose.set)

        def assess(evidence):
            observed_inputs.append(evidence)
            if not evidence["overdue"]:
                return {"action": "defer", "reason": "Collect remaining feedback first.", "defer_seconds": 999}
            assessing.set()
            if not choose.wait(10):
                raise RuntimeError("The integration assessment was not released.")
            return {"action": "integrate", "reason": "Resolve the documented conflict on the assessed target."}

        self.peer.integration_assessment = assess
        self.peer.ci_head = "a" * 40  # No readiness fast path until the new head is published.
        with patch.object(periodic, "now", side_effect=lambda: clock[0]):
            service = self.start(base_update="periodic", periodic_integration={
                "assessment_seconds": 10, "min_integration_seconds": 10, "max_deferral_seconds": 30,
            }, merge={"mode": "manual", "poll_seconds": 0.1})
            work = service.work.submit("Implement the bounded value.")
            original = eventually(lambda: self.row(service, work) if self.peer.pulls and
                                  (self.row(service, work) or {}).get("change_request") else None, 120)
            first = self.advance_target("src/value.txt", "3\n")
            self.peer.pulls[0].update(mergeable=False, mergeable_state="dirty")

            def schedule(target=None, state=None):
                value = (self.row(service, work) or {}).get("integration_schedule") or {}
                return value if (value and (target is None or value["observed_target"] == target)
                                 and (state is None or value["state"] == state)) else None

            first_risk = eventually(lambda: schedule(first, "waiting"), 20)
            for number in range(3):
                clock[0] = origin + number + 1
                target = self.advance_target("movement.txt", f"advance {number}\n")
                current = eventually(lambda: schedule(target, "waiting"), 10)
                self.assertEqual(current["risk_since"], first_risk["risk_since"])
                self.assertEqual(current["deferral_deadline_at"], origin + 30)
                self.assertEqual(self.row(service, work)["candidate"], original["candidate"])
            self.assertEqual(observed_inputs, [])

            clock[0] = origin + 10
            deferred = eventually(lambda: (value if (value := schedule(state="waiting"))
                                            and value.get("last_decision", {}).get("action") == "defer" else None), 25)
            self.assertEqual(deferred["next_assessment_at"], origin + 30)
            self.assertEqual(observed_inputs[0]["conflicts"], ["src/value.txt"])
            self.assertEqual(observed_inputs[0]["source_head"], original["candidate"]["commit"])
            self.assertIsNone(self.conflict_request(service))
            service.stop()
            self.assertTrue(service.wait_closed(20))
            service = self.host.services.start(identity=service.id)
            self.addCleanup(lambda: (service.close(), service.wait_closed(20)))
            restored = eventually(lambda: schedule(state="waiting"), 10)
            self.assertEqual(restored["risk_since"], deferred["risk_since"])
            self.assertEqual(restored["next_assessment_at"], deferred["next_assessment_at"])
            self.assertEqual(len(observed_inputs), 1)

            clock[0] = origin + 29
            assessed_target = self.advance_target("movement.txt", "before deadline\n")
            try:
                eventually(lambda: schedule(assessed_target, "waiting"), 30)
            except AssertionError:
                row = self.row(service, work)
                meta = service.store.get(service.id)
                self.fail(f"Restored service did not observe the latest target: state={meta['state']}, "
                          f"faults={meta.get('runtime_failures')}, observed={row.get('observed')}, "
                          f"schedule={row.get('integration_schedule')}, reads={row.get('read_errors')}")
            self.assertEqual(len(observed_inputs), 1)
            clock[0] = origin + 30
            try:
                self.assertTrue(assessing.wait(25))
                self.assertEqual(observed_inputs[-1]["target_commit"], assessed_target)
                latest = self.advance_target("movement.txt", "while assessment in flight\n")
                eventually(lambda: schedule(latest, "assessing"), 10)
            finally:
                choose.set()
            request = eventually(lambda: self.conflict_request(service), 40)
            prepared = self.row(service, work)
            self.assertEqual(prepared["candidate"]["target_base"], assessed_target)
            self.assertEqual(prepared["ci"]["conflicts"], ["src/value.txt"])
            self.assertEqual(prepared["candidate_history"][-1]["frozen"], original["frozen"])
            self.assertEqual(prepared["integration_schedule"]["last_integration_at"], origin + 30)
            self.assertEqual(prepared["integration_schedule"]["last_decision"]["target"], assessed_target)
            self.assertEqual(len(observed_inputs), 2)
            self.assertEqual(self.run_git("--git-dir=" + str(self.remote), "rev-parse", "main").decode().strip(), latest)
            self.peer.pulls[0].update(mergeable=True, mergeable_state="clean")
            self.peer.ci_head = None
            service.decide(request, "approve", decision_id="periodic-scoped-repair")
            try:
                current = eventually(lambda: self.ready(service, work), 120)
            except AssertionError:
                row = self.row(service, work)
                saved = service.store.work(work.id)
                self.fail(f"Periodic repair did not return to merge_ready: "
                          f"work={saved['state']}, delivery={row['state']}, "
                          f"error={row.get('error')}, detail={row.get('error_detail')}, "
                          f"result={saved.get('result')}, reads={row.get('read_errors')}, "
                          f"schedule={row.get('integration_schedule')}")
            self.assertEqual(current["candidate"]["target_base"], assessed_target)
            self.assertEqual(current["change_request"], original["change_request"])
            self.assertEqual(current["branch"], original["branch"])
            self.assertEqual(len(self.peer.pulls), 1)
            self.assertEqual(work.snapshot().state, "review")
            self.peer.merge_pull(1)
            eventually(lambda: work.snapshot().state == "delivered", 80)

    def test_clean_base_drift_keeps_original_candidate_and_needs_no_model(self):
        service, work, prior = self.manual(base_update="periodic")
        target = self.advance_target("unrelated.txt", "main moved independently\n")
        current = eventually(lambda: (row if (row := self.row(service, work))
                                      and (row.get("observed") or {}).get("base") == target else None), 20)
        self.assertEqual(current["state"], "merge_ready")
        self.assertEqual(current["candidate"], prior["candidate"])
        self.assertEqual(current["frozen"], prior["frozen"])
        self.assertFalse(current.get("integration_schedule"))
        self.assertFalse(any(job["kind"] == "integration_assessment" for job in
                             service.store.runtime.items("service_jobs_model").values()))
