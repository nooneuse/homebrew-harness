"""Goal completion, bounded Stop continuations and actual harness integration."""

from __future__ import annotations
from tests.support.tools import fixture_invocation
from tests.support.paths import PROJECT_ROOT
from homebrew_harness.runtime.capabilities import Capabilities

import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.support.tools import execute_fixture_tool
import homebrew_harness.runtime.errors
import homebrew_harness.runtime.hooks
import homebrew_harness.runtime.loop
import homebrew_harness.tools.files
from homebrew_harness.execution.background import BackgroundManager
from homebrew_harness.orchestration.goal_loop import (
    GoalController,
    GoalError,
    GoalEvaluation,
    PromptGoalEvaluator,
)
from homebrew_harness.runtime.session import HarnessSession
from homebrew_harness.security.permissions import PermissionDecision
from tests.support.clients import call, fake_client, reply
from homebrew_harness.orchestration.workflow_runtime import WorkflowRuntime
from homebrew_harness.orchestration.workflows import DEMO_CHANGES, DemoWorkflowRunner


class Evaluator:
    def __init__(self, action):
        self.action, self.calls = action, []

    def evaluate(self, packet):
        self.calls.append(copy.deepcopy(packet))
        return self.action(packet)


def final_ok(packet):
    return GoalEvaluation(
        True, "The requested answer is present.", evidence_ids=("final",)
    )


def tool_ok(packet):
    fresh = [
        item
        for item in packet["evidence"]
        if not item["historical"] and item["kind"] in {"tool", "background"}
    ]
    return (
        GoalEvaluation(
            True,
            "Concrete tool evidence satisfies the condition.",
            evidence_ids=(fresh[-1]["id"],),
        )
        if fresh
        else GoalEvaluation(False, "Provide actual verification results.")
    )


class GoalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for capture in (redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO())):
            capture.__enter__()
            self.addCleanup(capture.__exit__, None, None, None)

    def session(self, responses, action=tool_ok, **kwargs):
        evaluator = Evaluator(action)
        goal = GoalController(
            self.root, evaluator, block_cap=kwargs.get("stop_block_cap", 8)
        )
        client, requests = fake_client(responses)
        session = HarnessSession(
            client,
            "worker-model",
            self.root,
            goal_controller=goal,
            **kwargs,
            capabilities=Capabilities.full(memory_mode="off"),
        )
        self.addCleanup(lambda: session.wait_closed(5))
        self.addCleanup(session.close)
        return session, client, requests, evaluator

    def test_no_goal_and_status_commands_add_no_evaluator_requests(self):
        session, client, requests, evaluator = self.session([reply("ordinary answer")])
        self.assertEqual(json.loads(session.run_user("/goal"))["status"], "none")
        self.assertEqual(session.run_user("ordinary task"), "ordinary answer")
        self.assertEqual(evaluator.calls, [])
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertFalse((self.root / ".harness/goals").exists())

    def test_setting_goal_runs_immediately_and_unmet_result_continues_same_loop(self):
        (self.root / "result.txt").write_text("ready")
        session, client, requests, evaluator = self.session(
            [
                reply("I think it is done."),
                reply(
                    calls=[
                        call(
                            "read", name="read_file", arguments='{"path":"result.txt"}'
                        )
                    ]
                ),
                reply("Verified result.txt contains ready."),
            ]
        )
        prompts = Mock()
        session.hooks.register_hook("UserPromptSubmit", prompts)
        result = session.run_user("/goal result.txt contains ready and is read back")
        self.assertIn("[goal achieved]", result)
        self.assertEqual(session.goal.status()["status"], "achieved")
        self.assertEqual(len(evaluator.calls), 2)
        self.assertEqual(len(requests), 3)
        self.assertEqual(evaluator.calls[0]["evidence"], [])
        self.assertIn("ready", evaluator.calls[1]["evidence"][0]["body"])
        self.assertTrue(
            any(message.get("name") == "goal_feedback" for message in requests[1])
        )
        prompts.assert_called_once()

    def test_status_clear_and_prefix_do_not_create_goals(self):
        session, client, requests, evaluator = self.session(
            [reply("answer"), reply("prefix answer")], final_ok
        )
        session.run_user("/goal produce an answer")
        count = client.chat.completions.create.call_count
        self.assertEqual(json.loads(session.run_user("/goal"))["status"], "achieved")
        self.assertEqual(session.run_user("/goal off"), "Goal cleared.")
        self.assertEqual(client.chat.completions.create.call_count, count)
        self.assertEqual(session.run_user("/goals is ordinary text"), "prefix answer")
        self.assertEqual(len(evaluator.calls), 1)

    def test_invalid_condition_preserves_current_goal(self):
        goal = GoalController(self.root, Evaluator(final_ok))
        self.addCleanup(goal.close)
        goal.set_goal("valid condition")
        before = copy.deepcopy(goal.state)
        for condition in (None, " ", "x" * 4001):
            with self.assertRaises(GoalError):
                goal.set_goal(condition)
            self.assertEqual(goal.state, before)

    def test_invalid_command_does_not_suspend_a_valid_existing_goal(self):
        session, client, requests, evaluator = self.session([])
        session.goal.set_goal("valid condition")
        before = copy.deepcopy(session.goal.state)
        with self.assertRaises(GoalError):
            session.run_user("/goal " + "x" * 4001)
        self.assertEqual(session.goal.state, before)
        self.assertEqual(requests, [])

    def test_unfinished_goal_does_not_trigger_memory_extraction(self):
        memory = Mock()
        memory.mode = "auto"
        memory.recall.return_value = "[]"
        session, client, requests, evaluator = self.session(
            [reply("The condition conflicts")],
            lambda packet: GoalEvaluation(
                False, "Contradictory conditions.", True, ("condition",)
            ),
            memory_runtime=memory,
        )
        session.run_user("/goal satisfy contradictory conditions")
        memory.finish_turn.assert_not_called()

    def test_checker_failure_preserves_goal_and_does_not_retry(self):
        def failure(packet):
            raise RuntimeError("private-provider-marker")

        session, client, requests, evaluator = self.session(
            [reply("candidate")], failure
        )
        result = session.run_user("/goal verify the result")
        self.assertIn("[goal error]", result)
        self.assertNotIn("private-provider-marker", result)
        self.assertTrue(session.goal.pending)
        self.assertEqual(len(evaluator.calls), 1)
        self.assertEqual(len(requests), 1)
        session.team.bus.send("worker", "lead", "new observation", "result")
        self.assertTrue(session.process_once())
        self.assertEqual(len(evaluator.calls), 1)
        self.assertEqual(len(requests), 1)
        self.assertTrue(session.goal.state["evidence"])

    def test_invalid_or_uncited_completion_is_not_success(self):
        for judgement in (
            GoalEvaluation("yes", "invalid"),
            GoalEvaluation(True, "missing citation"),
            GoalEvaluation(True, "unknown", evidence_ids=("not-present",)),
            GoalEvaluation(True, "contradiction", True, ("final",)),
        ):
            goal = GoalController(
                self.root, Evaluator(lambda packet, judgement=judgement: judgement)
            )
            goal.set_goal("check a result")
            result = goal.evaluate_after_turn("candidate")
            self.assertEqual(result.action, "error")
            self.assertTrue(goal.pending)
            goal.close()

    def test_supported_impossibility_is_terminal_and_not_success(self):
        session, client, requests, evaluator = self.session(
            [reply("The constraints conflict")],
            lambda packet: GoalEvaluation(
                False,
                "The completion condition is contradictory.",
                True,
                ("condition",),
            ),
        )
        result = session.run_user("/goal produce both mutually exclusive outcomes")
        self.assertIn("[goal failed]", result)
        self.assertFalse(session.goal.pending)
        self.assertEqual(session.goal.status()["status"], "failed")

    def test_identical_failed_checks_are_cached_and_stop_cap_is_finite(self):
        session, client, requests, evaluator = self.session(
            [reply("unchanged")] * 4,
            lambda packet: GoalEvaluation(False, "Still missing evidence."),
            stop_block_cap=2,
        )
        result = session.run_user("/goal complete the missing verification")
        self.assertIn("[goal limit]", result)
        self.assertEqual(len(requests), 3)
        self.assertEqual(len(evaluator.calls), 1)
        self.assertEqual(session.goal.state["progress"]["cached_checks"], 2)
        self.assertTrue(session.goal.pending)

    def test_generic_stop_hooks_have_a_cap_without_an_active_goal(self):
        client, requests = fake_client([reply("candidate")] * 4)
        hooks = homebrew_harness.runtime.hooks.create_hooks()
        hooks.register_hook("Stop", lambda messages: "Continue")
        with self.assertRaisesRegex(
            homebrew_harness.runtime.errors.AgentError, "Stop hook continuation cap"
        ):
            homebrew_harness.runtime.loop.agent_loop(
                client,
                "worker",
                [],
                self.root,
                hooks=hooks,
                stop_block_cap=2,
                capabilities=Capabilities.full(memory_mode="off"),
            )
        self.assertEqual(len(requests), 3)

    def test_existing_stop_hook_runs_before_goal_judgement(self):
        session, client, requests, evaluator = self.session(
            [reply("first"), reply("second")], final_ok
        )
        calls = 0

        def stop(messages):
            nonlocal calls
            calls += 1
            return "Finish the report" if calls == 1 else None

        session.hooks.register_hook("Stop", stop)
        session.run_user("/goal provide the final answer")
        self.assertEqual(len(requests), 2)
        self.assertEqual(len(evaluator.calls), 1)
        self.assertEqual(evaluator.calls[0]["final_answer"]["content"], "second")

    def test_background_work_defers_judgement_and_host_events_resume_goal(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def command(*args, **kwargs):
            release.wait(3)
            return '{"exit_code":0,"stdout":"verified","stderr":""}'

        background = BackgroundManager(self.root, command)
        self.addCleanup(background.close)
        session, client, requests, evaluator = self.session(
            [
                reply(
                    calls=[
                        call(
                            "start",
                            name="bash",
                            arguments='{"command":"fixture","run_in_background":true}',
                        )
                    ]
                ),
                reply("Awaiting background verification"),
                reply("Background verified"),
            ],
            background_manager=background,
            poll_interval=0.01,
            dispatch_interval=0.01,
        )
        with patch(
            "homebrew_harness.security.permissions.ask_user",
            return_value=PermissionDecision("allow", "Exact fixture approval"),
        ):
            result = session.run_user(
                "/goal background verification exits with code zero"
            )
        self.assertIn("[goal defer]", result)
        self.assertEqual(evaluator.calls, [])
        session.start()
        release.set()
        self.assertIn("[goal achieved]", session.wait_for_goal(timeout=4))
        self.assertEqual(len(requests), 3)
        self.assertEqual(len(evaluator.calls), 1)
        self.assertTrue(
            any(
                record["kind"] == "background"
                for record in evaluator.calls[0]["evidence"]
            )
        )

    def test_working_and_plan_waiting_teammates_defer_until_work_finishes(self):
        (self.root / "team.txt").write_text("ready")
        session, client, requests, evaluator = self.session(
            [
                reply("Waiting for the teammate"),
                reply(
                    calls=[
                        call(
                            "verify", name="read_file", arguments='{"path":"team.txt"}'
                        )
                    ]
                ),
                reply("Verified teammate output"),
            ]
        )
        peer = SimpleNamespace(
            status="working",
            stop_event=threading.Event(),
            thread=None,
            store=Mock(),
            assignment=None,
        )
        session.team.members["peer"] = peer
        session.run_user("/goal teammate output is read and verified")
        self.assertEqual(evaluator.calls, [])
        peer.status = "waiting_approval"
        self.assertIn("team", session._goal_pending_work())
        peer.status = "idle"
        session.team.bus.send("peer", "lead", "Output ready; check it", "result")
        self.assertTrue(session.process_once())
        self.assertEqual(session.goal.state["status"], "achieved")
        self.assertEqual(len(evaluator.calls), 1)

    def test_global_worker_limit_is_shared_with_background_continuations(self):
        release = threading.Event()
        self.addCleanup(release.set)
        background = BackgroundManager(
            self.root, lambda *args, **kwargs: (release.wait(3), '{"exit_code":0}')[1]
        )
        self.addCleanup(background.close)
        session, client, requests, evaluator = self.session(
            [
                reply(
                    calls=[
                        call(
                            "start",
                            name="bash",
                            arguments='{"command":"fixture","run_in_background":true}',
                        )
                    ]
                ),
                reply("Wait"),
            ],
            background_manager=background,
            max_turns=2,
            poll_interval=0.01,
            dispatch_interval=0.01,
        )
        with patch(
            "homebrew_harness.security.permissions.ask_user",
            return_value=PermissionDecision("allow", "Fixture"),
        ):
            session.run_user("/goal verify background result")
        session.start()
        release.set()
        result = session.wait_for_goal(timeout=4)
        self.assertIn("[goal limit]", result)
        self.assertEqual(len(requests), 2)
        self.assertEqual(evaluator.calls, [])

    def test_standalone_loop_delivers_collected_background_evidence_to_goal(self):
        background = BackgroundManager(
            self.root, lambda *args, **kwargs: '{"exit_code":0,"stdout":"ready"}'
        )
        self.addCleanup(background.close)
        background.start("fixture")
        for thread in background._threads:
            thread.join(timeout=3)
        goal = GoalController(self.root, Evaluator(tool_ok))
        self.addCleanup(goal.close)
        goal.set_goal("background command exits successfully")
        client, requests = fake_client([reply("Background result observed")])
        result = homebrew_harness.runtime.loop.agent_loop(
            client,
            "worker",
            [],
            self.root,
            background_manager=background,
            goal_controller=goal,
            capabilities=Capabilities.full(memory_mode="off"),
        )
        self.assertIn("[goal achieved]", result)
        self.assertTrue(
            any(
                item["kind"] == "background"
                for item in goal.evaluator.calls[0]["evidence"]
            )
        )

    def test_cron_keeps_its_task_and_then_wakes_waiting_user_goal(self):
        session, client, requests, evaluator = self.session(
            [
                reply("Wait for cron"),
                reply(
                    calls=[
                        call(
                            "write",
                            name="write_file",
                            arguments='{"path":"cron.txt","content":"ready"}',
                        ),
                        call("read", name="read_file", arguments='{"path":"cron.txt"}'),
                    ]
                ),
                reply("Cron task complete"),
                reply("Goal verified"),
            ]
        )
        session.cron.schedule(
            "* * * * *", "Write and read back cron.txt", recurring=False
        )
        session.cron.poll_due(datetime(2026, 11, 3, 20))
        session.run_user("/goal cron.txt contains ready and has been read back")
        self.assertEqual(evaluator.calls, [])
        self.assertTrue(session.process_once())
        self.assertEqual(session.goal.state["status"], "waiting")
        self.assertEqual(evaluator.calls, [])
        self.assertFalse(
            any(message.get("name") == "goal_context" for message in requests[1])
        )
        self.assertTrue(session.process_once())
        self.assertEqual(session.goal.state["status"], "achieved")
        self.assertEqual(len(evaluator.calls), 1)
        self.assertEqual(session.goal.state["progress"]["worker_rounds"], 2)

    def test_workflow_completion_does_not_complete_goal_without_report_evidence(self):
        def evaluate(packet):
            proofs = [
                item
                for item in packet["evidence"]
                if '"name":"read_file"' in item["body"]
            ]
            return (
                GoalEvaluation(
                    True, "Report was read back.", evidence_ids=(proofs[-1]["id"],)
                )
                if proofs
                else GoalEvaluation(False, "Write and verify the report file.")
            )

        session, client, requests, evaluator = self.session(
            [
                reply(
                    calls=[
                        call(
                            "workflow",
                            name="workflow",
                            arguments=json.dumps(
                                {
                                    "name": "review-changes",
                                    "args": {"changes": DEMO_CHANGES},
                                }
                            ),
                        )
                    ]
                ),
                reply("Workflow complete"),
                reply(
                    calls=[
                        call(
                            "write",
                            name="write_file",
                            arguments='{"path":"report.txt","content":"reviewed"}',
                        ),
                        call(
                            "read", name="read_file", arguments='{"path":"report.txt"}'
                        ),
                    ]
                ),
                reply("Report verified"),
            ],
            evaluate,
            workflow_runtime=WorkflowRuntime(
                self.root,
                DemoWorkflowRunner(),
                registry=__import__(
                    "homebrew_harness.orchestration.workflows", fromlist=["*"]
                ).default_registry(),
            ),
        )
        session.run_user("/goal review the changes and write a verified report.txt")
        self.assertEqual(len(evaluator.calls), 2)
        self.assertEqual(session.goal.state["status"], "achieved")
        self.assertEqual((self.root / "report.txt").read_text(), "reviewed")

    def test_goal_evidence_survives_context_compaction(self):
        (self.root / "proof.txt").write_text("proof")
        session, client, requests, evaluator = self.session(
            [
                reply(
                    calls=[
                        call(
                            "read", name="read_file", arguments='{"path":"proof.txt"}'
                        ),
                        call("compact", name="compact", arguments="{}"),
                    ]
                ),
                reply("Archived work summary"),
                reply("Verified"),
            ]
        )
        session.run_user("/goal proof.txt is read and verified")
        self.assertTrue(
            any(
                '"name":"read_file"' in item["body"]
                for item in evaluator.calls[0]["evidence"]
            )
        )
        self.assertEqual(session.goal.state["progress"]["worker_rounds"], 2)
        self.assertEqual(len(requests), 3)
        self.assertTrue(
            any(message.get("name") == "goal_context" for message in requests[-1])
        )

    def test_restart_does_not_auto_activate_goal_and_old_evidence_cannot_prove_completion(
        self,
    ):
        (self.root / "proof.txt").write_text("proof")
        first, client, requests, evaluator = self.session(
            [
                reply(
                    calls=[
                        call("read", name="read_file", arguments='{"path":"proof.txt"}')
                    ]
                ),
                reply("candidate"),
            ],
            lambda packet: (_ for _ in ()).throw(RuntimeError("outage")),
        )
        first.run_user("/goal verify proof.txt")
        goal_id = first.goal.state["goal_id"]
        first.close()
        second, client, requests, evaluator = self.session(
            [reply("old proof exists")],
            lambda packet: GoalEvaluation(
                True, "Old results", evidence_ids=(packet["evidence"][0]["id"],)
            ),
        )
        self.assertIsNone(second.goal.state)
        result = second.run_user("/goal resume " + goal_id)
        self.assertIn("[goal error]", result)
        self.assertTrue(evaluator.calls[0]["evidence"][0]["historical"])
        self.assertTrue(second.goal.pending)

    def test_another_session_cannot_resume_an_owned_goal(self):
        owner = GoalController(self.root, Evaluator(final_ok))
        self.addCleanup(owner.close)
        owner.set_goal("active task")
        other = GoalController(self.root, Evaluator(final_ok))
        with self.assertRaisesRegex(GoalError, "active"):
            other.restore(owner.state["goal_id"])
        code = "from homebrew_harness.orchestration.goal_loop import GoalStore; import sys\nGoalStore(sys.argv[1]).claim(sys.argv[2])"
        child = subprocess.run(
            [sys.executable, "-c", code, str(self.root), owner.state["goal_id"]],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertNotEqual(child.returncode, 0)
        self.assertIn("active", child.stderr)

    def test_evidence_files_are_private_and_checker_input_is_bounded(self):
        goal = GoalController(self.root, Evaluator(tool_ok), input_chars=3000)
        self.addCleanup(goal.close)
        goal.set_goal("verify output")
        for index in range(5):
            goal.observe_notification(
                "background",
                {"bg_id": str(index), "result": {"exit_code": 0, "stdout": "x" * 5000}},
            )
        packet = goal.packet("answer " * 1000)
        self.assertLessEqual(
            len(
                json.dumps(
                    packet, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
            ),
            3000,
        )
        reference = goal.state["evidence"][-1]["archive"]
        self.assertEqual(os.stat(self.root / reference).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.root / ".harness/goals").st_mode & 0o777, 0o700)
        self.assertTrue(goal.owns_archive(reference))

    def test_own_evidence_can_be_read_after_task_directory_changes(self):
        goal = GoalController(self.root, Evaluator(tool_ok))
        self.addCleanup(goal.close)
        goal.set_goal("verify task data")
        goal.observe_notification(
            "background", {"bg_id": "fixture", "result": {"exit_code": 0}}
        )
        reference = goal.state["evidence"][0]["archive"]
        directory = self.root / "task-dir"
        directory.mkdir()
        team = SimpleNamespace(lead_cwd=lambda: directory, lead_assignment=None)
        tool = (
            reply(
                calls=[
                    call(
                        "read",
                        name="read_file",
                        arguments=json.dumps({"path": reference}),
                    )
                ]
            )
            .choices[0]
            .message.tool_calls[0]
        )
        result = json.loads(
            execute_fixture_tool(
                tool,
                self.root,
                team_manager=team,
                goal_controller=goal,
                handlers={"read_file": homebrew_harness.tools.files.run_read},
            )
        )
        self.assertIn("exit_code", result["content"])
        self.assertEqual(goal.state["evidence"][-1]["kind"], "evidence_read")
        goal.state["evidence"][0]["historical"] = True
        goal.observe_tool(fixture_invocation(tool), self.root, json.dumps({"content": "saved evidence"}))
        self.assertEqual(goal.state["evidence"][-1]["kind"], "archive_read")
        finder = (
            reply(
                calls=[
                    call(
                        "glob",
                        name="glob",
                        arguments=json.dumps(
                            {
                                "pattern": ".harness/goals/"
                                + goal.state["goal_id"]
                                + "/evidence/*.json"
                            }
                        ),
                    )
                ]
            )
            .choices[0]
            .message.tool_calls[0]
        )
        result = json.loads(
            execute_fixture_tool(
                finder,
                self.root,
                team_manager=team,
                goal_controller=goal,
                handlers={"glob": homebrew_harness.tools.files.run_glob},
            )
        )
        self.assertIn(reference, result["paths"])

    def test_evidence_capture_failure_preserves_executed_result_and_stops_more_calls(
        self,
    ):
        session, client, requests, evaluator = self.session(
            [
                reply(
                    calls=[
                        call(
                            "write",
                            name="write_file",
                            arguments='{"path":"done.txt","content":"saved"}',
                        )
                    ]
                ),
            ]
        )
        with patch.object(
            session.goal.store, "archive", side_effect=OSError("private-detail")
        ):
            result = session.run_user("/goal create and verify done.txt")
        self.assertEqual((self.root / "done.txt").read_text(), "saved")
        self.assertTrue(
            any(message.get("role") == "tool" for message in session.messages)
        )
        self.assertIn("[goal error]", result)
        self.assertNotIn("private-detail", result)
        self.assertEqual(len(requests), 1)
        self.assertEqual(evaluator.calls, [])

    def test_permission_denial_is_not_bypassed_by_goal_continuation(self):
        session, client, requests, evaluator = self.session(
            [
                reply(calls=[call("denied", command="sudo true")]),
                reply("Denied"),
                reply("Still denied"),
            ],
            lambda packet: GoalEvaluation(False, "Action is unavailable."),
            stop_block_cap=1,
        )
        with patch("homebrew_harness.security.permissions.ask_user") as approval:
            result = session.run_user("/goal run the requested verification")
        approval.assert_not_called()
        self.assertIn("[goal limit]", result)
        self.assertTrue(
            any(
                item.get("metadata", {}).get("has_error")
                for item in evaluator.calls[0]["evidence"]
            )
        )

    def test_children_do_not_inherit_parent_goal_gate(self):
        goal = GoalController(self.root, Evaluator(final_ok))
        self.addCleanup(goal.close)
        goal.set_goal("parent-only target")
        client, requests = fake_client([reply("child answer")])
        result = homebrew_harness.runtime.loop.agent_loop(
            client,
            "worker",
            [],
            self.root,
            subagent=True,
            goal_controller=goal,
            goal_observer=goal,
            capabilities=Capabilities.full(memory_mode="off"),
        )
        self.assertEqual(result, "child answer")
        self.assertEqual(goal.evaluator.calls, [])
        self.assertEqual(goal.state["progress"]["worker_rounds"], 0)

    def test_native_checker_disables_sdk_retries_and_truncation_preserves_usage(self):
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="length",
                    message=SimpleNamespace(tool_calls=None, content=""),
                )
            ],
            usage=SimpleNamespace(total_tokens=1024),
        )
        api = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=Mock(return_value=response))
            )
        )
        client = SimpleNamespace(with_options=Mock(return_value=api))
        evaluator = PromptGoalEvaluator(client, "checker", max_tokens=1024)
        client.with_options.assert_called_once_with(max_retries=0, timeout=20)
        goal = GoalController(self.root, evaluator)
        self.addCleanup(goal.close)
        goal.set_goal("check the answer")
        decision = goal.evaluate_after_turn("candidate")
        self.assertEqual(decision.action, "error")
        self.assertEqual(evaluator.client.summary()["known_tokens"], 1024)
        request = api.chat.completions.create.call_args.kwargs
        self.assertNotIn("tools", request)
        self.assertEqual(len(request["messages"]), 2)
        self.assertEqual(api.chat.completions.create.call_count, 1)

    def test_openai_sdk_checker_works_against_local_service(self):
        from openai import OpenAI

        requests = []

        class Service(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                requests.append(
                    json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                )
                content = json.dumps(
                    {
                        "ok": True,
                        "impossible": False,
                        "reason": "The requested answer is present.",
                        "evidence_ids": ["final"],
                    }
                )
                data = json.dumps(
                    {
                        "id": "local-goal",
                        "object": "chat.completion",
                        "created": 0,
                        "model": "checker",
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "stop",
                                "message": {"role": "assistant", "content": content},
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 20,
                            "completion_tokens": 5,
                            "total_tokens": 25,
                        },
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            address = (
                "http://"
                + server.server_address[0]
                + ":"
                + str(server.server_address[1])
                + "/v1"
            )
            with OpenAI(
                api_key=uuid.uuid4().hex, base_url=address, timeout=3, max_retries=0
            ) as client:
                goal = GoalController(self.root, PromptGoalEvaluator(client, "checker"))
                goal.set_goal("answer hello")
                self.assertEqual(goal.evaluate_after_turn("hello").action, "achieved")
                self.assertEqual(goal.evaluator.client.summary()["known_tokens"], 25)
                goal.close()
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]["response_format"], {"type": "json_object"})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_cli_goal_runs_worker_and_checker_with_separate_local_api_calls(self):
        requests = []

        class Service(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                request = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                requests.append(request)
                model = request["model"]
                if model == "checker":
                    packet = json.loads(request["messages"][-1]["content"])
                    result = {
                        "ok": True,
                        "impossible": False,
                        "reason": "The requested file was written and read back.",
                        "evidence_ids": [packet["evidence"][-1]["id"]],
                    }
                    message, finish = (
                        {"role": "assistant", "content": json.dumps(result)},
                        "stop",
                    )
                elif sum(item["model"] == "worker" for item in requests) == 1:
                    message = {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            call(
                                "write",
                                name="write_file",
                                arguments='{"path":"result.txt","content":"ready"}',
                            ),
                            call(
                                "read",
                                name="read_file",
                                arguments='{"path":"result.txt"}',
                            ),
                        ],
                    }
                    finish = "tool_calls"
                else:
                    message, finish = (
                        {"role": "assistant", "content": "result.txt verified"},
                        "stop",
                    )
                data = json.dumps(
                    {
                        "id": "local-cli",
                        "object": "chat.completion",
                        "created": 0,
                        "model": model,
                        "choices": [
                            {"index": 0, "message": message, "finish_reason": finish}
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

        server = ThreadingHTTPServer(("127.0.0.1", 0), Service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            address = (
                "http://"
                + server.server_address[0]
                + ":"
                + str(server.server_address[1])
                + "/v1"
            )
            profile, mcp = self.root / "empty.env", self.root / "empty-mcp.json"
            profile.write_text("")
            mcp.write_text('{"servers":{}}')
            env = {
                **os.environ,
                "OPENAI_API_KEY": uuid.uuid4().hex,
                "OPENAI_BASE_URL": address,
                "OPENAI_MODEL": "worker",
                "GOAL_EVALUATOR_MODEL": "checker",
                "NO_PROXY": "127.0.0.1,localhost",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            process = subprocess.run(
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
                    "/goal write result.txt containing ready and read it back",
                ],
                capture_output=True,
                text=True,
                timeout=15,
                env=env,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertIn("[goal achieved]", process.stdout)
            self.assertEqual((self.root / "result.txt").read_text(), "ready")
            self.assertEqual(
                [request["model"] for request in requests],
                ["worker", "worker", "checker"],
            )
            self.assertNotIn("tools", requests[-1])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
