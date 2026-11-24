"""Issue #12 regression: managed test approvals use a bounded, safe summary.

All secret-looking text below is a synthetic fixture invented to match the
secret-scanning assignment pattern; it is not a real credential. The point is
that a registered launcher carrying such text must still produce an approval
request that passes HumanRequests._create's private-value check, while the
exact revision/workspace/role/recipe/candidate bindings keep rejecting stale
or changed decisions.
"""

import json
import sys

from homebrew_harness.execution.test_runner import (
    TestContext,
    TestRecipe,
    TestRunner,
    digest,
)
from homebrew_harness.execution.test_selection import LAUNCHER
from homebrew_harness.runs import RunSpec
from homebrew_harness.runs.requests import HumanRequests
from homebrew_harness.runs.requests import digest as request_digest
from homebrew_harness.security.data_boundary import redact
from homebrew_harness.storage.control_boundary import ProposalSuperseded
from tests.runs.test_human_control import ManagedRunFixture
from tests.support.clients import call, reply

FIXTURE_VALUE = "synthetic-fixture-value-0123456789"
# Unquoted assignment text: it still matches the redaction patterns after the
# request payload is JSON-encoded, and it stays valid Python for execution.
FIXTURE_CODE = f'# secret = {FIXTURE_VALUE}\nprint("verified")'


class ApprovalSummaryTests(ManagedRunFixture):
    def make_fixture_tests(self):
        runner = TestRunner(
            self.root,
            [
                TestRecipe(
                    "check",
                    (sys.executable, "-c", FIXTURE_CODE),
                    input_globs=("*.txt",),
                    locked_globs=(),
                    max_runs=1,
                )
            ],
        )
        self.addCleanup(runner.close)
        return runner

    def pending_fixture_test(self):
        control, session, _, _ = self.session(
            [
                reply(
                    calls=[call("test", name="run_tests", arguments='{"name":"check"}')]
                )
            ],
            # The isolated test container mounts a small tmpfs; this synthetic
            # run needs no real free-space reserve.
            spec=RunSpec("Complete the requested work", min_free_bytes=0),
            tests=self.make_fixture_tests(),
        )
        self.assertEqual(control.execute().status, "waiting")
        records = HumanRequests(self.store).records(control.id)
        self.assertEqual(len(records), 1)
        return control, session, records[0]

    def assert_safe_summary(self, reason, runner, name, scope_text):
        self.assertIn(f"recipe '{name}'", reason)
        self.assertIn(scope_text, reason)
        self.assertIn(f"'{runner.backend.name}' execution backend", reason)
        self.assertIn(
            "Definition reference: "
            + digest(runner.approval_identity(name, self.root)),
            reason,
        )
        self.assertNotIn(FIXTURE_CODE, reason)
        self.assertNotIn(FIXTURE_VALUE, reason)
        self.assertNotIn(sys.executable, reason)
        self.assertEqual(redact(reason), reason)
        self.assertLessEqual(len(reason), 1000)

    def test_fixture_text_trips_scanning_so_the_old_reason_would_fail(self):
        # Sanity: the synthetic launcher text really matches redaction patterns,
        # so embedding it in the reason (the old behavior) fails the gate.
        self.assertNotEqual(redact(FIXTURE_CODE), FIXTURE_CODE)
        old_style = "Approve this fixed test invocation: " + json.dumps(
            {"argv": [sys.executable, "-c", FIXTURE_CODE]}
        )
        self.assertNotEqual(redact(old_style), old_style)

    def test_permission_ask_reason_is_a_bounded_summary(self):
        runner = self.make_fixture_tests()
        action, reason = runner.permission("check", self.root, TestContext())
        self.assertEqual(action, "ask")
        self.assert_safe_summary(reason, runner, "check", "full registered suite")

    def test_permission_reason_reports_selected_scope_within_bounds(self):
        runner = TestRunner(
            self.root,
            [
                TestRecipe(
                    "suite",
                    (sys.executable, "-I", "-c", LAUNCHER),
                    selector="unittest",
                    input_globs=("*.txt",),
                    locked_globs=(),
                )
            ],
        )
        self.addCleanup(runner.close)
        targets = ["tests.execution.test_" + "x" * 300]
        targets.extend(f"tests.execution.test_case_{index}" for index in range(7))
        action, reason = runner.permission(
            "suite",
            self.root,
            TestContext(),
            selection={"targets": targets, "filter": "bounded_filter"},
        )
        self.assertEqual(action, "ask")
        self.assertIn("selected scope: 8 target(s)", reason)
        self.assertIn("tests.execution.test_case_0", reason)
        self.assertIn(", and 3 more", reason)
        self.assertIn("; filter: bounded_filter", reason)
        self.assertIn("...", reason)
        self.assertNotIn("x" * 200, reason)
        self.assertEqual(redact(reason), reason)
        self.assertLessEqual(len(reason), 1000)

    def test_fixture_launcher_request_reaches_the_approval_queue(self):
        control, session, request = self.pending_fixture_test()
        self.assertEqual(request["state"], "pending")
        self.assertEqual(request["tool"], "run_tests")
        self.assert_safe_summary(
            request["reason"], session.tests, "check", "full registered suite"
        )
        # The stable definition reference matches the unchanged exact binding.
        self.assertIn(
            "Definition reference: " + digest(request["context"]["test"]),
            request["reason"],
        )
        # The internal review context keeps the (redacted) recipe binding.
        self.assertEqual(
            request["context"]["test_review"]["argv"][0], sys.executable
        )
        self.assertNotIn(FIXTURE_VALUE, json.dumps(request["context"]))

    def test_stale_decision_is_still_rejected_with_the_summary_reason(self):
        control, session, request = self.pending_fixture_test()
        self.close(control, session)
        HumanRequests(self.store).decide(control.id, request["id"], 0, "approve")
        self.store.submit(control.id, "Inspect first; revise the previous plan")
        resumed, s2, _, _ = self.session(
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
            tests=self.make_fixture_tests(),
        )
        self.assertEqual(resumed.execute().status, "completed")
        self.assertEqual(s2.tests.runs, 0)
        self.assertEqual(
            HumanRequests(self.store).get(control.id, request["id"])["state"],
            "stale",
        )

    def test_exact_bindings_still_guard_consumption(self):
        control, session, request = self.pending_fixture_test()
        self.close(control, session)
        approved = HumanRequests(self.store).decide(
            control.id, request["id"], 0, "approve"
        )
        resumed, s2, _, _ = self.session(
            [], identity=control.id, tests=self.make_fixture_tests()
        )
        resumed.prepare()
        operation = {
            "checkpoint": s2.checkpoint_key,
            "state": "started",
            "name": "run_tests",
            "call_id": "probe",
            "managed_run": resumed.header,
            "test_recipe": "check",
            "operation_id": "probe:probe",
            "approval_id": approved["id"],
            "approval_revision": approved["revision"],
            "approval_context": request_digest(approved["context"]),
            "approval_arguments": request_digest({"name": "check"}),
        }
        db = self.store.runtime.backend()
        guard = self.store.runtime.input_guard(s2.inputs.recipients)
        # Changed arguments are rejected.
        with self.assertRaises(ProposalSuperseded):
            db.accept_operation(
                "probe:probe",
                {**operation, "approval_arguments": request_digest({"name": "changed"})},
                guard,
            )
        # Changed context (definition binding) is rejected.
        with self.assertRaises(ProposalSuperseded):
            db.accept_operation(
                "probe:probe",
                {
                    **operation,
                    "approval_context": request_digest(
                        {
                            **approved["context"],
                            "test": {"fingerprint": "0" * 64, "locked": {}},
                        }
                    ),
                },
                guard,
            )
        # Changed revision is rejected.
        with self.assertRaises(ProposalSuperseded):
            db.accept_operation(
                "probe:probe",
                {**operation, "approval_revision": approved["revision"] + 1},
                guard,
            )
        self.assertEqual(
            HumanRequests(self.store).get(control.id, request["id"])["state"],
            "approved",
        )
        self.assertNotIn("check", self.store.get(control.id).get("test_attempts", {}))
        # The exact decision is consumed once.
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

    def test_approved_fixture_recipe_executes_once_through_the_exact_request(self):
        control, session, request = self.pending_fixture_test()
        self.close(control, session)
        HumanRequests(self.store).decide(control.id, request["id"], 0, "approve")
        resumed, s2, _, _ = self.session(
            [
                reply(
                    calls=[
                        call(
                            "accepted",
                            name="execute_approved",
                            arguments=json.dumps({"request_id": request["id"]}),
                        )
                    ]
                ),
                reply("done"),
            ],
            identity=control.id,
            tests=self.make_fixture_tests(),
        )
        self.assertEqual(resumed.execute().status, "completed")
        self.assertEqual(s2.tests.runs, 1)
        current = HumanRequests(self.store).get(control.id, request["id"])
        self.assertEqual(current["state"], "consumed")
        executed = [
            json.loads(m["content"])
            for m in s2.messages
            if m.get("tool_call_id") == "accepted"
        ]
        self.assertEqual(len(executed), 1)
        self.assertEqual(executed[0]["status"], "passed")
