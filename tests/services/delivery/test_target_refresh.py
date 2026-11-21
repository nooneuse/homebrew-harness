"""Current target integration uses existing diagnosis and same-PR delivery."""

import copy
import unittest
from unittest.mock import patch
from homebrew_harness.contracts import SDKError

from tests.runtime.test_interaction import eventually
from tests.services.delivery import test_merge_flow as fixture


class TargetRefreshTests(unittest.TestCase):
    setUp = fixture.MergeFlowTests.setUp
    start = fixture.MergeFlowTests.start
    run_git = fixture.MergeFlowTests.run_git
    ready = fixture.MergeFlowTests.ready
    manual = fixture.MergeFlowTests.manual

    def advance_target(self, name, content):
        (self.workspace / name).write_text(content)
        self.run_git("add", name, cwd=self.workspace)
        self.run_git("commit", "-qm", "independent target change", cwd=self.workspace)
        later = self.run_git("rev-parse", "HEAD", cwd=self.workspace).decode().strip()
        repo = "--git-dir=" + str(self.remote)
        self.run_git(repo, "fetch", str(self.workspace), "HEAD")
        self.run_git(repo, "update-ref", "refs/heads/main", later)
        return later

    def row(self, service, work):
        identity = service.store.work(work.id).get("changeset_id")
        return service.runtime.maintenance.delivery.store.get("changesets", identity) if identity else None

    def conflict_request(self, service):
        return next((r for r in service.requests() if r.kind == "ci_repair"), None)

    def test_conflicted_target_uses_scoped_repair_and_updates_the_same_pr(self):
        service, work, original = self.manual()
        delivery = service.runtime.maintenance.delivery
        old = copy.deepcopy(original)
        old_actions = {a["id"]: copy.deepcopy(a) for a in delivery.rows("actions")}
        later = self.advance_target("src/value.txt", "3\n")
        self.peer.pulls[0].update(mergeable=False, mergeable_state="dirty")
        request = eventually(lambda: self.conflict_request(service), 40)
        prepared = self.row(service, work)
        self.assertEqual(prepared["ci"]["conflicts"], ["src/value.txt"])
        self.assertEqual(prepared["candidate"]["target_base"], later)
        self.assertEqual(prepared["state"], "repair_waiting")
        self.assertIsNone(prepared["quality"])
        self.assertIsNone(prepared["review"])
        with self.assertRaisesRegex(ValueError, "not quality-ready"):
            service.plan_delivery(prepared["id"], "publish_branch")
        history = prepared["candidate_history"][-1]
        self.assertEqual(history["commit"], old["candidate"]["commit"])
        self.assertEqual(history["quality"], old["quality"])
        self.assertEqual(history["review"], old["review"])
        self.assertEqual(history["frozen"], old["frozen"])
        self.assertEqual(delivery.git.ref(prepared["branch"]), old["candidate"]["commit"])
        self.assertEqual(work.snapshot().state, "review")
        self.peer.pulls[0].update(mergeable=True, mergeable_state="clean")
        service.decide(request, "approve", decision_id="accept-scoped-integration-repair")
        repaired = eventually(lambda: self.ready(service, work), 55)
        self.assertEqual(repaired["candidate"]["target_base"], later)
        self.assertEqual(repaired["branch"], old["branch"])
        self.assertEqual(repaired["change_request"], old["change_request"])
        self.assertEqual(len(self.peer.pulls), 1)
        self.assertEqual(service.store.work(work.id)["ci_fix_attempts"], 1)
        self.assertEqual(repaired["publish_base"], old["candidate"]["commit"])
        self.assertTrue(repaired["quality"])
        self.assertTrue(repaired["review"])
        self.assertFalse(repaired["candidate"].get("conflicts"))
        for identity, action in old_actions.items():
            self.assertEqual(delivery.store.get("actions", identity), action)

    def test_conflict_diagnosis_does_not_wait_for_ci_or_reviewer_approval(self):
        service = self.start(required_checks=["unreported-CI"], reviewer_ids=[17],
                             required_approvals=1, merge={"mode": "manual", "poll_seconds": .1})
        work = service.work.submit("Implement the bounded value.")
        old = eventually(lambda: self.row(service, work) if self.row(service, work)
                         and self.row(service, work).get("change_request") else None, 40)
        self.advance_target("src/value.txt", "3\n")
        self.peer.pulls[0].update(mergeable=False, mergeable_state="dirty")
        request = eventually(lambda: self.conflict_request(service), 40)
        self.assertEqual(request.kind, "ci_repair")
        current = self.row(service, work)
        self.assertEqual(current["ci"]["previous_head"], old["candidate"]["commit"])
        self.assertEqual(current["ci"]["conflicts"], ["src/value.txt"])
        self.assertFalse(any(a["kind"] == "merge_change_request" for a in service.runtime.maintenance.delivery.rows("actions")))

    def test_unknown_publication_preserves_candidate_until_inspected(self):
        service, work, old = self.manual()
        service.pause("all")
        delivery = service.runtime.maintenance.delivery
        self.advance_target("upstream.txt", "independent change\n")
        action = next(a for a in delivery.rows("actions") if a["kind"] == "ensure_change_request")
        delivery.store.update("actions", action["id"], state="unknown")
        with self.assertRaisesRegex(ValueError, "uncertain publication"):
            service.refresh_delivery(old["id"], request_id="block-refresh-unknown")
        self.assertEqual(self.row(service, work)["candidate"], old["candidate"])
        self.assertEqual(self.row(service, work).get("candidate_history"), old.get("candidate_history"))
        calls = len(self.peer.model_requests)
        service.resume()
        current = eventually(lambda: self.row(service, work) if
            (self.row(service, work).get("read_errors") or {}).get("integration") else None, 15)
        self.assertEqual(current["state"], "integration_required")
        self.assertEqual(current["candidate"], old["candidate"])
        self.assertEqual(len(self.peer.model_requests), calls)

    def test_uncommitted_refresh_can_retry_after_target_advances_again(self):
        from homebrew_harness.services.delivery import merge_flow
        service, work, old = self.manual()
        service.pause("all")
        self.advance_target("upstream.txt", "first target\n")
        original = merge_flow.refresh_fence
        fences = []
        def interrupted(delivery, row, conn):
            result = original(delivery, row, conn)
            fences.append(True)
            if len(fences) == 2:
                raise OSError("Interrupted after preparing fixed candidate")
            return result
        with patch.object(merge_flow, "refresh_fence", interrupted):
            with self.assertRaisesRegex(SDKError, "Host persistence or resource operation failed"):
                service.refresh_delivery(old["id"], request_id="uncommitted-refresh")
        self.assertEqual(self.row(service, work)["candidate"], old["candidate"])
        self.assertEqual(self.row(service, work)["attempts"], old["attempts"])
        later = self.advance_target("upstream.txt", "second target\n")
        result = service.refresh_delivery(old["id"], request_id="retry-new-target")
        self.assertEqual(result["state"], "validating")
        self.assertEqual(self.row(service, work)["candidate"]["target_base"], later)
        self.assertEqual(len(self.row(service, work)["candidate_history"]), 1)

    def test_committed_diagnosis_survives_a_lost_completion_observation(self):
        from homebrew_harness.services.delivery import merge_flow
        service, work, old = self.manual()
        service.pause("all")
        self.advance_target("src/value.txt", "3\n")
        delivery = service.runtime.maintenance.delivery
        original = merge_flow.refresh
        def committed_then_error(*args):
            original(*args)
            raise OSError("Completion observation lost after candidate and job committed")
        with patch.object(merge_flow, "refresh", committed_then_error):
            merge_flow.refresh_waiting(delivery, old)
        current = self.row(service, work)
        jobs = [j for j in service.store.runtime.items("service_jobs_model").values()
                if j.get("target") == old["id"] and j["kind"] == "ci_triage"]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(current["state"], "ci_diagnosing")
        self.assertEqual(jobs[0]["revision"], current["revision"])

    def test_stale_pr_base_snapshot_still_reaches_current_target_diagnosis(self):
        service, work, old = self.manual()
        self.peer.stale_base_sha = old["candidate"]["target_base"]
        later = self.advance_target("src/value.txt", "3\n")
        request = eventually(lambda: self.conflict_request(service), 40)
        self.assertEqual(request.kind, "ci_repair")
        current = self.row(service, work)
        self.assertEqual(current["candidate"]["target_base"], later)
        self.assertEqual(current["ci"]["conflicts"], ["src/value.txt"])
        self.assertFalse((current.get("read_errors") or {}).get("integration"))

    def test_refresh_race_reports_observed_and_fetched_refs_without_publication(self):
        from homebrew_harness.services.delivery.integration import refresh_candidate
        from homebrew_harness.runtime.errors import error_info
        service, work, old = self.manual()
        service.pause("all")
        delivery = service.runtime.maintenance.delivery
        observed = delivery.adapter.metadata(old["change_request"])
        later = self.advance_target("upstream.txt", "new target\n")
        with self.assertRaises(ValueError) as caught:
            refresh_candidate(delivery.git, old["candidate"], observed, self.base / "raced-refresh")
        info = error_info(caught.exception)
        self.assertEqual(info["code"], "pr_version_changed")
        expected = "head=" + old["candidate"]["commit"] + ";base=" + observed["base"]
        current = "head=" + old["candidate"]["commit"] + ";base=" + later
        self.assertEqual(info["details"]["expected_version"], expected)
        self.assertEqual(info["details"]["observed_version"], current)
        self.assertIn("expected_version=" + expected, str(caught.exception))
        self.assertIn("observed_version=" + current, info["message"])
        self.assertEqual(delivery.git.ref(old["branch"]), old["candidate"]["commit"])
