from tests.support.tools import fixture_invocation
"""Critical close, complete parsing and explicit component lifetime contracts."""

import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import homebrew_harness.cli

import homebrew_harness.configuration

import homebrew_harness.context.skill_loader

import homebrew_harness.runtime.assembly

import homebrew_harness.runtime.hooks

import homebrew_harness.runtime.prompts

import homebrew_harness.runtime.turns

import homebrew_harness.tools.dispatch

import homebrew_harness.tools.files

import homebrew_harness.tools.registry
from homebrew_harness.execution.background import BackgroundManager
from homebrew_harness.runtime.capabilities import Capabilities
from homebrew_harness.context.compaction import CompactionError
from homebrew_harness.orchestration.cron_scheduler import CronScheduler
from homebrew_harness.execution.execution_leases import ExecutionLeases
from homebrew_harness.orchestration.goal_loop import GoalController
from homebrew_harness.runtime.session import HarnessSession, HostCommand
from homebrew_harness.runtime.loop_components import LoopComponents
from homebrew_harness.context.memory import MemoryRuntime, MemoryRecord, MemoryError
from homebrew_harness.execution.model_gateway import current_scope
from homebrew_harness.runtime.runtime_control import CloseProgress, ClosePending
from homebrew_harness.context.task_intent import TaskIntent
from homebrew_harness.orchestration.teams import TeamManager
from homebrew_harness.execution.test_runner import TestRunner
from tests.support.clients import fake_client, reply, call
from homebrew_harness.orchestration.workspace_runtime import WorkspaceRuntime


class SimplificationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        for context in (redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)

    def session(self, responses=(), **kwargs):
        client, requests = fake_client(responses)
        session = HarnessSession(
            client, "fixture", self.root, shutdown_timeout=0.03, **kwargs
        )
        self.addCleanup(lambda: session.wait_closed(3))
        self.addCleanup(session.close)
        session.run_user("/usage")
        return session, client, requests

    def retryable(self, session):
        self.assertTrue(session._shutdown.workers.wait(2))
        self.assertTrue(session.shutdown_status()["retryable"])
        self.assertTrue(session.workspace.leases.held("session"))

    def test_stop_failure_is_pending_and_retries_only_incomplete_resource(self):
        session, _, _ = self.session()
        original = session.tests.close
        with (
            patch.object(
                session.tests, "close", side_effect=[OSError("fixture stop"), None]
            ) as stop,
            patch.object(session.shell, "close", wraps=session.shell.close) as shell,
        ):
            session.close()
            self.retryable(session)
            self.assertNotIn("tests", session.shutdown_status()["completed_steps"])
            self.assertIn("shell", session.shutdown_status()["completed_steps"])
            self.assertEqual(shell.call_count, 1)
            session.close()
            self.assertTrue(session.wait_closed(2))
            self.assertEqual(stop.call_count, 2)
            self.assertEqual(
                session.shutdown_status()["completed_steps"].count("shell"), 1
            )
        original()
        self.assertEqual(session.shutdown_status()["state"], "closed")

    def test_wait_failure_does_not_release_ownership(self):
        session, _, _ = self.session()
        with patch.object(session.tests, "wait_closed", return_value=False):
            session.close()
            self.retryable(session)
            self.assertEqual(session.shutdown_status()["state"], "pending")
            self.assertNotIn("checkpoint", session.shutdown_status()["completed_steps"])
        session.close()
        self.assertTrue(session.wait_closed(2))
        self.assertEqual(session.shutdown_status()["state"], "closed")

    def test_wait_without_explicit_completion_keeps_ownership(self):
        session, _, _ = self.session()
        with patch.object(session.tests, "wait_closed", return_value=None):
            session.close()
            self.retryable(session)
            self.assertNotIn("wait_tests", session.shutdown_status()["completed_steps"])
        session.close()
        self.assertTrue(session.wait_closed(2))

    def test_checkpoint_failure_retains_delivery_and_retries_before_release(self):
        session, _, _ = self.session()
        session.workspace.bus.send("peer", "lead", "fixture event")
        session._pending_team = session.workspace.bus.claim("lead")
        pending = list(session._pending_team)
        with patch.object(
            session, "_save_checkpoint", side_effect=OSError("fixture checkpoint")
        ):
            session.close()
            self.retryable(session)
            self.assertEqual(session._pending_team, pending)
            self.assertNotIn(
                "team_delivery", session.shutdown_status()["completed_steps"]
            )
        session.close()
        self.assertTrue(session.wait_closed(2))
        self.assertFalse(session._pending_team)
        self.assertTrue(session.workspace.bus.peek("lead"))
        self.assertTrue(
            session.runtime_store.get("session_meta", session.session_id)[
                "close_complete"
            ]
        )

    def test_delivery_failure_does_not_repeat_completed_checkpoint(self):
        session, _, _ = self.session()
        with patch.object(
            session, "_save_checkpoint", wraps=session._save_checkpoint
        ) as checkpoint:
            with patch.object(
                session.workspace.bus,
                "release",
                side_effect=OSError("fixture delivery"),
            ):
                session.close()
                self.retryable(session)
                self.assertEqual(checkpoint.call_count, 1)
            session.close()
            self.assertTrue(session.wait_closed(2))
            self.assertEqual(checkpoint.call_count, 1)

    def test_lifecycle_failure_keeps_lease_and_retries_proof_before_release(self):
        session, _, _ = self.session()
        with patch.object(
            session.runtime_store,
            "finish_session",
            side_effect=OSError("fixture lifecycle"),
        ):
            session.close()
            self.retryable(session)
            self.assertTrue(
                any(
                    e["component"] == "session_lifecycle"
                    for e in session.shutdown_status()["errors"]
                )
            )
        session.close()
        self.assertTrue(session.wait_closed(2))
        self.assertTrue(
            session.runtime_store.get("session_meta", session.session_id)[
                "close_complete"
            ]
        )
        self.assertEqual(session.shutdown_status()["state"], "closed")

    def test_goal_close_failure_is_pending_and_retries_goal_without_reclosing_tests(
        self,
    ):
        session, _, _ = self.session()
        session.goal.set_goal("fixture goal")
        with patch.object(session.tests, "close", wraps=session.tests.close) as tests:
            with patch.object(
                session.goal, "close", side_effect=OSError("fixture goal close")
            ):
                session.close()
                self.retryable(session)
                self.assertIsNotNone(session.goal.lease)
            session.close()
            self.assertTrue(session.wait_closed(2))
            self.assertEqual(tests.call_count, 1)
        self.assertIsNone(session.goal.lease)

    def test_trace_callback_failure_is_observation_and_does_not_block_close(self):
        session, _, _ = self.session()
        with patch.object(session.trace, "emit", side_effect=OSError("fixture trace")):
            session.close()
            self.assertTrue(session.wait_closed(2))
        status = session.shutdown_status()
        self.assertEqual(status["state"], "closed")
        self.assertTrue(status["persistence_ok"])
        self.assertFalse(status["errors"])
        self.assertGreater(status["observations"]["trace"]["dropped"], 0)

    def test_goal_notification_callback_failure_keeps_state_and_is_visible(self):
        observer = Mock(side_effect=ValueError("fixture observer"))
        goal = GoalController(self.root, Mock(), on_event=observer)
        session, _, _ = self.session(goal_controller=goal)
        session.goal.set_goal("fixture goal")
        self.assertTrue(session._goal_changed.is_set())
        self.assertEqual(session.goal.state["status"], "active")
        self.assertGreater(
            session.shutdown_status()["observations"]["goal_callback"]["dropped"], 0
        )
        session.close()
        self.assertTrue(session.wait_closed(2))
        self.assertEqual(session.shutdown_status()["state"], "closed")

    def test_trace_storage_failure_does_not_change_user_result_or_close_proof(self):
        session, _, _ = self.session([reply("done")])
        with patch.object(session.trace, "path", Path("../denied-trace.jsonl")):
            self.assertEqual(session.run_user("Read fixture"), "done")
        session.close()
        self.assertTrue(session.wait_closed(2))
        self.assertGreater(session.trace.status()["dropped"], 0)
        self.assertTrue(session.shutdown_status()["persistence_ok"])

    def test_close_progress_snapshot_stays_available_while_action_blocks(self):
        progress = CloseProgress()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def action():
            entered.set()
            release.wait(2)

        thread = threading.Thread(target=lambda: progress.step("fixture", action))
        thread.start()
        self.assertTrue(entered.wait(1))
        self.assertEqual(progress.snapshot()["completed_steps"], [])
        release.set()
        thread.join(2)
        self.assertEqual(progress.snapshot()["completed_steps"], ["fixture"])

    def test_close_progress_failed_step_is_required_until_success(self):
        progress = CloseProgress()
        self.assertFalse(progress.step("fixture", Mock(side_effect=OSError("fixture"))))
        with self.assertRaises(ClosePending):
            progress.require(["fixture"])
        self.assertTrue(progress.step("fixture", lambda: None))
        progress.require(["fixture"])
        self.assertFalse(progress.snapshot()["errors"])
        self.assertEqual(len(progress.snapshot()["history"]), 1)

    def test_failed_final_checkpoint_can_close_with_failed_proof_when_no_delivery_is_pending(
        self,
    ):
        session, _, _ = self.session()
        with patch.object(
            session, "_save_checkpoint", side_effect=OSError("fixture final checkpoint")
        ):
            session.close()
            self.assertTrue(session.wait_closed(2))
        status = session.shutdown_status()
        self.assertEqual(status["state"], "error")
        self.assertTrue(status["resources_closed"])
        self.assertFalse(status["persistence_ok"])
        self.assertNotIn("checkpoint", status["completed_steps"])
        self.assertIn("checkpoint", status["failed_steps"])
        self.assertFalse(
            session.runtime_store.get("session_meta", session.session_id)[
                "close_complete"
            ]
        )

    def test_recovery_parser_preserves_cron_identity_containing_a_local_minute(self):
        identity = "cron_fixture@2026-01-01 12:00"
        command = HostCommand.parse("/recovery inspect " + identity + " 3")
        self.assertEqual(command.values, (identity, 3))

    def test_cli_construction_failure_closes_external_test_runner(self):
        client, _ = fake_client([])
        runner = TestRunner(self.root)
        settings = {
            name: "fixture" for name in homebrew_harness.configuration.CONFIG_VARIABLES
        }
        with (
            patch.dict("os.environ", settings),
            patch("homebrew_harness.cli.load_test_runner", return_value=runner),
            patch("homebrew_harness.cli.OpenAI") as sdk,
            patch(
                "homebrew_harness.runtime.session.HarnessSession",
                side_effect=ValueError("fixture initialization"),
            ),
        ):
            sdk.return_value.__enter__.return_value = client
            self.assertEqual(
                homebrew_harness.cli.main(
                    [
                        "--profile",
                        "core",
                        "--cwd",
                        str(self.root),
                        "--env-file",
                        str(self.root / "absent.env"),
                        "/usage",
                    ]
                ),
                1,
            )
        self.assertTrue(runner.closed.is_set())
        client.chat.completions.create.assert_not_called()

    def test_cli_approval_failure_closes_test_runner_before_client_start(self):
        runner = TestRunner(self.root)
        settings = {
            name: "fixture" for name in homebrew_harness.configuration.CONFIG_VARIABLES
        }
        with (
            patch.dict("os.environ", settings),
            patch("homebrew_harness.cli.load_test_runner", return_value=runner),
            patch("homebrew_harness.cli.OpenAI") as sdk,
        ):
            self.assertEqual(
                homebrew_harness.cli.main(
                    [
                        "--profile",
                        "core",
                        "--cwd",
                        str(self.root),
                        "--env-file",
                        str(self.root / "absent.env"),
                        "--allow-tests",
                        "missing",
                        "/usage",
                    ]
                ),
                1,
            )
            sdk.assert_not_called()
        self.assertTrue(runner.closed.is_set())

    def test_memory_correction_parser_preserves_body_whitespace_and_newlines(self):
        body = "  first line\n\n    second line\n"
        command = HostCommand.parse("/memory correct fixture " + body)
        self.assertEqual(command.argument, "fixture")
        self.assertEqual(command.values, (body,))
        self.assertEqual(command.mode, "manage")

    def test_plain_and_task_text_keep_original_body(self):
        plain = "  Explain this\n    exactly.\n"
        self.assertEqual(HostCommand.parse(plain).query, plain)
        self.assertEqual(HostCommand.parse("/unknown " + plain).name, "")
        self.assertEqual(HostCommand.parse("/task " + plain).argument, plain.lstrip())

    def test_goal_spec_is_parsed_into_validated_structure(self):
        spec = {
            "condition": "fixture exists",
            "judge": False,
            "predicates": [
                {"id": "file", "type": "file_exists", "path": "fixture.txt"}
            ],
        }
        raw = json.dumps(spec)
        with patch(
            "homebrew_harness.runtime.host_commands.json.loads", wraps=json.loads
        ) as loads:
            command = HostCommand.parse("/goal spec " + raw)
        self.assertEqual(sum(item.args == (raw,) for item in loads.call_args_list), 1)
        self.assertEqual(command.action, "spec")
        self.assertEqual(command.values[0]["condition"], spec["condition"])
        self.assertFalse(command.local)

    def test_invalid_host_syntax_rejects_before_initializing_runtime(self):
        session, client, _ = self.session()
        before = session.intent.to_dict()
        invalid = [
            "/memory list extra",
            "/memory show",
            "/memory delete a b",
            "/memory correct fixture ",
            "/memory invalid",
            "/tests approve",
            "/tests revoke a b",
            "/recovery inspect a -1",
            "/recovery inspect a 1 extra",
            "/goal spec",
            "/goal spec {invalid",
            "/goal resume a b",
        ]
        with patch.object(
            session,
            "_ensure_runtime",
            side_effect=AssertionError("unexpected initialization"),
        ):
            for query in invalid:
                with self.subTest(query=query), self.assertRaises(ValueError):
                    session.run_user(query)
        self.assertEqual(session.intent.to_dict(), before)
        client.chat.completions.create.assert_not_called()

    def test_memory_read_and_update_commands_use_explicit_operations_without_model(
        self,
    ):
        client, _ = fake_client([])
        memory = MemoryRuntime(client, "fixture", self.root)
        record = MemoryRecord.validate(
            {
                "name": "fixture",
                "type": "user",
                "description": "fixture preference",
                "body": "Original preference",
                "evidence": "Original preference",
                "updated_at": "2026-01-01T00:00:00+00:00",
            }
        )
        memory.store.replace([record], memory.store.snapshot().version)
        session, worker, _ = self.session(memory_runtime=memory)
        self.assertEqual(
            json.loads(session.run_user("/memory list"))[0]["name"], "fixture"
        )
        self.assertEqual(
            json.loads(session.run_user("/memory show fixture"))["body"],
            "Original preference",
        )
        self.assertEqual(
            session.run_user("/memory correct fixture New\n  exact preference"),
            "Memory updated by the host.",
        )
        updated = json.loads(session.run_user("/memory show fixture"))
        self.assertEqual(updated["body"], "New\n  exact preference")
        self.assertEqual(updated["body"], updated["evidence"])
        self.assertEqual(
            session.run_user("/memory delete fixture"), "Memory updated by the host."
        )
        self.assertEqual(json.loads(session.run_user("/memory")), [])
        worker.chat.completions.create.assert_not_called()
        client.chat.completions.create.assert_not_called()

    def test_memory_existence_is_checked_at_execution(self):
        command = HostCommand.parse("/memory show missing")
        self.assertEqual(command.argument, "missing")
        client, _ = fake_client([])
        memory = MemoryRuntime(client, "fixture", self.root)
        with self.assertRaises(MemoryError):
            memory.show(command.argument)

    def test_default_user_components_are_resolved_before_recall(self):
        client, _ = fake_client([reply("done")])
        memory = SimpleNamespace(recall=Mock(return_value=""), finish_turn=Mock())
        self.assertEqual(
            homebrew_harness.runtime.turns.run_user_turn(
                client,
                "fixture",
                [],
                "Read fixture",
                self.root,
                components=LoopComponents(capabilities=Capabilities({"tasks"})),
                memory_runtime=memory,
            ),
            "done",
        )
        memory.recall.assert_called_once()
        names = {
            t["function"]["name"]
            for t in client.chat.completions.create.call_args.kwargs["tools"]
        }
        self.assertIn("create_task", names)

    def test_default_scheduled_components_restore_private_intent(self):
        scheduler = CronScheduler(self.root)
        scheduler.schedule("* * * * *", "Scheduled fixture", recurring=False)
        scheduler.poll_due(datetime(2026, 1, 1, 12, 0))
        job = scheduler.take_pending()
        client, _ = fake_client([reply("done")])
        contexts = []
        original = homebrew_harness.tools.registry.build_tool_pool

        def build(client, model, workdir, components):
            contexts.append(components)
            return original(client, model, workdir, components)

        with patch(
            "homebrew_harness.tools.registry.build_tool_pool", side_effect=build
        ):
            self.assertEqual(
                homebrew_harness.runtime.turns.run_scheduled_turn(
                    job,
                    client=client,
                    model="fixture",
                    messages=[],
                    workdir=self.root,
                    scheduler=scheduler,
                    components=LoopComponents(capabilities=Capabilities({"cron"})),
                ),
                "done",
            )
        self.assertEqual(contexts[0].compactor.intent.original, "")
        self.assertFalse(scheduler.list())

    def test_scheduled_preparation_failure_restores_original_delivery_once(self):
        scheduler = CronScheduler(self.root)
        scheduler.schedule("* * * * *", "Scheduled fixture", recurring=False)
        scheduler.poll_due(datetime(2026, 1, 1, 12, 0))
        job = scheduler.take_pending()
        client, _ = fake_client([])
        with (
            patch(
                "homebrew_harness.tools.registry.build_tool_pool",
                side_effect=ValueError("fixture preparation"),
            ),
            patch.object(scheduler, "restore", wraps=scheduler.restore) as restore,
        ):
            with self.assertRaises(ValueError):
                homebrew_harness.runtime.turns.run_scheduled_turn(
                    job,
                    client=client,
                    model="fixture",
                    messages=[],
                    workdir=self.root,
                    scheduler=scheduler,
                    components=LoopComponents(capabilities=Capabilities({"cron"})),
                )
            restore.assert_called_once_with(job)
        self.assertEqual(scheduler.take_pending().id, job.id)
        client.chat.completions.create.assert_not_called()

    def test_component_cancellation_event_reaches_gateway_request_scope(self):
        client, _ = fake_client([])
        event = threading.Event()
        observed = []

        def create(**kwargs):
            observed.append(event in current_scope().cancel_events)
            return reply("done")

        client.chat.completions.create.side_effect = create
        self.assertEqual(
            homebrew_harness.runtime.turns.run_user_turn(
                client,
                "fixture",
                [],
                "Read fixture",
                self.root,
                components=LoopComponents(stop_event=event),
            ),
            "done",
        )
        self.assertEqual(observed, [True])

    def test_prompt_builder_never_constructs_skills(self):
        skills = homebrew_harness.context.skill_loader.SkillLoader(self.root / "skills")
        with patch(
            "homebrew_harness.runtime.assembly.SkillLoader",
            side_effect=AssertionError("implicit skills"),
        ):
            prompt = homebrew_harness.runtime.prompts.build_system_prompt(
                self.root, skill_loader=skills
            )
        self.assertIn("Available skills", prompt)

    def test_tool_dispatch_never_constructs_hooks_or_context(self):
        (self.root / "fixture.txt").write_text("fixture")
        hooks = homebrew_harness.runtime.hooks.create_hooks()
        request = (
            reply(
                calls=[
                    call("read", name="read_file", arguments='{"path":"fixture.txt"}')
                ]
            )
            .choices[0]
            .message.tool_calls[0]
        )
        with (
            patch(
                "homebrew_harness.runtime.hooks.create_hooks",
                side_effect=AssertionError("implicit hooks"),
            ),
            patch(
                "homebrew_harness.runtime.assembly.SkillLoader",
                side_effect=AssertionError("implicit skills"),
            ),
            patch(
                "homebrew_harness.runtime.assembly.TodoManager",
                side_effect=AssertionError("implicit plan"),
            ),
        ):
            result = json.loads(
                homebrew_harness.tools.dispatch.execute_tool(
                    fixture_invocation(request),
                    self.root,
                    hooks=hooks,
                    handlers={"read_file": homebrew_harness.tools.files.run_read},
                )
            )
        self.assertEqual(result["content"], "fixture")

    def test_dispatch_requires_explicit_bindings(self):
        request = (
            reply(
                calls=[
                    call("read", name="read_file", arguments='{"path":"fixture.txt"}')
                ]
            )
            .choices[0]
            .message.tool_calls[0]
        )
        with self.assertRaises(TypeError):
            homebrew_harness.tools.dispatch.execute_tool(fixture_invocation(request), self.root)
        with self.assertRaises(TypeError):
            homebrew_harness.runtime.prompts.build_system_prompt(self.root)

    def test_session_and_turns_reuse_context_and_updated_plan(self):
        session, _, _ = self.session([reply("done")])
        components = session.loop_components()
        old_todo = components.todo_manager
        session.run_user("/task Read the new fixture")
        updated = session.loop_components()
        self.assertIs(updated.compactor, components.compactor)
        self.assertIs(updated.test_runner, components.test_runner)
        self.assertIs(updated.shell_runner, components.shell_runner)
        self.assertIsNot(updated.todo_manager, old_todo)
        self.assertIs(
            session.tool_pool().handlers["todo_write"].__self__, updated.todo_manager
        )

    def test_session_context_validation_failure_closes_only_created_resources(self):
        client, _ = fake_client([])
        created = {}

        def track(name, constructor):
            def create(*args, **kwargs):
                value = constructor(*args, **kwargs)
                value.close = Mock(wraps=value.close)
                created[name] = value
                return value

            return create

        with (
            patch(
                "homebrew_harness.runtime.assembly.TestRunner.default",
                side_effect=track("tests", TestRunner.default),
            ),
            patch(
                "homebrew_harness.execution.background.BackgroundManager",
                side_effect=track("background", BackgroundManager),
            ),
            patch(
                "homebrew_harness.runtime.session.WorkspaceRuntime",
                side_effect=track("workspace", WorkspaceRuntime),
            ),
        ):
            with self.assertRaises(CompactionError):
                HarnessSession(
                    client,
                    "fixture",
                    self.root,
                    context_chars=1000,
                    capabilities=Capabilities({"tasks", "background"}),
                )
        self.assertEqual(set(created), {"tests", "workspace", "background"})
        for value in created.values():
            value.close.assert_called_once()
        self.assertTrue(created["tests"].closed.is_set())
        self.assertTrue(created["workspace"].shell_runner._closed)
        self.assertTrue(created["background"]._closed)
        with ExecutionLeases(self.root).use("session"):
            pass

    def test_session_initialization_failure_restores_injected_host_bindings(self):
        client, _ = fake_client([])
        runner = Mock()
        team = TeamManager(self.root, runner, worktrees=False)
        self.addCleanup(team.close)
        previous = {
            name: getattr(team, name, None)
            for name in (
                "session_owner",
                "test_runner",
                "background_manager",
                "model_pending",
                "run_member",
            )
        }
        with patch.object(team, "close", wraps=team.close) as close:
            with self.assertRaises(CompactionError):
                HarnessSession(
                    client, "fixture", self.root, team_manager=team, context_chars=1000
                )
            close.assert_not_called()
        for name, value in previous.items():
            self.assertIs(getattr(team, name, None), value)
        self.assertFalse(team.shell_runner._closed)

    def test_invalid_turn_timeout_constructs_no_optional_resources(self):
        client, _ = fake_client([])
        with patch(
            "homebrew_harness.runtime.assembly.prepare_core",
            side_effect=AssertionError("unexpected preparation"),
        ):
            with self.assertRaises(ValueError):
                HarnessSession(client, "fixture", self.root, turn_timeout=0)


if __name__ == "__main__":
    unittest.main()
