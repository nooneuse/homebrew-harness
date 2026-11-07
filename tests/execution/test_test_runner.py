from tests.support.tools import fixture_invocation
"""Real fixed commands and deterministic goal proof, without remote models."""

from homebrew_harness.runtime.capabilities import Capabilities

import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import subprocess
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from tests.support.tools import execute_fixture_tool

from homebrew_harness.orchestration.goal_loop import GoalController, GoalEvaluation
from homebrew_harness.orchestration.hard_conditions import (
    HardVerifier,
    validate_spec,
    ConditionError,
)
from homebrew_harness.runtime.session import HarnessSession
from homebrew_harness.execution.model_gateway import Deadline, ModelGateway
from homebrew_harness.security.permissions import (
    PermissionDecision,
    noninteractive_permissions,
)
from tests.support.clients import fake_client, reply, call
from homebrew_harness.execution.test_runner import (
    TestRunner,
    TestRecipe,
    TestRunnerError,
    TestContext,
    load_test_runner,
)


class Fixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        (self.root / "source.py").write_text("value = 1\n")
        for redirect in (
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            redirect.__enter__()
            self.addCleanup(redirect.__exit__, None, None, None)

    def runner(self, code="print('ok')", *, approve=True, **settings):
        recipe = TestRecipe("suite", (sys.executable, "-c", code), **settings)
        runner = TestRunner(self.root, [recipe])
        self.addCleanup(runner.close)
        if approve:
            runner.approve("suite")
        return runner

    def invoke(self, runner, tool_name="run_tests", **arguments):
        if tool_name == "run_tests" and not arguments:
            arguments = {"name": "suite"}
        block = (
            reply(calls=[call("test", name=tool_name, arguments=json.dumps(arguments))])
            .choices[0]
            .message.tool_calls[0]
        )
        return json.loads(execute_fixture_tool(block, self.root, test_runner=runner))

    def goal(self, runner=None, evaluator=None):
        evaluator = evaluator or Mock()
        if isinstance(evaluator, Mock):
            evaluator.total_tokens = 0
            evaluator.known_usage_calls = 0
        verifier = HardVerifier(self.root, test_runner=runner)
        goal = GoalController(self.root, evaluator, hard_verifier=verifier)
        self.addCleanup(goal.close)
        return goal, evaluator

    def spec(self, predicates, judge=False):
        return {
            "condition": "Meet explicit requirements",
            "predicates": predicates,
            "judge": judge,
        }


class RunnerTests(Fixture):
    def test_actual_success_and_failure_have_host_receipts(self):
        runner = self.runner()
        result = runner.run("suite")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(runner.verify("suite", self.root)["ok"])
        failing = self.runner("import sys; sys.exit(7)")
        result = failing.run("suite")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 7)
        self.assertFalse(failing.verify("suite", self.root)["ok"])

    def test_no_grant_never_starts_process_or_modifies_workspace(self):
        runner = self.runner("open('marker','w').write('bad')", approve=False)
        with (
            patch("homebrew_harness.execution.test_runner.subprocess.Popen") as spawn,
            self.assertRaisesRegex(TestRunnerError, "approval_required"),
        ):
            runner.run("suite")
        spawn.assert_not_called()
        self.assertFalse((self.root / "marker").exists())

    def test_noninteractive_tool_uses_preapproval_without_shell_permission(self):
        runner = self.runner()
        with (
            noninteractive_permissions(),
            patch("homebrew_harness.security.permissions.ask_user") as ask,
        ):
            result = self.invoke(runner)
        self.assertEqual(result["status"], "passed")
        ask.assert_not_called()

    def test_unapproved_noninteractive_call_is_denied(self):
        runner = self.runner(approve=False)
        with (
            noninteractive_permissions(),
            patch("homebrew_harness.execution.test_runner.subprocess.Popen") as spawn,
        ):
            result = self.invoke(runner)
        self.assertEqual(result["permission"], "deny")
        spawn.assert_not_called()

    def test_exact_terminal_approval_records_a_one_shot_grant(self):
        runner = self.runner(approve=False)
        with patch(
            "homebrew_harness.security.permissions.ask_user",
            return_value=PermissionDecision("allow", "Fixture approval"),
        ) as ask:
            result = self.invoke(runner)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(runner.grants[-1].remaining, 0)
        ask.assert_called_once()

    def test_tool_arguments_cannot_override_command_env_or_limits(self):
        runner = self.runner()
        for extra in (
            {"argv": ["bad"]},
            {"env": {"BAD": "1"}},
            {"timeout": 999},
            {"role": "lead"},
            {"workdir": "outside"},
        ):
            with patch(
                "homebrew_harness.execution.test_runner.subprocess.Popen"
            ) as spawn:
                result = self.invoke(
                    runner, tool_name="run_tests", **{"name": "suite", **extra}
                )
            self.assertIn("error", result)
            spawn.assert_not_called()

    def test_role_and_task_scopes_cannot_be_widened(self):
        runner = self.runner(approve=False, roles=("lead", "teammate"))
        runner.approve("suite", cwd=self.root, role="lead", task_id="task_aaaaaaaa")
        for context in (
            TestContext("teammate", "task_aaaaaaaa"),
            TestContext("lead", "task_bbbbbbbb"),
        ):
            with self.assertRaises(TestRunnerError):
                runner.run("suite", context=context)
        self.assertEqual(
            runner.run("suite", context=TestContext("lead", "task_aaaaaaaa"))["status"],
            "passed",
        )

    def test_expired_and_exhausted_grants_refuse_execution(self):
        runner = self.runner(max_runs=1)
        runner.run("suite")
        with self.assertRaises(TestRunnerError):
            runner.run("suite")
        runner.approve("suite")
        runner.grants[-1].expires = time.monotonic() - 1
        with self.assertRaises(TestRunnerError):
            runner.run("suite")

    def test_protected_test_changes_require_reapproval(self):
        (self.root / "tests").mkdir()
        path = self.root / "tests/test_code.py"
        path.write_text("old")
        runner = self.runner(locked_globs=("tests/**",))
        path.write_text("changed")
        with self.assertRaises(TestRunnerError):
            runner.run("suite")
        runner.approve("suite")
        self.assertEqual(runner.run("suite")["status"], "passed")

    def test_changed_definition_during_terminal_approval_is_refused(self):
        (self.root / "tests").mkdir()
        path = self.root / "tests/test_code.py"
        path.write_text("old")
        runner = self.runner(approve=False, locked_globs=("tests/**",))

        def approve(*args):
            path.write_text("changed while approving")
            return PermissionDecision("allow", "Fixture human approval")

        with (
            patch(
                "homebrew_harness.security.permissions.ask_user", side_effect=approve
            ),
            patch("homebrew_harness.execution.test_runner.subprocess.Popen") as spawn,
        ):
            result = self.invoke(runner)
        self.assertIn("changed during approval", result["error"])
        spawn.assert_not_called()

    def test_source_change_during_execution_invalidates_zero_exit(self):
        runner = self.runner("open('source.py','w').write('value=2')")
        result = runner.run("suite")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["status"], "changed")
        self.assertFalse(runner.verify("suite", self.root)["ok"])

    def test_change_and_restore_after_pass_still_invalidates_version(self):
        runner = self.runner()
        runner.run("suite")
        path = self.root / "source.py"
        original = path.read_text()
        path.write_text("changed")
        path.write_text(original)
        self.assertFalse(runner.verify("suite", self.root)["ok"])

    def test_forged_or_old_session_receipt_files_are_not_trusted(self):
        runner = self.runner()
        receipt = runner.run("suite")
        other = self.runner()
        with self.assertRaises(TestRunnerError):
            other.get(receipt["run_id"])
        self.assertFalse(other.verify("suite", self.root)["ok"])
        file = (
            self.root
            / ".harness/tests"
            / runner.session_id
            / receipt["run_id"]
            / "receipt.json"
        )
        data = json.loads(file.read_text())
        data["status"] = "forged"
        file.write_text(json.dumps(data))
        self.assertEqual(runner.get(receipt["run_id"])["status"], "passed")

    def test_output_limit_stops_and_cannot_count_as_success(self):
        runner = self.runner("print('x'*100000)", output_bytes=1000)
        result = runner.run("suite")
        self.assertEqual(result["status"], "output_limit")
        self.assertFalse(runner.verify("suite", self.root)["ok"])

    def test_timeout_kills_command_and_records_failure(self):
        runner = self.runner("import time; time.sleep(5)", timeout=0.1)
        started = time.monotonic()
        result = runner.run("suite")
        self.assertEqual(result["status"], "timeout")
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(runner.verify("suite", self.root)["ok"])

    def test_parent_deadline_clamps_test_timeout(self):
        runner = self.runner("import time; time.sleep(5)", timeout=5)
        client, _ = fake_client([])
        gateway = ModelGateway(client)
        with gateway.scope(deadline=Deadline.after(0.1)):
            result = runner.run("suite")
        self.assertEqual(result["status"], "timeout")
        self.assertLess(result["elapsed_seconds"], 1)

    def test_close_cancels_running_test_without_unbounded_join(self):
        runner = self.runner("import time; time.sleep(5)")
        result = []
        thread = threading.Thread(target=lambda: result.append(runner.run("suite")))
        thread.start()
        limit = time.monotonic() + 1
        while not runner._processes and time.monotonic() < limit:
            threading.Event().wait(0.01)
        runner.close()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0]["status"], "cancelled")

    def test_environment_does_not_inherit_credentials_or_real_home(self):
        runner = self.runner(
            "import os; print(os.getenv('OPENAI_API_KEY','absent')); print(os.environ['HOME'])"
        )
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fixture-private-key-value"}):
            result = runner.run("suite")
        self.assertIn("absent", result["stdout_preview"])
        self.assertNotIn("fixture-private-key-value", result["stdout_preview"])
        self.assertIn(".harness/tests", result["stdout_preview"])

    def test_workspace_symlink_inputs_are_refused(self):
        (self.root / "alias.py").symlink_to(self.root / "source.py")
        runner = self.runner()
        with self.assertRaises((OSError, TestRunnerError)):
            runner.run("suite")

    def test_unsafe_registry_environment_and_shell_are_rejected(self):
        for env in (
            ("PYTHONPATH", "outside"),
            ("OPENAI_API_KEY", "secret"),
            ("LD_PRELOAD", "outside"),
        ):
            with self.assertRaises(TestRunnerError):
                TestRecipe("bad", (sys.executable, "-c", "pass"), env=(env,))
        with self.assertRaises(TestRunnerError):
            TestRecipe("bad", ("/bin/sh", "-c", "echo bad"))

    def test_default_suite_detects_zero_tests_and_avoids_module_shadowing(self):
        (self.root / "tests").mkdir()
        (self.root / "unittest.py").write_text("raise RuntimeError('shadowed')")
        runner = TestRunner.default(self.root)
        self.addCleanup(runner.close)
        runner.approve("unit-tests")
        result = runner.run("unit-tests")
        self.assertEqual(result["exit_code"], 3)
        (self.root / "tests/test_real.py").write_text(
            "import unittest\nclass Test(unittest.TestCase):\n def test_real(self): self.assertTrue(True)\n"
        )
        runner.approve("unit-tests")
        self.assertEqual(runner.run("unit-tests")["status"], "passed")

    def test_inflight_verification_cannot_reuse_an_earlier_pass(self):
        runner = self.runner()
        runner.run("suite")
        runner.active.set()
        try:
            self.assertFalse(runner.verify("suite", self.root)["ok"])
        finally:
            runner.active.clear()

    def test_builtin_registry_shortcut_uses_the_trusted_bootstrap(self):
        (self.root / "tests").mkdir()
        (self.root / "tests/test_real.py").write_text(
            "import unittest\nclass Test(unittest.TestCase):\n def test_real(self): self.assertTrue(True)\n"
        )
        path = self.root.parent / (self.root.name + "-builtin.json")
        self.addCleanup(path.unlink, missing_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "recipes": [
                        {
                            "name": "unit-tests",
                            "builtin": "unittest",
                            "authorization": "allow",
                        }
                    ],
                }
            )
        )
        runner = load_test_runner(path, self.root)
        self.addCleanup(runner.close)
        self.assertEqual(runner.run("unit-tests")["status"], "passed")

    def test_host_registry_must_live_outside_workspace(self):
        path = self.root / "tests.json"
        path.write_text('{"version":1,"recipes":[]}')
        with self.assertRaises(TestRunnerError):
            load_test_runner(path, self.root)
        outside = self.root.parent / (self.root.name + "-registry.json")
        self.addCleanup(outside.unlink, missing_ok=True)
        outside.write_text(
            json.dumps(
                {
                    "version": 1,
                    "recipes": [
                        {
                            "name": "suite",
                            "argv": ["{python}", "-c", "print('ok')"],
                            "authorization": "allow",
                        }
                    ],
                }
            )
        )
        runner = load_test_runner(outside, self.root)
        self.addCleanup(runner.close)
        self.assertEqual(runner.run("suite")["status"], "passed")


class HardGoalTests(Fixture):
    def test_missing_file_cannot_be_overridden_by_positive_model_verdict(self):
        goal, judge = self.goal(
            evaluator=Mock(
                evaluate=Mock(
                    return_value=GoalEvaluation(
                        True, "Claimed complete", evidence_ids=("final",)
                    )
                )
            )
        )
        goal.set_goal(
            self.spec(
                [{"id": "file", "type": "file_exists", "path": "missing.txt"}],
                judge=True,
            )
        )
        decision = goal.evaluate_after_turn("I created the file")
        self.assertEqual(decision.action, "block")
        judge.evaluate.assert_not_called()
        self.assertNotEqual(goal.state["status"], "achieved")

    def test_file_existence_content_and_hash_are_verified_locally(self):
        path = self.root / "result.txt"
        path.write_text("ready")
        goal, judge = self.goal()
        goal.set_goal(
            self.spec(
                [
                    {
                        "id": "exists",
                        "type": "file_exists",
                        "path": "result.txt",
                        "min_bytes": 1,
                    },
                    {
                        "id": "text",
                        "type": "file_contains",
                        "path": "result.txt",
                        "text": "ready",
                    },
                    {
                        "id": "hash",
                        "type": "file_sha256",
                        "path": "result.txt",
                        "sha256": hashlib.sha256(b"ready").hexdigest(),
                    },
                ]
            )
        )
        self.assertEqual(goal.evaluate_after_turn("").action, "achieved")
        judge.evaluate.assert_not_called()

    def test_mixed_goal_requires_both_hard_checks_and_judge(self):
        (self.root / "result.txt").write_text("ready")
        judge = Mock(
            evaluate=Mock(return_value=GoalEvaluation(False, "Need explanation"))
        )
        goal, _ = self.goal(evaluator=judge)
        goal.set_goal(
            self.spec(
                [{"id": "file", "type": "file_exists", "path": "result.txt"}],
                judge=True,
            )
        )
        self.assertEqual(
            goal.evaluate_after_turn("incomplete explanation").action, "block"
        )
        judge.evaluate.assert_called_once()

    def test_mutation_during_judge_invalidates_earlier_hard_check(self):
        path = self.root / "result.txt"
        path.write_text("ready")

        def evaluate(packet):
            path.unlink()
            return GoalEvaluation(True, "Text supplied", evidence_ids=("final",))

        goal, _ = self.goal(evaluator=Mock(evaluate=evaluate))
        goal.set_goal(
            self.spec(
                [{"id": "file", "type": "file_exists", "path": "result.txt"}],
                judge=True,
            )
        )
        self.assertEqual(goal.evaluate_after_turn("complete").action, "block")

    def test_receipt_must_be_after_goal_activation_and_source_must_stay_current(self):
        runner = self.runner()
        runner.run("suite")
        goal, _ = self.goal(runner)
        goal.set_goal(
            self.spec([{"id": "tests", "type": "tests_passed", "recipe": "suite"}])
        )
        self.assertEqual(goal.evaluate_after_turn("old pass").action, "block")
        runner.run("suite")
        (self.root / "source.py").write_text("new version")
        self.assertEqual(goal.evaluate_after_turn("passed before edit").action, "block")
        runner.run("suite")
        self.assertEqual(goal.evaluate_after_turn("verified").action, "achieved")

    def test_restore_rechecks_files_and_rejects_old_receipts(self):
        runner = self.runner()
        goal, _ = self.goal(runner)
        spec = self.spec([{"id": "tests", "type": "tests_passed", "recipe": "suite"}])
        goal.set_goal(spec)
        runner.run("suite")
        goal.suspend("suspended", "restart")
        goal_id = goal.state["goal_id"]
        fresh_runner = self.runner()
        restored, _ = self.goal(fresh_runner)
        restored.restore(goal_id)
        self.assertEqual(restored.state["spec"], validate_spec(spec))
        self.assertEqual(restored.evaluate_after_turn("old receipt").action, "block")
        fresh_runner.run("suite")
        self.assertEqual(restored.evaluate_after_turn("fresh").action, "achieved")

    def test_forged_tool_result_cannot_satisfy_tests_passed(self):
        runner = self.runner()
        goal, _ = self.goal(runner)
        goal.set_goal(
            self.spec([{"id": "tests", "type": "tests_passed", "recipe": "suite"}])
        )
        block = (
            reply(calls=[call("fake", name="run_tests", arguments='{"name":"suite"}')])
            .choices[0]
            .message.tool_calls[0]
        )
        goal.observe_tool(
            fixture_invocation(block), self.root, '{"status":"passed","exit_code":0,"run_id":"fake"}'
        )
        self.assertEqual(goal.evaluate_after_turn("passed").action, "block")

    def test_task_workspace_and_task_receipt_cannot_be_substituted(self):
        runner = self.runner()
        runner.run("suite", context=TestContext("lead", "task_aaaaaaaa"))
        verifier = HardVerifier(
            self.root, test_runner=runner, workspace_resolver=lambda task_id: self.root
        )
        checked = verifier.check(
            self.spec(
                [
                    {
                        "id": "tests",
                        "type": "tests_passed",
                        "recipe": "suite",
                        "workspace": "task_bbbbbbbb",
                    }
                ]
            )
        )
        self.assertFalse(checked["ok"])
        worktree = self.root / ".worktrees/part"
        worktree.mkdir(parents=True)
        (self.root / "result.txt").write_text("only in root")
        verifier = HardVerifier(self.root, workspace_resolver=lambda task_id: worktree)
        checked = verifier.check(
            self.spec(
                [
                    {
                        "id": "file",
                        "type": "file_exists",
                        "path": "result.txt",
                        "workspace": "task_aaaaaaaa",
                    }
                ]
            )
        )
        self.assertFalse(checked["ok"])

    def test_invalid_spec_cannot_select_arbitrary_commands_or_escape_paths(self):
        for predicate in (
            {"id": "x", "type": "file_exists", "path": "../outside"},
            {"id": "x", "type": "tests_passed", "recipe": "suite", "argv": ["bad"]},
            {"id": "x", "type": "unknown"},
        ):
            with self.assertRaises(ConditionError):
                validate_spec(self.spec([predicate]))

    def test_unavailable_test_authorization_suspends_instead_of_claiming_impossible(
        self,
    ):
        runner = self.runner(approve=False)
        goal, _ = self.goal(runner)
        goal.set_goal(
            self.spec([{"id": "tests", "type": "tests_passed", "recipe": "suite"}])
        )
        with noninteractive_permissions():
            decision = goal.evaluate_after_turn("Need test permission")
        self.assertEqual(decision.action, "suspended")
        self.assertTrue(goal.pending)


class IntegrationTests(Fixture):
    def test_pure_hard_goal_completes_after_tool_batch_without_judge_or_final_model_call(
        self,
    ):
        runner = self.runner()
        client, _ = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "write",
                            name="write_file",
                            arguments='{"path":"result.txt","content":"ready"}',
                        ),
                        call("test", name="run_tests", arguments='{"name":"suite"}'),
                    ]
                )
            ]
        )
        session = HarnessSession(
            client,
            "test",
            self.root,
            test_runner=runner,
            capabilities=Capabilities.full(memory_mode="off"),
        )
        self.addCleanup(lambda: session.wait_closed(5))
        self.addCleanup(session.close)
        spec = self.spec(
            [
                {
                    "id": "file",
                    "type": "file_contains",
                    "path": "result.txt",
                    "text": "ready",
                },
                {"id": "tests", "type": "tests_passed", "recipe": "suite"},
            ]
        )
        result = session.run_user("/goal spec " + json.dumps(spec))
        self.assertIn("[goal achieved]", result)
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertEqual(session.goal.state["progress"]["judge_attempts"], 0)

    def test_host_test_commands_do_not_call_model_and_revoke_execution(self):
        runner = self.runner(approve=False)
        client, _ = fake_client([])
        session = HarnessSession(
            client,
            "test",
            self.root,
            test_runner=runner,
            capabilities=Capabilities.full(memory_mode="off"),
        )
        self.addCleanup(lambda: session.wait_closed(5))
        self.addCleanup(session.close)
        self.assertIn("suite", session.run_user("/tests"))
        session.run_user("/tests approve suite")
        self.assertEqual(runner.run("suite")["status"], "passed")
        session.run_user("/tests revoke suite")
        with self.assertRaises(TestRunnerError):
            runner.run("suite")
        client.chat.completions.create.assert_not_called()

    def test_subagent_receives_runner_but_cannot_widen_role(self):
        runner = self.runner(roles=("lead",))
        client, _ = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "delegate", name="task", arguments='{"prompt":"Run suite"}'
                        )
                    ]
                ),
                reply(
                    calls=[call("test", name="run_tests", arguments='{"name":"suite"}')]
                ),
                reply("denied"),
                reply("lead report"),
            ]
        )
        session = HarnessSession(
            client,
            "test",
            self.root,
            test_runner=runner,
            capabilities=Capabilities.full(memory_mode="off"),
        )
        self.addCleanup(lambda: session.wait_closed(5))
        self.addCleanup(session.close)
        session.run_user("Delegate read-only work")
        self.assertEqual(runner.runs, 0)

    def test_preapproved_subagent_executes_only_the_registered_recipe(self):
        runner = self.runner(roles=("lead", "subagent"))
        client, _ = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "delegate", name="task", arguments='{"prompt":"Run suite"}'
                        )
                    ]
                ),
                reply(
                    calls=[call("run", name="run_tests", arguments='{"name":"suite"}')]
                ),
                reply("child verified"),
                reply("lead report"),
            ]
        )
        session = HarnessSession(
            client,
            "test",
            self.root,
            test_runner=runner,
            capabilities=Capabilities.full(memory_mode="off"),
        )
        self.addCleanup(lambda: session.wait_closed(5))
        self.addCleanup(session.close)
        session.run_user("Delegate verification")
        self.assertEqual(runner.runs, 1)
        self.assertEqual(next(iter(runner._receipts.values()))["role"], "subagent")

    def test_preapproved_teammate_receipt_is_bound_to_its_task(self):
        runner = self.runner(roles=("lead", "teammate"))
        responses = iter(
            [
                reply(
                    calls=[call("run", name="run_tests", arguments='{"name":"suite"}')]
                ),
                reply("verified"),
            ]
        )
        client = type("Client", (), {})()
        from types import SimpleNamespace

        client.chat = SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kwargs: next(responses))
        )
        session = HarnessSession(
            client,
            "test",
            self.root,
            test_runner=runner,
            capabilities=Capabilities.full(memory_mode="off"),
        )
        self.addCleanup(lambda: session.wait_closed(5))
        self.addCleanup(session.close)
        task = session.team.store.create("Verify")
        arguments = dict(
            name="reader", role="reader", prompt="Run suite", task_id=task.id
        )
        session.team.authorize_spawn(arguments)
        session.team.spawn(**arguments)
        member = session.team.members["reader"]
        deadline = time.monotonic() + 3
        while member.status not in {"idle", "failed"} and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        self.assertEqual(member.status, "idle")
        record = next(iter(runner._receipts.values()))
        self.assertEqual(record["role"], "teammate")
        self.assertEqual(record["task_id"], task.id)
        self.assertEqual(record["status"], "passed")

    def test_cli_structured_goal_with_real_local_model_transport(self):
        requests = []

        class Service(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                requests.append(
                    json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                )
                data = json.dumps(
                    {
                        "id": "local",
                        "object": "chat.completion",
                        "created": 0,
                        "model": "test",
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "tool_calls",
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [
                                        call(
                                            "tests",
                                            name="run_tests",
                                            arguments='{"name":"unit-tests"}',
                                        )
                                    ],
                                },
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 10,
                            "completion_tokens": 5,
                            "total_tokens": 15,
                        },
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        (self.root / "tests").mkdir()
        (self.root / "tests/test_real.py").write_text(
            "import unittest\nclass Test(unittest.TestCase):\n def test_real(self): self.assertTrue(True)\n"
        )
        specification = self.root / "goal.json"
        specification.write_text(
            json.dumps(
                self.spec(
                    [{"id": "tests", "type": "tests_passed", "recipe": "unit-tests"}]
                )
            )
        )
        profile, mcp = self.root / "empty.env", self.root / "empty-mcp.json"
        profile.write_text("")
        mcp.write_text('{"servers":{}}')
        server = ThreadingHTTPServer(("127.0.0.1", 0), Service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        address = (
            "http://"
            + server.server_address[0]
            + ":"
            + str(server.server_address[1])
            + "/v1"
        )
        try:
            env = {
                **os.environ,
                "OPENAI_API_KEY": uuid.uuid4().hex,
                "OPENAI_BASE_URL": address,
                "OPENAI_MODEL": "test",
                "NO_PROXY": "127.0.0.1,localhost",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "homebrew_harness",
                    "--cwd",
                    str(self.root),
                    "--env-file",
                    str(profile),
                    "--mcp-config",
                    str(mcp),
                    "--memory-mode",
                    "off",
                    "--allow-tests",
                    "unit-tests",
                    "--goal-spec",
                    str(specification),
                ],
                capture_output=True,
                text=True,
                timeout=20,
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("[goal achieved]", result.stdout)
            self.assertEqual(len(requests), 1)
            receipts = list((self.root / ".harness/tests").glob("*/*/receipt.json"))
            self.assertEqual(len(receipts), 1)
            self.assertEqual(json.loads(receipts[0].read_text())["status"], "passed")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
