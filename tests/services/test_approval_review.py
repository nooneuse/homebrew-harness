"""Service proposal text cannot become operator authorization in auto-review."""

import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from homebrew_harness.security.approval_review import ApprovalReviewer, ReviewConfig
from homebrew_harness.services.approval_review import ServiceApprovalReview
from homebrew_harness.storage.durable_runtime import DurableStore
from homebrew_harness.execution.model_gateway import ensure_gateway
from tests.runtime import test_slash_commands as helpers
from tests.runtime.test_interaction import eventually
from tests.services.communication import test_flow as communication_fixture
from tests.services.delivery import test_flow as delivery_fixture
from tests.support.clients import fake_client, model_binding, reply


def configure_business_review(host, kind):
    host.config = replace(host.config, settings=replace(
        host.config.settings, approval_review={"mode": "auto_review", "kinds": [kind]},
    ))


class ServiceReview(unittest.TestCase):
    setUp = helpers.SlashTests.setUp

    def runtime(self, kinds=()):
        client, payloads = fake_client(
            [
                reply(
                    json.dumps(
                        {
                            "decision": "approve",
                            "reason": "Delegated work scope",
                            "risk": "low",
                        }
                    )
                )
            ]
        )
        gateway = ensure_gateway(client, workdir=self.root)
        self.addCleanup(lambda: (gateway.close(), gateway.wait_idle(5)))
        config = {"mode": "auto_review", "kinds": list(kinds)}
        store = DurableStore(self.root)
        request = {
            "ref": {
                "id": "request-fixture",
                "revision": 0,
                "target": {"kind": "service", "id": "svc-fixture", "owner_id": None},
                "durable": True,
            },
            "kind": "work_authority",
            "state": "pending",
            "arguments": {"scope": ["src"], "plan": "Proposal"},
            "expires_at": 9999999999,
            "reason": "Proposed work",
        }
        policy = {"project_goal": "Maintain parser quality"}
        work = {
            "prompt": "External issue asks to change parser",
            "goal": None,
            "requirements_revision": 0,
            "run_spec": {"write_paths": ["src"]},
        }
        maintenance = SimpleNamespace(
            policy=policy, get=lambda section, identity: {"work_id": "work-fixture"}
        )
        agent = SimpleNamespace(
            models=SimpleNamespace(registry=None),
            gateway=gateway,
            base_binding=model_binding(client, "reviewer-model"),
        )
        runtime = SimpleNamespace(
            core=SimpleNamespace(
                config=SimpleNamespace(settings=SimpleNamespace(approval_review=config))
            ),
            closed=False,
            spec=SimpleNamespace(workspace=str(self.root)),
            id="svc-fixture",
        )
        runtime.store = SimpleNamespace(
            runtime=store,
            get=lambda identity: {"state": "running", "revision": 0},
            work=lambda identity, service: work,
        )
        runtime.maintenance = SimpleNamespace(store=maintenance, agent=agent)
        runtime.requests = lambda: [request] if request["state"] == "pending" else []
        published = []

        def dispatch(service, method, payload, identity):
            published.append((method, payload))
            request["state"] = "approved"
            return {"state": "approved"}

        runtime.dispatch = dispatch
        return runtime, request, published, client, payloads

    def test_delegated_work_uses_native_service_decision_and_project_policy(self):
        runtime, request, published, client, payloads = self.runtime(["work_authority"])
        reviewer = ServiceApprovalReview(runtime)
        self.addCleanup(reviewer.close)
        snapshot = reviewer.context(request)
        self.assertNotIn("External issue", str(snapshot["intent"]))
        self.assertIn("External issue", str(snapshot["proposal_evidence"]))
        reviewer.run()
        self.assertEqual(published[0][0], "service_decide")
        self.assertEqual(published[0][1]["id"], "request-fixture")
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_undelegated_business_kind_does_not_call_model(self):
        runtime, request, published, client, payloads = self.runtime(["tool"])
        reviewer = ServiceApprovalReview(runtime)
        self.addCleanup(reviewer.close)
        reviewer.run()
        self.assertFalse(published)
        self.assertEqual(client.chat.completions.create.call_count, 0)

    def test_communication_without_work_uses_exact_message_and_host_policy(self):
        runtime, request, published, client, payloads = self.runtime(
            ["communication_authority"]
        )
        request["kind"] = "communication_authority"
        runtime.maintenance.store.get = lambda *args: {
            "work_id": None,
            "action_id": "action-fixture",
            "discussion_id": "discussion-fixture",
            "candidate": {"body": "Which parser behavior?"},
        }
        runtime.store.work = lambda *args: self.fail(
            "Discussion does not have executable work"
        )
        action = {
            "id": "action-fixture",
            "plan": {"body": "Which parser behavior?", "intent": "clarify"},
        }
        runtime.maintenance.communication = SimpleNamespace(
            store=SimpleNamespace(
                get=lambda *args: action, policy={"allowed_intents": ["clarify"]}
            )
        )
        reviewer = ServiceApprovalReview(runtime)
        self.addCleanup(reviewer.close)
        reviewer.run()
        self.assertEqual(len(published), 1)
        self.assertIn("Which parser behavior?", str(payloads[0]))
        self.assertIn("allowed_intents", str(payloads[0]))

    def test_requirements_review_includes_bounded_discussion_and_prepared_navigation(self):
        runtime, request, published, client, payloads = self.runtime(["discussion_requirements"])
        draft = {"text": "Support offline export.", "acceptance": "Keep old files readable.", "scope": "code"}
        request.update(kind="discussion_requirements", arguments={"candidate": draft})
        runtime.maintenance.store.get = lambda *args: {
            "work_id": None, "discussion_id": "discussion", "candidate": draft,
        }
        runtime.maintenance.store.job_get = lambda identity: {
            "target": "discussion", "input": {"review_id": "snapshot"},
            "discussion_assessment": {"evidence": {"src/value.py": "a" * 64}},
        }
        workspace = self.root / "fixed"
        workspace.mkdir()
        rows = {
            "discussions": {
                "id": "discussion", "subject": {"kind": "change_request", "number": 1},
                "reason": "The scope is clear.", "alignment": "Preserves compatibility.",
                "projection": {"draft": draft, "facts": [{"text": "x" * 50000}]},
                "review_id": "snapshot", "last_round": "round",
            },
            "review_snapshots": {"workspace": str(workspace), "coverage": {"complete": True}},
            "rounds": {"job_id": "round-job"},
        }
        runtime.maintenance.communication = SimpleNamespace(store=SimpleNamespace(
            get=lambda section, identity: rows[section], policy={"auto_intents": []},
        ))
        reviewer = ServiceApprovalReview(runtime)
        self.addCleanup(reviewer.close)
        reviewer.run()
        self.assertEqual(published, [("service_decide", {"id": "request-fixture", "revision": 0, "decision": "approve"})])
        self.assertEqual(client.chat.completions.create.call_count, 1)
        context = json.loads(payloads[0][1]["content"])["host_context"]
        discussion = context["proposal_evidence"]["discussion"]
        self.assertEqual(discussion["projection"]["draft"], draft)
        self.assertTrue(discussion["projection"]["facts"]["truncated"])
        self.assertEqual(discussion["alignment"], rows["discussions"]["alignment"])
        self.assertEqual(discussion["review_snapshot"]["coverage"], {"complete": True})
        self.assertEqual(context["host_scope"]["communication_policy"], {"auto_intents": []})
        self.assertEqual(context["cwd"], str(workspace))
        self.assertEqual(context["evidence_navigation"], [
            {"path": "src/value.py", "observed_sha256": "a" * 64},
        ])

    def ci_review(self, *, extra_required=False):
        runtime, request, published, client, payloads = self.runtime(["ci_repair"])
        request.update(kind="ci_repair", arguments={
            "changeset_id": "change-fixture", "plan": "Restore the parser.",
            "repair_attempts": {"used": 0, "limit": 0 if extra_required else 1,
                                "extra_required": extra_required},
        })
        workspace = self.root / "candidate"
        workspace.mkdir()
        rows = {
            "decisions": {"work_id": "work-fixture", "changeset_id": "change-fixture"},
            "changesets": {
                "candidate": {"workspace": str(workspace), "commit": "a" * 40,
                              "files": {f"src/value_{i}.py": {"sha256": "b" * 64} for i in range(40)}},
                "quality": [{"recipe": "fixed", "status": "failed", "internal": "not evidence"}],
                "ci": {"kind": "independent_review", "head": "a" * 40},
                "ci_diagnosis": {"classification": "code", "plan": "Restore the parser."},
            },
        }
        runtime.maintenance.store.get = lambda section, identity: rows[section]
        runtime.maintenance.delivery = SimpleNamespace(store=SimpleNamespace(spec={"auto_ci_fix": False}))
        return runtime, request, published, client, payloads

    def test_ci_review_includes_candidate_evidence_and_publishes_without_extension(self):
        runtime, request, published, client, payloads = self.ci_review()
        reviewer = ServiceApprovalReview(runtime)
        self.addCleanup(reviewer.close)
        reviewer.run()
        self.assertEqual(published, [("service_decide", {"id": "request-fixture", "revision": 0, "decision": "approve"})])
        self.assertEqual(client.chat.completions.create.call_count, 1)
        context = json.loads(payloads[0][1]["content"])["host_context"]
        changeset = context["proposal_evidence"]["changeset"]
        self.assertEqual(changeset["candidate"]["commit"], "a" * 40)
        self.assertNotIn("files", changeset["candidate"])
        self.assertEqual(changeset["quality"], [{"recipe": "fixed", "status": "failed"}])
        self.assertEqual(changeset["ci"]["head"], "a" * 40)
        self.assertEqual(changeset["ci_diagnosis"]["plan"], request["arguments"]["plan"])
        self.assertEqual(changeset["repair_attempts"], request["arguments"]["repair_attempts"])
        self.assertEqual(context["host_scope"]["delivery_policy"], {"auto_ci_fix": False})
        self.assertEqual(context["cwd"], str(self.root / "candidate"))
        self.assertEqual(len(context["evidence_navigation"]), 16)
        self.assertEqual(context["evidence_navigation"][0], {"path": "src/value_0.py", "observed_sha256": "b" * 64})

    def test_capped_ci_review_stays_manual_in_tick_and_run(self):
        runtime, request, published, client, _ = self.ci_review(extra_required=True)
        runtime.workers = Mock()
        reviewer = ServiceApprovalReview(runtime)
        self.addCleanup(reviewer.close)
        reviewer.tick()
        runtime.workers.start.assert_not_called()
        reviewer.run()
        self.assertFalse(published)
        client.chat.completions.create.assert_not_called()
        self.assertFalse(runtime.store.runtime.items("approval_reviews"))
        self.assertEqual(request["state"], "pending")

    def test_ci_reaching_cap_during_review_supersedes_verdict(self):
        runtime, request, published, client, _ = self.ci_review()
        original = client.chat.completions.create.side_effect

        def model(**kwargs):
            result = original(**kwargs)
            request["arguments"]["repair_attempts"].update(used=1, extra_required=True)
            return result

        client.chat.completions.create.side_effect = model
        reviewer = ServiceApprovalReview(runtime)
        self.addCleanup(reviewer.close)
        reviewer.run()
        self.assertFalse(published)
        self.assertEqual(request["state"], "pending")
        record, = runtime.store.runtime.items("approval_reviews").values()
        self.assertEqual(record["state"], "superseded")

    def test_formal_pr_votes_stay_manual_even_if_communication_is_delegated(self):
        runtime, request, published, client, _ = self.runtime(["communication_authority"])
        runtime.workers = Mock()
        reviewer = ServiceApprovalReview(runtime)
        self.addCleanup(reviewer.close)
        for intent in ("approve_review", "request_changes"):
            with self.subTest(intent=intent):
                request.update(kind="communication_authority", arguments={"candidate": {"intent": intent}})
                reviewer.tick()
                runtime.workers.start.assert_not_called()
                reviewer.run()
                self.assertFalse(published)
                client.chat.completions.create.assert_not_called()
                self.assertEqual(request["state"], "pending")

    def test_delivery_review_includes_frozen_plan_and_quality_facts(self):
        runtime, request, published, client, payloads = self.runtime(
            ["delivery_authority"]
        )
        request["kind"] = "delivery_authority"
        rows = {
            "decisions": {
                "work_id": "work-fixture",
                "action_id": "action-fixture",
                "changeset_id": "change-fixture",
            },
            "actions": {
                "plan": {"subject": "Review parser update", "commit": "a" * 40}
            },
            "changesets": {
                "quality": [{"run_id": "test-fixture", "status": "passed"}],
                "review": {"decision": "accept"},
            },
        }
        runtime.maintenance.store.get = lambda section, identity: rows[section]
        runtime.maintenance.delivery = SimpleNamespace(
            store=SimpleNamespace(spec={"merge_mode": "maintainer"})
        )
        reviewer = ServiceApprovalReview(runtime)
        self.addCleanup(reviewer.close)
        reviewer.run()
        self.assertEqual(len(published), 1)
        self.assertIn("Review parser update", str(payloads[0]))
        self.assertIn("maintainer", str(payloads[0]))

    def test_large_delivery_review_keeps_accepted_constraints_without_internal_copies(self):
        from dataclasses import replace

        runtime, request, published, client, payloads = self.runtime(["delivery_authority"])
        request["kind"] = "delivery_authority"
        inventory = {f"src/file_{number}.py": {"sha256": "a" * 64, "executable": False}
                     for number in range(2000)}
        plan = {"commit": "b" * 40, "hash": "frozen-fixture", "branch": "harness/feature",
                "title": "Add parser support", "body": "Confirmed changes and tests."}
        work = {"id": "work-fixture", "prompt": "Implement the accepted parser feature.",
                "requirements_revision": 7, "requirements_id": "requirements-fixture",
                "amendments": [{"text": "preserve_binary_inputs " * 1200}],
                "execution_context": [{"text": "retain_current_test_receipt " * 1200}],
                "goal": {"criteria": ["Keep existing parser behavior"]},
                "authority": {"requirements_revision": 7, "scope": "src", "expires_at": 9999999999},
                "run_spec": {"write_paths": ["src"], "tools": ["read_file"],
                             "prompt": "compiled-copy " * 24000,
                             "work_context": {"compiled": "duplicate-copy " * 2000}}}
        rows = {"decisions": {"work_id": work["id"], "action_id": "action-fixture",
                              "changeset_id": "change-fixture"},
                "actions": {"plan": plan},
                "requirements": {"id": "requirements-fixture", "draft_revision": 7,
                                 "draft": {"out_of_scope": "No audio or video"}},
                "changesets": {"id": "change-fixture", "frozen": "frozen-fixture",
                               "candidate": {"workspace": str(self.root), "commit": "b" * 40,
                                             "delta": {"src/file_1.py": inventory["src/file_1.py"]},
                                             "files": inventory},
                               "primary_expected": inventory,
                               "quality": [{"run_id": "real-receipt", "status": "passed",
                                            "source_stable": True,
                                            "isolation": {"os_isolation_verified": True,
                                                          "cleanup_complete": True}}],
                               "review": {"text": "Approved with one warning", "findings": []}}}
        runtime.store.work = lambda *args: work
        runtime.maintenance.store.get = lambda section, identity: rows[section]
        runtime.maintenance.delivery = SimpleNamespace(store=SimpleNamespace(spec={"merge": "manual"}))
        runtime.maintenance.agent.base_binding = replace(runtime.maintenance.agent.base_binding,
                                                         context_chars=160000, max_output_tokens=20000)
        reviewer = ServiceApprovalReview(runtime)
        self.addCleanup(reviewer.close)
        reviewer.run()
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertEqual(len(published), 1)
        supplied = json.loads(payloads[0][1]["content"])["host_context"]
        self.assertEqual(supplied["proposal_evidence"]["action"]["plan"], plan)
        self.assertEqual(supplied["proposal_evidence"]["work"]["amendments"], work["amendments"])
        self.assertEqual(supplied["proposal_evidence"]["work"]["execution_context"], work["execution_context"])
        self.assertEqual(supplied["proposal_evidence"]["work"]["accepted_requirements"]["draft"],
                         rows["requirements"]["draft"])
        self.assertEqual(supplied["proposal_evidence"]["changeset"]["quality"], rows["changesets"]["quality"])
        self.assertEqual(supplied["proposal_evidence"]["changeset"]["review"], rows["changesets"]["review"])
        self.assertNotIn("files", supplied["proposal_evidence"]["changeset"]["candidate"])
        self.assertNotIn("primary_expected", supplied["proposal_evidence"]["changeset"])
        self.assertNotIn("prompt", supplied["host_scope"]["run_scope"])
        self.assertNotIn("work_context", supplied["host_scope"]["run_scope"])
        self.assertLess(len(json.dumps(supplied)), 100000)
        self.assertEqual(len(supplied["evidence_navigation"]), 16)

    def test_changed_service_request_supersedes_review(self):
        runtime, request, published, client, payloads = self.runtime(["work_authority"])
        original = client.chat.completions.create.side_effect

        def model(**kwargs):
            result = original(**kwargs)
            request["ref"]["revision"] += 1
            return result

        client.chat.completions.create.side_effect = model
        reviewer = ServiceApprovalReview(runtime)
        self.addCleanup(reviewer.close)
        reviewer.run()
        self.assertFalse(published)
        self.assertEqual(
            next(iter(runtime.store.runtime.items("approval_reviews").values()))[
                "state"
            ],
            "superseded",
        )

    def test_current_execution_instruction_change_supersedes_review(self):
        runtime, request, published, client, _ = self.runtime(["work_authority"])
        work = runtime.store.work("work-fixture", "svc-fixture")
        work["execution_context"] = [{"text": "Keep the existing accepted scope."}]
        original = client.chat.completions.create.side_effect

        def model(**kwargs):
            result = original(**kwargs)
            work["execution_context"].append({"text": "Pause public delivery pending this clarification."})
            return result

        client.chat.completions.create.side_effect = model
        reviewer = ServiceApprovalReview(runtime)
        self.addCleanup(reviewer.close)
        reviewer.run()
        self.assertFalse(published)
        self.assertEqual(next(iter(runtime.store.runtime.items("approval_reviews").values()))["state"],
                         "superseded")

    def test_long_frozen_publication_is_supplied_once_without_mutating_records(self):
        import copy

        runtime, request, published, client, payloads = self.runtime(["delivery_authority"])
        request["kind"] = "delivery_authority"
        body = "PUBLIC DRAFT WORDING " * 700
        plan = {"commit": "a" * 40, "title": "Describe the change", "body": body}
        rows = {"decisions": {"work_id": "work-fixture", "action_id": "action-fixture",
                              "changeset_id": "change-fixture", "candidate": {"scope": "delivery", "plan": str(plan)}},
                "actions": {"plan": plan},
                "changesets": {"quality": [], "review": {"text": "Accepted", "findings": []},
                               "publication": {"title": plan["title"], "body": body}}}
        request["arguments"]["plan"] = str(plan)
        original_rows, original_request = copy.deepcopy(rows), copy.deepcopy(request)
        runtime.maintenance.store.get = lambda section, identity: rows[section]
        runtime.maintenance.delivery = SimpleNamespace(store=SimpleNamespace(spec={"merge": "manual"}))
        reviewer = ServiceApprovalReview(runtime)
        self.addCleanup(reviewer.close)
        snapshot = reviewer.context(request)
        self.assertEqual(snapshot["proposal_evidence"]["action"]["plan"], plan)
        self.assertEqual(rows, original_rows)
        self.assertEqual(request, original_request)
        reviewer.run()
        self.assertEqual(len(published), 1)
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertEqual(json.dumps(payloads[0]).count(body), 1)


    def test_communication_review_uses_the_fixed_snapshot_root(self):
        runtime, request, _, _, _ = self.runtime(["communication_authority"])
        request['kind'] = 'communication_authority'
        snapshot = self.root / 'fixed'; snapshot.mkdir()
        runtime.maintenance.store.get = lambda *args: {'work_id': None, 'action_id': 'action', 'discussion_id': 'discussion'}
        values = {'communication_actions': {'id': 'action'},
                  'discussions': {'id': 'discussion', 'review_id': 'review'},
                  'review_snapshots': {'workspace': str(snapshot)}}
        runtime.maintenance.communication = SimpleNamespace(store=SimpleNamespace(get=lambda kind, identity: values[kind], policy={}))
        reviewer = ServiceApprovalReview(runtime)
        self.addCleanup(reviewer.close)
        self.assertEqual(reviewer.context(request)['cwd'], str(snapshot))

class ServiceRuntimeReview(unittest.TestCase):
    from tests.services.test_maintenance import MaintenanceTests as _fixture

    setUp = _fixture.setUp
    start = _fixture.start

    def configure_review(self):
        from homebrew_harness import Harness, HostConfig

        self.host.close()
        self.host = Harness(
            workdir=self.control,
            config=HostConfig.from_files(
                env_file=self.env,
                shutdown_seconds=5,
                approval_review={
                    "mode": "auto_review",
                    "kinds": ["work_authority"],
                    "timeout_seconds": 1,
                },
            ),
        )
        self.addCleanup(self.host.close)
        self.peer.review_verdict = {
            "decision": "approve",
            "reason": "Delegated scoped project improvement",
            "risk": "low",
        }
        self.peer.triage["disposition"] = "decide"

    def test_async_service_review_commits_native_authority_and_runs_work(self):
        from tests.runtime.test_interaction import eventually

        self.configure_review()
        service = self.start(auto=False)
        work = service.work.submit("Fix the documented parser defect.")
        eventually(lambda: work.snapshot().state == "review", 20)
        self.assertTrue(self.peer.review_started.is_set())
        self.assertTrue(
            service.store.work(work.id)["authority"]["origin"].startswith(
                "operator:auto-review-"
            )
        )
        audits = self.host.store.items("approval_reviews")
        self.assertEqual(len(audits), 1)
        self.assertEqual(next(iter(audits.values()))["state"], "approved")
        service.stop()
        self.assertTrue(service.wait_closed(8))

    def test_stop_cancels_review_and_never_publishes_late_approval(self):
        import threading

        self.configure_review()
        release = threading.Event()
        self.addCleanup(release.set)
        self.peer.review_hold = release
        service = self.start(auto=False)
        work = service.work.submit("Consider parser behavior.")
        self.assertTrue(self.peer.review_started.wait(10))
        service.stop()
        self.assertTrue(service.wait_closed(8))
        release.set()
        self.assertIsNone(service.store.work(work.id).get("authority"))


class DiscussionRuntimeReview(unittest.TestCase):
    setUp = communication_fixture.CommunicationFlow.setUp
    start = communication_fixture.CommunicationFlow.start

    def test_automatic_requirements_review_accepts_and_grants_scoped_work(self):
        configure_business_review(self.host, "discussion_requirements")
        self.peer.issue()
        self.peer.proposals = [{
            "next_step": "develop", "reason": "The scope is clear.", "alignment": "Preserve formats.",
            "draft": {"text": "Add offline export.", "scope": "code", "acceptance": "Keep old files readable."},
        }]
        with patch.object(ApprovalReviewer, "assess", return_value=(
            {"decision": "approve", "reason": "Within the configured project scope."}, [],
        )) as assess:
            service = self.start(maintenance={"grant_on_requirements_acceptance": True})
            audit = eventually(lambda: next((
                row for row in self.host.store.items("approval_reviews").values()
                if row["state"] == "approved"
            ), None), 15)
            assess.assert_called_once()
        store = service.runtime.maintenance.store
        decision = store.get("decisions", audit["request_id"])
        work = service.store.work(service.work.list()["items"][0]["id"])
        self.assertEqual(decision["kind"], "discussion_requirements")
        self.assertEqual(decision["state"], "approved")
        self.assertEqual(work["authority"]["scope"], "code")
        self.assertEqual(work["authority"]["origin"], "operator:auto-review-" + audit["id"])
        self.assertFalse(any(request.kind == "work_authority" for request in service.requests()))


class CIRuntimeReview(unittest.TestCase):
    setUp = delivery_fixture.FlowTests.setUp
    start = delivery_fixture.FlowTests.start
    run_git = delivery_fixture.FlowTests.run_git

    def test_automatic_ci_review_admits_one_native_repair_attempt(self):
        service = self.start(ci_fix_limit=1)
        delivery = service.runtime.maintenance.delivery
        reviewer = ServiceApprovalReview(service.runtime)
        reviewer.config = ReviewConfig(mode="auto_review", kinds=("ci_repair",))
        self.addCleanup(reviewer.close)
        # Exercise real request projection and dispatch without executing a repair Run.
        with (
            service.runtime.lock,
            patch.object(delivery, "advance"),
            patch.object(ApprovalReviewer, "assess", return_value=(
                {"decision": "approve", "reason": "Current candidate has a scoped conflict."}, [],
            )) as assess,
        ):
            with service.store.db.write() as conn:
                work = service.store.enqueue(conn, service.id, "Keep the existing scope.", "repair-fixture")
                work.update(state="review", run_id="completed-run")
                changeset = delivery.store.create(conn, work)
                changeset.update(
                    state="repair_waiting", candidate={"commit": "a" * 40, "target_base": "b" * 40},
                    ci_diagnosis={"classification": "code", "next_action": "repair",
                                  "plan": "Resolve the tracked conflict.", "reason": "Current target changed."},
                )
                delivery.store.put(conn, "changesets", changeset)
                pending = delivery.store.repair_request(conn, changeset)
            (request,) = service.requests()
            self.assertEqual(request.ref.id, pending["id"])
            self.assertFalse(request.arguments["repair_attempts"]["extra_required"])
            reviewer.run()
            assess.assert_called_once()
            decision = delivery.store.get("decisions", request.ref.id)
            self.assertEqual(decision["kind"], "ci_repair")
            self.assertEqual(decision["state"], "approved", self.host.store.items("approval_reviews"))
            self.assertEqual(decision["work_id"], work["id"])
            self.assertEqual(decision["authorized_attempt"], 1)
            self.assertFalse(decision["extra_attempt"])
            self.assertEqual(delivery.store.get("changesets", changeset["id"])["state"], "repair_approved")
            service.pause("all")
