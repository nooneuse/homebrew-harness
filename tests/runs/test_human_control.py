import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

from homebrew_harness import HarnessSession, Capabilities
from homebrew_harness.execution.test_runner import TestRecipe, TestRunner
from homebrew_harness.runs import RunController, RunSpec, RunStore
from homebrew_harness.runs.requests import HumanRequests
from homebrew_harness.runs.requests import digest
from tests.support.clients import fake_client, reply, call


class ManagedRunFixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.store = RunStore(self.root)
        self.addCleanup(self.store.close)
        for capture in (redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            capture.__enter__()
            self.addCleanup(capture.__exit__, None, None, None)

    def session(self, responses, *, spec=None, identity=None, tests=None):
        state = (
            self.store.get(identity)
            if identity
            else self.store.create(spec or RunSpec("Complete the requested work"))
        )
        state = self.store.claim(state["id"])
        control = RunController(
            self.store, state["id"], state["owner"], state["generation"]
        )
        client, requests = fake_client(responses)
        session = HarnessSession(
            client,
            "fixture",
            self.root,
            capabilities=Capabilities(),
            run_control=control,
            test_runner=tests,
            shutdown_timeout=0.05,
        )
        self.addCleanup(lambda: self.close(control, session))
        return control, session, client, requests

    def close(self, control, session):
        session.close()
        self.assertTrue(session.wait_closed(5))
        control.close_heartbeat()
        if self.store.get(control.id)["owner"] == control.header["owner"]:
            self.store.ended(control.header, cleanup=True)

    def make_tests(self):
        runner = TestRunner(
            self.root,
            [
                TestRecipe(
                    "check",
                    (sys.executable, "-c", 'print("verified")'),
                    input_globs=("*.txt",),
                    locked_globs=(),
                    max_runs=1,
                )
            ],
        )
        self.addCleanup(runner.close)
        return runner


class HumanControlTests(ManagedRunFixture):
    def pending_test(self):
        control, session, _, _ = self.session(
            [
                reply(
                    calls=[call("test", name="run_tests", arguments='{"name":"check"}')]
                )
            ],
            tests=self.make_tests(),
        )
        self.assertEqual(control.execute().status, "waiting")
        request = HumanRequests(self.store).records(control.id)[0]
        self.close(control, session)
        return control, request

    def test_new_user_revision_invalidates_old_approval_without_running_test(self):
        control, request = self.pending_test()
        HumanRequests(self.store).decide(control.id, request["id"], 0, "approve")
        self.store.submit(control.id, "Inspect first; revise the previous plan")
        resumed, session, _, _ = self.session(
            [
                reply(
                    calls=[
                        call(
                            "old",
                            name="execute_approved",
                            arguments=json.dumps({"request_id": request["id"]}),
                        )
                    ]
                ),
                reply("done"),
            ],
            identity=control.id,
            tests=self.make_tests(),
        )
        self.assertEqual(resumed.execute().status, "completed")
        self.assertEqual(session.tests.runs, 0)
        self.assertEqual(
            HumanRequests(self.store).get(control.id, request["id"])["state"], "stale"
        )

    def test_request_expiry_refuses_decision_and_exposes_blocker(self):
        import time
        from homebrew_harness.runs.services import RunServices

        control, request = self.pending_test()
        self.store.runtime.put(
            "run_requests", request["id"], {**request, "expires_at": time.time() - 1}
        )
        with self.assertRaises(ValueError):
            HumanRequests(self.store).decide(control.id, request["id"], 0, "approve")
        services = RunServices(self.store)
        self.addCleanup(services.close)
        services.tick(control.id)
        self.assertEqual(
            self.store.get(control.id)["last_outcome"]["reason_code"],
            "approval_expired",
        )

    def test_approval_arguments_and_test_attempts_commit_atomically(self):
        from homebrew_harness.storage.control_boundary import ProposalSuperseded

        control, request = self.pending_test()
        approved = HumanRequests(self.store).decide(
            control.id, request["id"], 0, "approve"
        )
        resumed, session, _, _ = self.session(
            [], identity=control.id, tests=self.make_tests()
        )
        resumed.prepare()
        operation = {
            "checkpoint": session.checkpoint_key,
            "state": "started",
            "name": "run_tests",
            "call_id": "probe",
            "managed_run": resumed.header,
            "test_recipe": "check",
            "operation_id": "probe:probe",
            "approval_id": approved["id"],
            "approval_revision": approved["revision"],
            "approval_context": digest(approved["context"]),
            "approval_arguments": digest({"name": "changed"}),
        }
        db = self.store.runtime.backend()
        guard = self.store.runtime.input_guard(session.inputs.recipients)
        with self.assertRaises(ProposalSuperseded):
            db.accept_operation("probe:probe", operation, guard)
        self.assertEqual(
            HumanRequests(self.store).get(control.id, request["id"])["state"],
            "approved",
        )
        self.assertNotIn("check", self.store.get(control.id).get("test_attempts", {}))
        operation["approval_arguments"] = digest({"name": "check"})
        db.accept_operation("probe:probe", operation, guard)
        self.assertEqual(
            HumanRequests(self.store).get(control.id, request["id"])["state"],
            "consumed",
        )
        self.assertEqual(self.store.get(control.id)["test_attempts"]["check"], 1)
        with self.assertRaises(ProposalSuperseded):
            db.accept_operation(
                "other:probe", {**operation, "operation_id": "other:probe"}, guard
            )
        self.assertEqual(self.store.get(control.id)["test_attempts"]["check"], 1)
        self.store.runtime.remove("operations", "probe:probe")

    def test_cancel_closes_unconsumed_requests(self):
        control, request = self.pending_test()
        self.store.control(control.id, "cancel")
        self.assertEqual(
            HumanRequests(self.store).get(control.id, request["id"])["state"],
            "cancelled",
        )
        with self.assertRaises(ValueError):
            HumanRequests(self.store).decide(control.id, request["id"], 0, "approve")

    def test_attention_answer_survives_new_session_and_is_applied_once(self):
        ask = reply(
            calls=[
                call(
                    "question",
                    name="request_attention",
                    arguments=json.dumps(
                        {
                            "question": "What should empty input return?",
                            "reason": "The requirement is ambiguous",
                            "impact": "Empty input behavior",
                        }
                    ),
                )
            ]
        )
        control, session, _, _ = self.session([ask])
        self.assertEqual(control.execute().status, "waiting")
        request = HumanRequests(self.store).records(control.id)[0]
        self.close(control, session)
        requests = HumanRequests(self.store)
        first = requests.decide(
            control.id, request["id"], 0, "answer", "Return an empty string"
        )
        again = requests.decide(
            control.id, request["id"], 0, "answer", "Return an empty string"
        )
        self.assertEqual(first, again)
        with self.assertRaises(ValueError):
            requests.decide(control.id, request["id"], 0, "approve")
        resumed, s2, _, captured = self.session([reply("done")], identity=control.id)
        self.assertEqual(resumed.execute().status, "completed")
        self.assertEqual(s2.intent.original, "Complete the requested work")
        answers = [
            m
            for m in s2.messages
            if m.get("user_input_id") == "answer_" + request["id"]
        ]
        self.assertEqual(len(answers), 1)
        self.assertEqual(
            sum(
                m.get("role") == "user"
                and "Answer to " + request["id"] in str(m.get("content"))
                for m in captured[-1]
            ),
            1,
        )
        self.assertEqual(len(s2.intent.to_dict()["amendments"]), 1)

    def test_exact_test_approval_is_consumed_once_and_does_not_replenish(self):
        proposal = reply(
            calls=[call("test", name="run_tests", arguments='{"name":"check"}')]
        )
        control, session, _, _ = self.session([proposal], tests=self.make_tests())
        self.assertEqual(control.execute().status, "waiting")
        self.assertEqual(session.tests.runs, 0)
        request = HumanRequests(self.store).records(control.id)[0]
        self.assertEqual(request["context"]["test_review"]["argv"][0], sys.executable)
        self.close(control, session)
        HumanRequests(self.store).decide(control.id, request["id"], 0, "approve")
        execute = lambda cid: reply(
            calls=[
                call(
                    cid,
                    name="execute_approved",
                    arguments=json.dumps({"request_id": request["id"]}),
                )
            ]
        )
        resumed, s2, _, _ = self.session(
            [execute("accepted"), execute("repeat"), reply("done")],
            identity=control.id,
            tests=self.make_tests(),
        )
        self.assertEqual(resumed.execute().status, "completed")
        self.assertEqual(s2.tests.runs, 1)
        self.assertEqual(self.store.get(control.id)["test_attempts"]["check"], 1)
        current = HumanRequests(self.store).get(control.id, request["id"])
        self.assertEqual(current["state"], "consumed")
        self.assertTrue(current["operation_id"].endswith(":accepted"))
        executed = [
            json.loads(m["content"])
            for m in s2.messages
            if m.get("tool_call_id") == "accepted"
        ]
        self.assertEqual(len(executed), 1)
        self.assertEqual(executed[0]["status"], "passed")
        self.assertFalse(
            self.store.runtime.items("operations", owner=s2.checkpoint_key)
        )

    def test_workspace_change_rejects_stale_approval_before_execution(self):
        (self.root / "input.txt").write_text("first")
        control, session, _, _ = self.session(
            [
                reply(
                    calls=[call("test", name="run_tests", arguments='{"name":"check"}')]
                )
            ],
            tests=self.make_tests(),
        )
        self.assertEqual(control.execute().status, "waiting")
        request = HumanRequests(self.store).records(control.id)[0]
        self.close(control, session)
        (self.root / "input.txt").write_text("changed")
        with self.assertRaisesRegex(ValueError, "Workspace changed"):
            HumanRequests(self.store).decide(control.id, request["id"], 0, "approve")
        self.assertEqual(session.tests.runs, 0)

    def test_original_write_boundary_remains_in_force(self):
        response = reply(
            calls=[
                call(
                    "outside",
                    name="write_file",
                    arguments='{"path":"outside.txt","content":"no"}',
                ),
                call(
                    "inside",
                    name="write_file",
                    arguments='{"path":"allowed.txt","content":"yes"}',
                ),
            ]
        )
        control, session, _, _ = self.session(
            [response, reply("done")],
            spec=RunSpec("Write only allowed.txt", write_paths=("allowed.txt",)),
        )
        self.assertEqual(control.execute().status, "completed")
        self.assertFalse((self.root / "outside.txt").exists())
        self.assertEqual((self.root / "allowed.txt").read_text(), "yes")
        self.assertFalse(HumanRequests(self.store).records(control.id))
