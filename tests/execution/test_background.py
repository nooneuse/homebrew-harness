"""Nonblocking dispatch, single-result protocol, isolation and process lifetime."""

from __future__ import annotations
from tests.support.paths import PROJECT_ROOT
from homebrew_harness.runtime.capabilities import Capabilities

import io
import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from functools import partial
from pathlib import Path
from unittest.mock import Mock, patch

from tests.support.tools import execute_fixture_tool
import homebrew_harness.runtime.assembly
import homebrew_harness.runtime.errors
import homebrew_harness.runtime.hooks
import homebrew_harness.runtime.loop
import homebrew_harness.tools.registry
import homebrew_harness.tools.shell
from homebrew_harness.execution.background import (
    BackgroundManager,
    ShellRunner,
    should_run_background,
)
from homebrew_harness.security.permissions import PermissionDecision
from tests.support.clients import call, fake_client, reply
from homebrew_harness.context.todo import TodoManager


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workdir = Path(temporary.name).resolve()
        self.stderr = io.StringIO()
        capture = redirect_stderr(self.stderr)
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)
        self.release, self.started, self.finished = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        self.addCleanup(self.release.set)

    def manager(self, runner=None, **kwargs):
        manager = BackgroundManager(
            self.workdir, runner or homebrew_harness.tools.shell.run_bash, **kwargs
        )
        self.addCleanup(manager.close)
        return manager

    def controlled_runner(self, command, **kwargs):
        self.started.set()
        self.release.wait(timeout=3)
        self.finished.set()
        return json.dumps({"exit_code": 0, "stdout": command, "stderr": ""})

    def invoke(self, name, arguments, manager, hooks=None):
        block = (
            reply(
                calls=[
                    call("original-call", name=name, arguments=json.dumps(arguments))
                ]
            )
            .choices[0]
            .message.tool_calls[0]
        )
        return execute_fixture_tool(
            block, self.workdir, hooks=hooks, background_manager=manager
        )

    def finish_worker(self, manager):
        self.release.set()
        for thread in manager._threads:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())

    def wait_path(self, path):
        deadline = time.monotonic() + 3
        while not path.exists() and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        self.assertTrue(path.exists(), "Worker did not produce its startup marker")

    def assert_process_stopped(self, pid):
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            # Linux can retain a killed orphan as a zombie awaiting reaping.
            stat = Path(f"/proc/{pid}/stat")
            if stat.exists() and stat.read_text().split(") ", 1)[1].startswith("Z"):
                return
            threading.Event().wait(0.01)
        self.fail("Child process remained alive after session cleanup")

    def test_background_is_selected_only_by_literal_true_for_bash(self):
        for name, value in (
            ("bash", False),
            ("bash", "true"),
            ("bash", 1),
            ("read_file", True),
        ):
            self.assertFalse(should_run_background(name, {"run_in_background": value}))
        self.assertTrue(should_run_background("bash", {"run_in_background": True}))
        manager = self.manager()
        for value in ("true", 1, None, [], {}):
            with (
                self.subTest(value=value),
                patch("homebrew_harness.security.permissions.ask_user") as ask,
            ):
                output = json.loads(
                    self.invoke(
                        "bash", {"command": "true", "run_in_background": value}, manager
                    )
                )
                self.assertIn("error", output)
                ask.assert_not_called()
        self.assertEqual(manager.collect(), [])

    def test_start_returns_while_worker_runs_and_next_tool_can_finish(self):
        manager = self.manager(self.controlled_runner)
        hooks = homebrew_harness.runtime.hooks.create_hooks()
        events = []
        hooks.register_hook(
            "PostToolUse", lambda event, output: events.append((event.name, output))
        )
        with patch(
            "homebrew_harness.security.permissions.ask_user",
            return_value=PermissionDecision("allow", "Test approval"),
        ) as ask:
            started = json.loads(
                self.invoke(
                    "bash",
                    {"command": "independent-command", "run_in_background": True},
                    manager,
                    hooks,
                )
            )
        self.assertTrue(self.started.wait(timeout=1))
        self.assertFalse(self.finished.is_set())
        result = json.loads(
            self.invoke(
                "write_file",
                {"path": "foreground.txt", "content": "ready"},
                manager,
                hooks,
            )
        )
        self.assertEqual(result["path"], "foreground.txt")
        self.assertFalse(self.finished.is_set())
        self.assertEqual(started["status"], "running")
        self.assertEqual(len(events), 2)
        ask.assert_called_once()
        self.finish_worker(manager)
        notifications = manager.collect()
        self.assertEqual(notifications[0]["bg_id"], started["bg_id"])
        self.assertEqual(notifications[0]["result"]["stdout"], "independent-command")
        self.assertEqual(len(events), 2)  # Completion is not another PostToolUse.

    def test_denied_hard_forbidden_and_no_terminal_commands_never_start_workers(self):
        for command, approval in (
            ("true", "declined"),
            ("sudo true", "forbidden"),
            ("true", "no-terminal"),
        ):
            manager = self.manager(Mock())
            if approval == "declined":
                context = patch(
                    "homebrew_harness.security.permissions.ask_user",
                    return_value=PermissionDecision("deny", "Declined"),
                )
            else:
                context = patch(
                    "homebrew_harness.security.permissions.sys.stdin.isatty",
                    return_value=False,
                )
            with (
                self.subTest(approval=approval),
                context,
                patch.object(manager, "start") as start,
            ):
                result = json.loads(
                    self.invoke(
                        "bash", {"command": command, "run_in_background": True}, manager
                    )
                )
                self.assertEqual(result["permission"], "deny")
                start.assert_not_called()

    def test_terminal_approval_displays_flag_and_stays_on_main_thread(self):
        manager = self.manager(self.controlled_runner)
        seen = []

        def approve(*args):
            seen.append(threading.current_thread())
            return "yes"

        with (
            patch(
                "homebrew_harness.security.permissions.sys.stdin.isatty",
                return_value=True,
            ),
            patch("builtins.input", side_effect=approve),
        ):
            result = json.loads(
                self.invoke(
                    "bash", {"command": "approved", "run_in_background": True}, manager
                )
            )
        self.assertEqual(seen, [threading.main_thread()])
        self.assertIn('"run_in_background": true', self.stderr.getvalue())
        self.assertEqual(result["status"], "running")
        self.finish_worker(manager)

    def test_false_and_omitted_flags_keep_synchronous_handler_contract(self):
        manager = self.manager(Mock())
        handler = Mock(return_value='{"exit_code":0}')
        with (
            patch.dict(
                homebrew_harness.tools.registry.TOOL_HANDLERS, {"bash": handler}
            ),
            patch(
                "homebrew_harness.security.permissions.ask_user",
                return_value=PermissionDecision("allow", "Test approval"),
            ),
        ):
            for arguments in (
                {"command": "sync"},
                {"command": "sync", "run_in_background": False},
            ):
                self.assertEqual(
                    json.loads(self.invoke("bash", arguments, manager))["exit_code"], 0
                )
        self.assertEqual(handler.call_count, 2)
        self.assertEqual(
            handler.call_args.kwargs, {"command": "sync", "workdir": self.workdir}
        )
        manager.run_command.assert_not_called()

    def test_completion_queue_is_once_only_and_query_results_remain_available(self):
        manager = self.manager(self.controlled_runner)
        task = manager.start("output")
        running = json.loads(manager.check(task["bg_id"], workdir=self.workdir))
        self.assertEqual(running["status"], "running")
        self.assertIsNone(running["result"])
        self.finish_worker(manager)
        first = manager.collect()
        self.assertEqual(manager.collect(), [])
        first[0]["result"]["stdout"] = "changed snapshot"
        stored = json.loads(manager.check(task["bg_id"], workdir=self.workdir))
        self.assertEqual(stored["result"]["stdout"], "output")
        self.assertEqual(stored["status"], "completed")

    def test_bad_ids_and_other_sessions_do_not_expose_results(self):
        manager = self.manager(self.controlled_runner)
        task = manager.start("private-result")
        other = self.manager(Mock())
        for task_id in ("../file", "bg_bad", task["bg_id"]):
            with self.subTest(task_id=task_id), self.assertRaises(ValueError):
                other.check(task_id, workdir=self.workdir)
        with self.assertRaises(ValueError):
            manager.check(task["bg_id"], workdir=self.workdir.parent)
        self.finish_worker(manager)

    def test_nonzero_timeout_spawn_error_and_worker_exception_become_failed(self):
        runners = [
            partial(homebrew_harness.tools.shell.run_bash, timeout=1),
            partial(homebrew_harness.tools.shell.run_bash, timeout=0.03),
            Mock(side_effect=RuntimeError("private-connection-detail")),
            lambda *args, **kwargs: '{"error":"Cannot start Bash"}',
        ]
        commands = [
            "printf failed >&2; exit 7",
            "while :; do :; done",
            "unused",
            "unused",
        ]
        for runner, command in zip(runners, commands):
            with self.subTest(command=command):
                manager = self.manager(runner)
                task = manager.start(command)
                for thread in manager._threads:
                    thread.join(timeout=3)
                result = json.loads(manager.check(task["bg_id"], workdir=self.workdir))
                self.assertEqual(result["status"], "failed")
                self.assertNotIn("private-connection-detail", json.dumps(result))
                if command.startswith("printf"):
                    self.assertEqual(result["result"]["exit_code"], 7)

    def test_background_output_is_bounded_archived_and_private_env_is_removed(self):
        manager = self.manager()
        with patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "test-secret", "DASHSCOPE_API_KEY": "test-secret"},
        ):
            task = manager.start(
                "printf '%12000s' x; printf '%s%s' \"$OPENAI_API_KEY\" \"$DASHSCOPE_API_KEY\" >&2"
            )
            for thread in manager._threads:
                thread.join(timeout=3)
        result = json.loads(manager.check(task["bg_id"], workdir=self.workdir))[
            "result"
        ]
        self.assertLess(len(result["stdout"]), 11000)
        self.assertEqual(len((self.workdir / result["stdout_path"]).read_text()), 12000)
        self.assertEqual(result["stderr"], "")

    def test_thread_start_failure_rolls_back_registration(self):
        manager = self.manager(Mock())
        with patch(
            "homebrew_harness.execution.background.threading.Thread.start",
            side_effect=RuntimeError("Cannot start"),
        ):
            with self.assertRaises(RuntimeError):
                manager.start("unused")
        self.assertEqual(manager._tasks, {})
        self.assertFalse(manager._threads)
        manager.close()

    def test_capacity_rejects_new_work_and_close_cancels_queued_workers(self):
        manager = self.manager(self.controlled_runner, max_running=1)
        first = manager.start("first")
        with self.assertRaisesRegex(ValueError, "Too many"):
            manager.start("second")
        manager.close()
        self.finish_worker(manager)
        self.assertEqual(
            json.loads(manager.check(first["bg_id"], workdir=self.workdir))["status"],
            "cancelled",
        )
        self.assertEqual(len(manager.collect()), 1)
        self.assertEqual(manager.collect(), [])
        with self.assertRaisesRegex(ValueError, "closed"):
            manager.start("after close")

    def test_close_and_timeout_stop_shell_child_process_groups(self):
        for cancel in (True, False):
            with self.subTest(cancel=cancel):
                pid_path = self.workdir / ("cancel.pid" if cancel else "timeout.pid")
                manager = self.manager(
                    partial(
                        homebrew_harness.tools.shell.run_bash,
                        timeout=5 if cancel else 0.2,
                    )
                )
                task = manager.start(f"sleep 30 & echo $! > {pid_path.name}; wait")
                self.wait_path(pid_path)
                if cancel:
                    manager.close()
                for thread in manager._threads:
                    thread.join(timeout=3)
                self.assert_process_stopped(int(pid_path.read_text()))
                state = json.loads(manager.check(task["bg_id"], workdir=self.workdir))[
                    "status"
                ]
                self.assertEqual(state, "cancelled" if cancel else "failed")

    def test_closed_runner_cannot_spawn_late_processes(self):
        runner = ShellRunner()
        runner.close()
        with (
            patch("homebrew_harness.execution.background.subprocess.Popen") as spawn,
            self.assertRaises(OSError),
        ):
            runner.run("unused", self.workdir, {}, 1)
        spawn.assert_not_called()

    def test_successful_shell_also_cleans_up_leftover_group_members(self):
        manager = self.manager()
        task = manager.start(
            "sleep 30 >/dev/null 2>&1 & echo $! > leftover.pid; printf done"
        )
        self.wait_path(self.workdir / "leftover.pid")
        for thread in manager._threads:
            thread.join(timeout=3)
        self.assert_process_stopped(int((self.workdir / "leftover.pid").read_text()))
        result = json.loads(manager.check(task["bg_id"], workdir=self.workdir))
        self.assertEqual(result["status"], "completed")

    def test_cli_termination_guard_cleans_up_children_on_sigterm(self):
        script = """
import signal, sys, time
from pathlib import Path
from homebrew_harness.tools.shell import run_bash
from homebrew_harness.execution.background import BackgroundManager, termination_guard
root = Path(sys.argv[1])
with termination_guard(), BackgroundManager(root, run_bash) as manager:
    manager.start('sleep 30 & echo $! > signal.pid; wait')
    while not (root / 'signal.pid').exists():
        time.sleep(0.01)
    print('ready', flush=True)
    signal.pause()
"""
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(self.workdir)],
            cwd=PROJECT_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        try:
            self.assertTrue(select.select([process.stdout], [], [], 5)[0])
            self.assertEqual(process.stdout.readline().strip(), "ready")
            process.send_signal(signal.SIGTERM)
            process.communicate(timeout=5)
            self.assertEqual(process.returncode, 143)
            self.assert_process_stopped(int((self.workdir / "signal.pid").read_text()))
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=3)

    def test_notification_is_after_closed_batch_and_does_not_repeat_tool_result(self):
        manager = self.manager(self.controlled_runner)
        client, snapshots = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "start",
                            name="bash",
                            arguments='{"command":"background-output","run_in_background":true}',
                        ),
                        call(
                            "foreground",
                            name="write_file",
                            arguments='{"path":"front.txt","content":"ready"}',
                        ),
                    ]
                ),
                reply("Work is still running."),
                reply("Background succeeded."),
            ]
        )
        original_create = client.chat.completions.create.side_effect
        count = 0

        def create(**kwargs):
            nonlocal count
            count += 1
            if count == 2:
                self.assertTrue((self.workdir / "front.txt").exists())
                self.assertFalse(self.finished.is_set())
                self.finish_worker(manager)
            return original_create(**kwargs)

        client.chat.completions.create.side_effect = create
        history = [{"role": "user", "content": "Start slow work and edit a file."}]
        with patch(
            "homebrew_harness.security.permissions.ask_user",
            return_value=PermissionDecision("allow", "Test approval"),
        ):
            result = homebrew_harness.runtime.loop.agent_loop(
                client,
                "test-model",
                history,
                self.workdir,
                background_manager=manager,
                capabilities=Capabilities.full(memory_mode="off"),
            )
        self.assertEqual(result, "Background succeeded.")
        results = [message for message in history if message["role"] == "tool"]
        self.assertEqual(
            [message["tool_call_id"] for message in results], ["start", "foreground"]
        )
        notifications = [
            message
            for message in history
            if message.get("name") == "background_notification"
        ]
        self.assertEqual(len(notifications), 1)
        self.assertNotIn("tool_call_id", notifications[0])
        self.assertIn('"status": "completed"', notifications[0]["content"])
        self.assertEqual(manager.collect(), [])
        self.assertEqual(len(snapshots), 3)

    def test_completion_before_next_model_call_is_injected_once(self):
        manager = self.manager(self.controlled_runner)
        task = manager.start("already done")
        self.finish_worker(manager)
        client, snapshots = fake_client([reply("Observed completion.")])
        history = [{"role": "user", "content": "Report background status."}]
        homebrew_harness.runtime.loop.agent_loop(
            client,
            "test-model",
            history,
            self.workdir,
            background_manager=manager,
            capabilities=Capabilities.full(memory_mode="off"),
        )
        self.assertEqual(snapshots[0][-1]["name"], "background_notification")
        self.assertIn(task["bg_id"], snapshots[0][-1]["content"])
        homebrew_harness.runtime.loop.inject_background_results(history, manager)
        self.assertEqual(
            sum(m.get("name") == "background_notification" for m in history), 1
        )

    def test_shared_manager_survives_between_user_turns_without_model_wakeup(self):
        manager = self.manager(self.controlled_runner)
        client, _ = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "start",
                            name="bash",
                            arguments='{"command":"result","run_in_background":true}',
                        )
                    ]
                ),
                reply("Started; still running."),
            ]
        )
        history = [{"role": "user", "content": "Start only."}]
        with patch(
            "homebrew_harness.security.permissions.ask_user",
            return_value=PermissionDecision("allow", "Test approval"),
        ):
            homebrew_harness.runtime.loop.agent_loop(
                client,
                "test-model",
                history,
                self.workdir,
                background_manager=manager,
                capabilities=Capabilities.full(memory_mode="off"),
            )
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.finish_worker(manager)
        self.assertEqual(client.chat.completions.create.call_count, 2)
        next_client, snapshots = fake_client([reply("Collected on the next request.")])
        history.append({"role": "user", "content": "Now check."})
        homebrew_harness.runtime.loop.agent_loop(
            next_client,
            "test-model",
            history,
            self.workdir,
            background_manager=manager,
            capabilities=Capabilities.full(memory_mode="off"),
        )
        self.assertEqual(snapshots[0][-1]["name"], "background_notification")

    def test_owned_loop_closes_manager_on_normal_return_error_and_turn_limit(self):
        cases = [
            ([reply("Done")], 20, None),
            (
                [reply(calls=[call("x", name="glob", arguments='{"pattern":"*.py"}')])],
                1,
                homebrew_harness.runtime.errors.AgentError,
            ),
            (None, 20, RuntimeError),
        ]
        for responses, turns, error in cases:
            manager = self.manager(Mock())
            client, _ = fake_client(responses or [])
            if responses is None:
                client.chat.completions.create.side_effect = RuntimeError("API failed")
            with (
                self.subTest(error=error),
                patch(
                    "homebrew_harness.runtime.assembly.BackgroundManager",
                    return_value=manager,
                ),
                patch.object(manager, "close", wraps=manager.close) as close,
            ):
                if error:
                    with self.assertRaises(error):
                        homebrew_harness.runtime.loop.agent_loop(
                            client,
                            "test-model",
                            [],
                            self.workdir,
                            max_turns=turns,
                            capabilities=Capabilities.full(memory_mode="off"),
                        )
                else:
                    homebrew_harness.runtime.loop.agent_loop(
                        client,
                        "test-model",
                        [],
                        self.workdir,
                        max_turns=turns,
                        capabilities=Capabilities.full(memory_mode="off"),
                    )
                close.assert_called_once()
            self.assertTrue(manager._closed)

    def test_host_managed_child_ids_and_notifications_are_isolated_from_parent(self):
        parent = self.manager(self.controlled_runner)
        parent_task = parent.start("PARENT_BACKGROUND_OUTPUT")
        self.finish_worker(parent)
        # This child's worker uses the same deterministic runner, but its own
        # task table and queue. The parent's result must remain uncollected.
        child_release = threading.Event()
        self.addCleanup(child_release.set)

        def child_runner(command, **kwargs):
            child_release.wait(timeout=3)
            return json.dumps({"exit_code": 0, "stdout": command, "stderr": ""})

        child = self.manager(child_runner)
        client, _ = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "parent-id",
                            name="check_background",
                            arguments=json.dumps({"task_id": parent_task["bg_id"]}),
                        ),
                        call(
                            "child-start",
                            name="bash",
                            arguments='{"command":"CHILD_BACKGROUND_OUTPUT","run_in_background":true}',
                        ),
                    ]
                ),
                reply("Candidate child reply"),
                reply("Final child report"),
            ]
        )
        create = client.chat.completions.create.side_effect
        count = 0

        def complete_before_final(**kwargs):
            nonlocal count
            count += 1
            if count == 2:
                child_release.set()
                for thread in child._threads:
                    thread.join(timeout=3)
                    self.assertFalse(thread.is_alive())
            return create(**kwargs)

        client.chat.completions.create.side_effect = complete_before_final
        history = [{"role": "user", "content": "Run focused work."}]
        with (
            patch(
                "homebrew_harness.runtime.assembly.BackgroundManager",
                return_value=child,
            ),
            patch(
                "homebrew_harness.security.permissions.ask_user",
                return_value=PermissionDecision("allow", "Test approval"),
            ),
        ):
            result = homebrew_harness.runtime.loop.agent_loop(
                client,
                "test-model",
                history,
                self.workdir,
                subagent=True,
                background_manager=child,
                capabilities=Capabilities.full(memory_mode="off"),
            )
        self.assertEqual(result, "Final child report")
        self.assertIn(
            "error",
            json.loads(
                next(m for m in history if m.get("tool_call_id") == "parent-id")[
                    "content"
                ]
            ),
        )
        self.assertIn("CHILD_BACKGROUND_OUTPUT", json.dumps(history))
        self.assertNotIn("PARENT_BACKGROUND_OUTPUT", json.dumps(history))
        self.assertEqual(len(parent.collect()), 1)
        child.close()
        self.assertTrue(child._closed)
        self.assertEqual(
            {
                tool["function"]["name"]
                for tool in client.chat.completions.create.call_args_list[0].kwargs[
                    "tools"
                ]
            },
            set(homebrew_harness.tools.catalog.SUB_TOOL_NAMES)
            - {
                "run_tests",
                "list_tests",
                "get_test_result",
                "list_artifacts",
                "get_artifact",
                "publish_artifacts",
            },
        )

    def test_custom_pre_hook_blocks_background_start_after_permission(self):
        manager = self.manager(Mock())
        hooks = homebrew_harness.runtime.hooks.create_hooks()
        hooks.register_hook(
            "PreToolUse",
            lambda event: "Custom denial" if event.name == "bash" else None,
        )
        with (
            patch(
                "homebrew_harness.security.permissions.ask_user",
                return_value=PermissionDecision("allow", "Test approval"),
            ),
            patch.object(manager, "start") as start,
        ):
            result = self.invoke(
                "bash", {"command": "unused", "run_in_background": True}, manager, hooks
            )
        self.assertEqual(result, "Custom denial")
        start.assert_not_called()

    def test_direct_background_dispatch_requires_a_session_manager(self):
        with patch(
            "homebrew_harness.security.permissions.ask_user",
            return_value=PermissionDecision("allow", "Test approval"),
        ):
            result = json.loads(
                self.invoke(
                    "bash", {"command": "unused", "run_in_background": True}, None
                )
            )
        self.assertIn("requires a manager", result["error"])
