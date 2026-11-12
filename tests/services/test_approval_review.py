"""Service proposal text cannot become operator authorization in auto-review."""

import json
import unittest
from types import SimpleNamespace

from homebrew_harness.services.approval_review import ServiceApprovalReview
from homebrew_harness.storage.durable_runtime import DurableStore
from homebrew_harness.execution.model_gateway import ensure_gateway
from tests.runtime import test_slash_commands as helpers
from tests.support.clients import fake_client, model_binding, reply


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
                            "authorization": "explicit",
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
                "quality": {"state": "passed"},
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
            "authorization": "explicit",
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
