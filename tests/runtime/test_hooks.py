"""Lifecycle order, extension behavior, and control-flow failure boundaries."""

from __future__ import annotations
from tests.support.tools import queued_cli_input
from homebrew_harness.runtime.capabilities import Capabilities

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.support.tools import execute_fixture_tool
import homebrew_harness.cli
import homebrew_harness.session_cli
import homebrew_harness.configuration
import homebrew_harness.runtime.errors
import homebrew_harness.runtime.hooks
import homebrew_harness.runtime.loop
import homebrew_harness.tools.files
import homebrew_harness.tools.registry
from homebrew_harness.runtime.hooks import HookError, HookRegistry
from homebrew_harness.security.permissions import PermissionDecision
from tests.support.clients import conversation, call, fake_client, reply


class RegistryTests(unittest.TestCase):
    def test_control_hooks_run_in_order_and_stop_at_first_result(self):
        for event in ("PreToolUse", "Stop"):
            with self.subTest(event=event):
                registry, visited = HookRegistry(), []
                registry.register_hook(event, lambda: visited.append("first"))
                registry.register_hook(event, lambda: "blocked or continue")
                registry.register_hook(event, lambda: visited.append("last"))
                self.assertEqual(registry.trigger_hooks(event), "blocked or continue")
                self.assertEqual(visited, ["first"])

    def test_observer_results_do_not_short_circuit_following_hooks(self):
        for event in ("UserPromptSubmit", "PostToolUse"):
            with self.subTest(event=event):
                registry, visited = HookRegistry(), []
                registry.register_hook(event, lambda: "ignored")
                registry.register_hook(event, lambda: visited.append("next"))
                self.assertIsNone(registry.trigger_hooks(event))
                self.assertEqual(visited, ["next"])

    def test_control_hooks_reject_false_empty_or_wrong_type_results(self):
        for event in ("PreToolUse", "Stop"):
            for value in (False, "", "  ", 0, {}):
                with self.subTest(event=event, value=value):
                    registry = HookRegistry()
                    registry.register_hook(event, lambda: value)
                    with self.assertRaises(HookError):
                        registry.trigger_hooks(event)

    def test_new_registration_during_emission_applies_next_time(self):
        registry, visited = HookRegistry(), []

        def register_later():
            visited.append("initial")
            registry.register_hook("PostToolUse", lambda: visited.append("later"))

        registry.register_hook("PostToolUse", register_later)
        registry.trigger_hooks("PostToolUse")
        self.assertEqual(visited, ["initial"])
        registry.trigger_hooks("PostToolUse")
        self.assertEqual(visited, ["initial", "initial", "later"])

    def test_registration_rejects_unknown_events_and_noncallables(self):
        registry = HookRegistry()
        with self.assertRaises(ValueError):
            registry.register_hook("Typo", lambda: None)
        with self.assertRaises(TypeError):
            registry.register_hook("Stop", None)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workdir = Path(temp.name)
        self.stderr = io.StringIO()
        capture = redirect_stderr(self.stderr)
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def execute(self, name, arguments, hooks):
        request = SimpleNamespace(
            id="test-call",
            type="function",
            function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
        )
        return execute_fixture_tool(request, self.workdir, hooks=hooks)

    def test_all_four_events_surround_the_real_handler(self):
        hooks, events, messages = homebrew_harness.runtime.hooks.create_hooks(), [], []
        hooks.register_hook(
            "UserPromptSubmit", lambda query, cwd: events.append("input")
        )
        hooks.register_hook("PreToolUse", lambda event: events.append("pre"))
        hooks.register_hook("PostToolUse", lambda event, output: events.append("post"))
        hooks.register_hook("Stop", lambda messages: events.append("stop"))
        client, _ = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "write",
                            name="write_file",
                            arguments='{"path":"result.txt","content":"ok"}',
                        )
                    ]
                ),
                reply("Done."),
            ]
        )
        original = homebrew_harness.tools.files.run_write

        def write(**kwargs):
            events.append("handler")
            return original(**kwargs)

        homebrew_harness.runtime.hooks.submit_prompt(
            messages, "Create a file.", self.workdir, hooks
        )
        with patch.dict(
            homebrew_harness.tools.registry.TOOL_HANDLERS, {"write_file": write}
        ):
            self.assertEqual(
                homebrew_harness.runtime.loop.agent_loop(
                    client,
                    "test-model",
                    messages,
                    self.workdir,
                    hooks=hooks,
                    capabilities=Capabilities.full(memory_mode="off"),
                ),
                "Done.",
            )
        self.assertEqual(events, ["input", "pre", "handler", "post", "stop"])
        self.assertEqual((self.workdir / "result.txt").read_text(), "ok")

    def test_permission_remains_first_and_cannot_be_overridden_by_later_hooks(self):
        hooks, after, handler = (
            homebrew_harness.runtime.hooks.create_hooks(),
            Mock(),
            Mock(),
        )
        hooks.register_hook("PreToolUse", after)
        hooks.register_hook("PostToolUse", after)
        with (
            patch.dict(
                homebrew_harness.tools.registry.TOOL_HANDLERS, {"bash": handler}
            ),
            patch("homebrew_harness.security.permissions.ask_user") as approve,
        ):
            result = json.loads(self.execute("bash", {"command": "sudo true"}, hooks))
        self.assertEqual(result["permission"], "deny")
        handler.assert_not_called()
        after.assert_not_called()
        approve.assert_not_called()

    def test_custom_pre_hook_blocks_one_call_without_dropping_the_batch(self):
        hooks, posts = homebrew_harness.runtime.hooks.create_hooks(), []
        (self.workdir / "keep.txt").write_text("keep")
        hooks.register_hook(
            "PreToolUse",
            lambda event: (
                "Writes are blocked by the host."
                if event.name == "write_file"
                else None
            ),
        )
        hooks.register_hook(
            "PostToolUse", lambda event, output: posts.append(event.name)
        )
        client, snapshots = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "blocked",
                            name="write_file",
                            arguments='{"path":"keep.txt","content":"changed"}',
                        ),
                        call("read", name="read_file", arguments='{"path":"keep.txt"}'),
                    ]
                ),
                reply("Write blocked; original file verified."),
            ]
        )
        messages = [{"role": "user", "content": "Check the file."}]
        homebrew_harness.runtime.loop.agent_loop(
            client,
            "test-model",
            messages,
            self.workdir,
            hooks=hooks,
            capabilities=Capabilities.full(memory_mode="off"),
        )
        results = conversation(snapshots[1])[2:]
        self.assertEqual(
            [result["tool_call_id"] for result in results], ["blocked", "read"]
        )
        self.assertEqual(results[0]["content"], "Writes are blocked by the host.")
        self.assertEqual(posts, ["read_file"])
        self.assertEqual((self.workdir / "keep.txt").read_text(), "keep")

    def test_pre_hook_exception_prevents_execution_and_hides_error_details(self):
        hooks, handler, post = (
            homebrew_harness.runtime.hooks.create_hooks(),
            Mock(),
            Mock(),
        )

        def broken(event):
            raise RuntimeError("private-test-value")

        hooks.register_hook("PreToolUse", broken)
        hooks.register_hook("PostToolUse", post)
        with patch.dict(
            homebrew_harness.tools.registry.TOOL_HANDLERS, {"write_file": handler}
        ):
            output = self.execute(
                "write_file", {"path": "file.txt", "content": "x"}, hooks
            )
        self.assertEqual(json.loads(output)["hook"], "PreToolUse")
        self.assertNotIn("private-test-value", output + self.stderr.getvalue())
        handler.assert_not_called()
        post.assert_not_called()

    def test_post_hook_exception_preserves_success_after_file_write(self):
        hooks, following = (
            homebrew_harness.runtime.hooks.create_hooks(),
            Mock(return_value=None),
        )

        def broken(event, output):
            raise RuntimeError("private-test-value")

        hooks.register_hook("PostToolUse", broken)
        hooks.register_hook("PostToolUse", following)
        client, snapshots = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "write",
                            name="write_file",
                            arguments='{"path":"done.txt","content":"done"}',
                        )
                    ]
                ),
                reply("Done."),
            ]
        )
        messages = [{"role": "user", "content": "Create a file."}]
        self.assertEqual(
            homebrew_harness.runtime.loop.agent_loop(
                client,
                "test-model",
                messages,
                self.workdir,
                hooks=hooks,
                capabilities=Capabilities.full(memory_mode="off"),
            ),
            "Done.",
        )
        self.assertEqual(json.loads(snapshots[1][-1]["content"])["bytes_written"], 4)
        self.assertEqual((self.workdir / "done.txt").read_text(), "done")
        following.assert_called_once()
        self.assertIn("PostToolUse hook failed", self.stderr.getvalue())
        self.assertNotIn("private-test-value", self.stderr.getvalue())

    def test_post_hook_observes_a_handler_error_but_not_a_validation_failure(self):
        hooks, outputs = homebrew_harness.runtime.hooks.create_hooks(), []
        hooks.register_hook(
            "PostToolUse", lambda event, output: outputs.append(json.loads(output))
        )
        self.execute("read_file", {"path": "missing.txt"}, hooks)
        self.assertEqual(len(outputs), 1)
        self.assertIn("error", outputs[0])
        pre = Mock(return_value=None)
        hooks.register_hook("PreToolUse", pre)
        self.execute("read_file", {"path": 42}, hooks)
        pre.assert_not_called()
        self.assertEqual(len(outputs), 1)

    def test_pre_hooks_cannot_change_approved_tool_arguments(self):
        hooks, handler = homebrew_harness.runtime.hooks.create_hooks(), Mock()

        def mutate(event):
            event.arguments["command"] = "printf changed"

        hooks.register_hook("PreToolUse", mutate)
        with (
            patch(
                "homebrew_harness.security.permissions.ask_user",
                return_value=PermissionDecision("allow", "Test approval"),
            ),
            patch.dict(
                homebrew_harness.tools.registry.TOOL_HANDLERS, {"bash": handler}
            ),
        ):
            result = json.loads(
                self.execute("bash", {"command": "printf original"}, hooks)
            )
        self.assertEqual(result["hook"], "PreToolUse")
        handler.assert_not_called()

    def test_stop_can_request_verification_without_resubmitting_user_prompt(self):
        hooks, user_hook, stop_hook = (
            homebrew_harness.runtime.hooks.create_hooks(),
            Mock(return_value=None),
            Mock(),
        )
        (self.workdir / "proof.txt").write_text("verified")
        hooks.register_hook("UserPromptSubmit", user_hook)
        stop_hook.side_effect = ["Read proof.txt before finishing.", None]
        hooks.register_hook("Stop", stop_hook)
        client, snapshots = fake_client(
            [
                reply("I am ready to finish."),
                reply(
                    calls=[
                        call("read", name="read_file", arguments='{"path":"proof.txt"}')
                    ]
                ),
                reply("The file contains verified."),
            ]
        )
        messages = []
        homebrew_harness.runtime.hooks.submit_prompt(
            messages, "Check proof.txt.", self.workdir, hooks
        )
        result = homebrew_harness.runtime.loop.agent_loop(
            client,
            "test-model",
            messages,
            self.workdir,
            hooks=hooks,
            capabilities=Capabilities.full(memory_mode="off"),
        )
        self.assertEqual(result, "The file contains verified.")
        self.assertEqual(
            snapshots[1][-1],
            {"role": "user", "content": "Read proof.txt before finishing."},
        )
        self.assertEqual(snapshots[2][-1]["tool_call_id"], "read")
        user_hook.assert_called_once()
        self.assertEqual(stop_hook.call_count, 2)

    def test_stop_continuations_obey_existing_model_turn_limit(self):
        hooks = homebrew_harness.runtime.hooks.create_hooks()
        hooks.register_hook("Stop", lambda messages: "Continue.")
        client, snapshots = fake_client([reply("First."), reply("Second.")])
        with self.assertRaisesRegex(
            homebrew_harness.runtime.errors.AgentError, "unfinished"
        ):
            homebrew_harness.runtime.loop.agent_loop(
                client,
                "test-model",
                [{"role": "user", "content": "Task"}],
                self.workdir,
                max_turns=2,
                hooks=hooks,
                capabilities=Capabilities.full(memory_mode="off"),
            )
        self.assertEqual(len(snapshots), 2)

    def test_stop_failure_does_not_silently_accept_the_final_answer(self):
        hooks = homebrew_harness.runtime.hooks.create_hooks()
        hooks.register_hook(
            "Stop", Mock(side_effect=RuntimeError("private-test-value"))
        )
        client, snapshots = fake_client([reply("Done.")])
        with self.assertRaisesRegex(HookError, "Stop hook failed"):
            homebrew_harness.runtime.loop.agent_loop(
                client,
                "test-model",
                [{"role": "user", "content": "Task"}],
                self.workdir,
                hooks=hooks,
                capabilities=Capabilities.full(memory_mode="off"),
            )
        self.assertEqual(len(snapshots), 1)

    def test_user_observer_failure_keeps_original_request_and_later_observers(self):
        hooks, seen, messages = homebrew_harness.runtime.hooks.create_hooks(), [], []
        hooks.register_hook(
            "UserPromptSubmit", Mock(side_effect=ValueError("private-test-value"))
        )
        hooks.register_hook("UserPromptSubmit", lambda query, cwd: seen.append(query))
        homebrew_harness.runtime.hooks.submit_prompt(
            messages, "Original request", self.workdir, hooks
        )
        self.assertEqual(messages, [{"role": "user", "content": "Original request"}])
        self.assertEqual(seen, ["Original request"])
        self.assertNotIn("private-test-value", self.stderr.getvalue())

    def test_separate_registries_do_not_leak_custom_hooks_between_sessions(self):
        first, second = (
            homebrew_harness.runtime.hooks.create_hooks(),
            homebrew_harness.runtime.hooks.create_hooks(),
        )
        first.register_hook(
            "PreToolUse", lambda event: "Blocked only in the first session."
        )
        self.assertEqual(
            self.execute("write_file", {"path": "a.txt", "content": "a"}, first),
            "Blocked only in the first session.",
        )
        self.assertIn(
            "bytes_written",
            json.loads(
                self.execute("write_file", {"path": "b.txt", "content": "b"}, second)
            ),
        )
        self.assertFalse((self.workdir / "a.txt").exists())
        self.assertTrue((self.workdir / "b.txt").exists())

    def test_cli_emits_user_event_in_both_modes_and_reuses_session_registry(self):
        for argv, answers, expected in (
            (["Single task"], [], ["Single task"]),
            ([], ["", "/queue First task", "/queue Second task", "q"], ["First task", "Second task"]),
        ):
            with self.subTest(argv=argv):
                hooks, seen = homebrew_harness.runtime.hooks.create_hooks(), []
                hooks.register_hook(
                    "UserPromptSubmit", lambda query, cwd: seen.append(query)
                )
                client, _ = fake_client([reply("Done.") for _ in expected])
                config = {
                    name: "test-value"
                    for name in homebrew_harness.configuration.CONFIG_VARIABLES
                }
                with (
                    patch.dict(os.environ, config),
                    patch("homebrew_harness.session_cli.load_dotenv"),
                    patch("homebrew_harness.session_cli.OpenAI") as factory,
                    patch("builtins.input", side_effect=queued_cli_input(answers, lambda: len(seen) == len(expected))),
                    patch(
                        "homebrew_harness.runtime.hooks.create_hooks",
                        return_value=hooks,
                    ) as create,
                    redirect_stdout(io.StringIO()),
                ):
                    factory.return_value.__enter__.return_value = client
                    self.assertEqual(
                        homebrew_harness.cli.main(["session"] +
                            ["--profile", "full"]
                            + [
                                "--memory-mode",
                                "off",
                                "--cwd",
                                str(self.workdir),
                                *argv,
                            ]
                        ),
                        0,
                    )
                self.assertEqual(seen, expected)
                create.assert_called_once()


if __name__ == "__main__":
    unittest.main()
