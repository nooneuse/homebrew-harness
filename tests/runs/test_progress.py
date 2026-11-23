import json

from homebrew_harness.execution.model_protocol import ToolInvocation
from homebrew_harness.execution.test_runner import TestRunner
from homebrew_harness.runs import RunSpec
from homebrew_harness.runs.progress import ProgressTracker
from homebrew_harness.runs.requests import digest
from homebrew_harness.runtime.limits import limits_scope
from tests.runs import test_human_control as support
from tests.support.clients import call, reply


class ProgressTests(support.ManagedRunFixture):
    def test_repeated_ineffective_writes_replan_then_block_across_episodes(self):
        writes = [
            reply(
                calls=[
                    call(
                        "write" + str(i),
                        name="write_file",
                        arguments=json.dumps({"path": "same.txt", "content": "same"}),
                    )
                ]
            )
            for i in range(7)
        ]
        control, session, api, _ = self.session(
            writes, spec=RunSpec("Keep writing same.txt", episode_model_calls=1, repeat_limit=3, replan_limit=1)
        )
        self.assertEqual(control.execute().reason_code, "no_progress")
        state = ProgressTracker(self.store).get(control.id)
        self.assertTrue(state["blocked"])
        self.assertEqual(state["replans"], 1)
        self.assertEqual(api.chat.completions.create.call_count, 7)
        self.close(control, session)
        self.store.control(control.id, "run")
        resumed, again, client, _ = self.session([reply("done")], identity=control.id)
        self.assertEqual(resumed.execute().reason_code, "no_progress")
        client.chat.completions.create.assert_not_called()

    def test_readonly_analysis_is_not_counted_as_no_progress(self):
        (self.root / "info.txt").write_text("information")
        reads = [
            reply(
                calls=[
                    call(
                        "read" + str(i),
                        name="read_file",
                        arguments='{"path":"info.txt"}',
                    )
                ]
            )
            for i in range(10)
        ]
        control, _, _, _ = self.session([*reads, reply("done")])
        self.assertEqual(control.execute().status, "completed")
        state = ProgressTracker(self.store).get(control.id)
        self.assertEqual(state["count"], 0)
        self.assertIsNone(state["feedback"])

    def test_full_failure_and_redundant_selected_passes_have_separate_feedback(self):
        (self.root / "tests").mkdir()
        (self.root / "tests" / "__init__.py").touch()
        (self.root / "tests" / "test_suite.py").write_text(
            "import unittest\n"
            "class Suite(unittest.TestCase):\n"
            "    def test_pass(self): self.assertTrue(True)\n"
            "    def test_fail(self): self.fail('fixture failure')\n"
        )
        runner = TestRunner.default(self.root)
        self.addCleanup(runner.close)
        full = {"name": "unit-tests"}
        selected = {**full, "selection": {"targets": ["tests.test_suite.Suite.test_pass"]}}
        steps = [full, selected, full, selected, full, selected, selected, selected]
        responses = [
            reply(calls=[call(str(i), name="run_tests", arguments=json.dumps(args))])
            for i, args in enumerate(steps)
        ]
        responses.append(reply(calls=[call(
            "report", name="report_run_blocker", arguments=json.dumps({
                "reason": "Full suite still fails; the focused retry did not reproduce it.",
                "required_action": "Inspect the full failure before delivery.",
            }),
        )]))
        control, _, _, requests = self.session(
            responses, tests=runner,
            spec=RunSpec("Diagnose the suite", allow_tests=("unit-tests",), episode_model_calls=2),
        )
        outcome = control.execute()
        self.assertEqual((outcome.status, outcome.reason_code), ("blocked", "model_blocked"))
        self.assertEqual(runner.runs, 8)  # Even a rerun after the advisory is permitted.

        def feedback(index):
            return next((json.loads(message["content"]) for message in requests[index]
                         if message.get("name") == "run_progress"), None)

        self.assertIsNone(feedback(4))  # Two failures aren't enough, even interleaved with passes.
        failure = feedback(5)
        self.assertEqual(failure["kind"], "test_failure")
        self.assertEqual(failure["evidence"]["test_scope"], "full")
        self.assertEqual(failure["decision"], "replan_required")
        self.assertIn("what it would establish", failure["action"])
        self.assertIn("report_run_blocker", failure["action"])
        repeated = feedback(7)
        self.assertEqual(repeated["kind"], "redundant_verification")
        self.assertEqual(repeated["count"], 3)  # The first pass is a new observation.
        self.assertEqual(repeated["enforcement"], "advisory")
        self.assertIn("controlled retry", repeated["action"])
        self.assertEqual(repeated["other_obstructions"][0]["signature"], failure["signature"])

        state = control.progress.get(control.id)
        self.assertFalse(state["blocked"])  # Blocked by the chosen report, not by a global cap.
        rows = {row["kind"]: row for row in state["observations"].values()}
        self.assertEqual(rows["test_failure"]["count"], 3)
        self.assertEqual(rows["redundant_verification"]["count"], 4)
        for row in rows.values():
            evidence = row["evidence"]
            self.assertNotEqual(evidence["first_receipt_ref"], evidence["receipt_ref"])
            for reference in (evidence["first_receipt_ref"], evidence["receipt_ref"]):
                receipt = self.store.runtime.get("test_receipts", reference)
                self.assertEqual(evidence["basis"], digest(receipt["verification_basis"]))

    def test_new_input_contract_or_backend_is_not_an_identical_pass(self):
        control, session, _, _ = self.session([], spec=RunSpec(
            "Verify current inputs", repeat_warning=1, repeat_limit=1, replan_limit=0,
        ))
        tracker = control.progress
        basis = {"version": 1, "inputs": "source-1", "contract": "recipe-1", "backend": "native",
                 "role": "lead", "test_scope": "full", "selection": None}

        def observe(receipt, **overrides):
            args = {"name": "check", "execution": {"wait_seconds": receipt}}
            output = {"run_id": "test-" + str(receipt), "recipe": "check", "cwd": ".",
                      "status": "passed", "exit_code": 0, "source_stable": True,
                      "verification_basis": basis, **overrides}
            self.observe(control, "run_tests", args, output)
            return tracker.get(control.id)

        self.assertIsNone(observe(1)["feedback"])
        self.assertEqual(observe(2)["feedback"]["count"], 1)
        state = observe(2, executed_at=42)  # Re-reading the same receipt isn't a rerun.
        self.assertEqual(state["feedback"]["count"], 1)
        self.assertEqual(observe(3, source_stable=False)["revision"], state["revision"])
        self.assertEqual(observe(3, verification_basis=None)["revision"], state["revision"])
        for new, field in enumerate(("inputs", "contract", "backend"), 3):
            basis[field] += "-changed"
            self.assertIsNone(observe(new)["feedback"], field)
        basis.update(test_scope="selected", selection={"targets": ["tests.first"]})
        self.assertIsNone(observe(6)["feedback"])
        basis["selection"] = {"targets": ["tests.second"]}
        self.assertIsNone(observe(7)["feedback"])
        basis["selection"] = {"targets": ["tests.first"]}
        state = observe(8)
        self.assertEqual(state["feedback"]["count"], 1)
        self.assertEqual(state["feedback"]["enforcement"], "advisory")
        self.assertFalse(state["blocked"])  # Existing hard limits did not apply to passes.
        scope = control.evidence_scope()
        session.intent.update("Use the amended verification requirements.")
        self.assertEqual(observe(9, scope=scope)["revision"], state["revision"])
        self.assertEqual(tracker.ensure_intent(control)["observations"], {})

    def test_file_cycle_survives_new_content_on_other_path_and_clears_on_its_own(self):
        control, _, _, _ = self.session([], spec=RunSpec("Update two files", repeat_warning=2))

        def write(path, content):
            (self.root / path).write_text(content)
            self.observe(control, "write_file", {"path": path, "content": content}, {"path": path})
            return control.progress.get(control.id)

        write("loop.txt", "a")
        write("loop.txt", "b")
        write("loop.txt", "a")
        write("unrelated.txt", "new")
        state = write("loop.txt", "b")
        self.assertEqual(state["feedback"]["kind"], "ineffective_mutation")
        self.assertEqual(state["feedback"]["evidence"]["pattern"], "cycle")
        self.assertEqual(write("unrelated.txt", "newer")["feedback"], state["feedback"])
        self.assertIsNone(write("loop.txt", "c")["feedback"])

    def test_active_warning_does_not_consume_the_recent_baseline_budget(self):
        control, _, _, _ = self.session([], spec=RunSpec("Diagnose and verify", repeat_warning=2))
        with limits_scope({"progress_paths": 1}):
            tracker = control.progress
            tracker.record(control, "obstacle", "Original unresolved obstruction")
            tracker.record(control, "obstacle", "Original unresolved obstruction")
            for number in (1, 2):
                self.observe(control, "run_tests", {"name": "check"}, {
                    "run_id": "test-" + str(number), "recipe": "check", "cwd": ".",
                    "status": "passed", "exit_code": 0, "source_stable": True,
                    "verification_basis": {"version": 1, "inputs": "source", "contract": "recipe",
                                           "backend": "native", "role": "lead", "test_scope": "full",
                                           "selection": None},
                })
            state = tracker.get(control.id)
        self.assertEqual(state["feedback"]["signature"], "obstacle")
        self.assertEqual(next(row for row in state["observations"].values()
                              if row["kind"] == "redundant_verification")["count"], 1)

    def test_legacy_counter_continues_explicit_hard_budget_with_stable_error_code(self):
        control, _, _, _ = self.session([], spec=RunSpec("Fix a file", repeat_limit=2, replan_limit=1))
        tracker = control.progress
        args = {"path": "item.txt", "expected_version": "old"}
        first_error = {"error": "Version moved: request one", "error_info": {"code": "version_changed"}}
        legacy = {key: value for key, value in tracker.ensure_intent(control).items() if key != "observations"}
        legacy.update(count=1, signature=digest({"tool": "delete_file", "args": args, "failure": {
            "error": first_error["error"], "status": None, "exit_code": None, "reason_code": None,
        }}))
        self.store.runtime.put("run_progress", control.id, legacy)
        self.observe(control, "delete_file", args, first_error)
        warning = tracker.get(control.id)
        self.assertEqual(warning["count"], 2)
        self.assertEqual(warning["replans"], 1)
        self.assertFalse(warning["blocked"])
        for error in ("request two", "request three"):
            self.observe(control, "delete_file", args, {**first_error, "error": error})
        blocked = tracker.get(control.id)
        self.assertTrue(blocked["blocked"])
        self.assertEqual(blocked["count"], 4)
        self.assertEqual(blocked["feedback"]["enforcement"], "hard")
        self.observe(control, "delete_file", args, {"path": "item.txt", "already_absent": True})
        self.assertEqual(tracker.get(control.id), blocked)
        cleared = tracker.reset(control.id, blocked["revision"], "Operator changed the precondition")
        self.assertFalse(cleared["blocked"])
        self.assertEqual(cleared["observations"], {})

    def observe(self, control, name, args, result):
        control.progress.observe(
            control, ToolInvocation("fixture", name, json.dumps(args)), self.root, json.dumps(result),
        )
