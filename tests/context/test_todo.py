"""Plan state, lifecycle integration, and batch-level reminder semantics."""

from __future__ import annotations
from tests.support.tools import queued_cli_input
from homebrew_harness.runtime.capabilities import Capabilities

from tests.support.clients import cli_model_factory

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
import homebrew_harness.runtime.assembly
import homebrew_harness.runtime.hooks
import homebrew_harness.runtime.loop
import homebrew_harness.tools.registry
from homebrew_harness.runtime.hooks import ToolEvent
from tests.support.clients import call, fake_client, reply
from homebrew_harness.context.todo import MAX_TODOS, TODO_REMINDER, TodoManager


def item(content, status="pending"):
    return {"content": content, "status": status}


def todo_round(identifier, todos):
    return reply(
        calls=[
            call(identifier, name="todo_write", arguments=json.dumps({"todos": todos}))
        ]
    )


def glob_round(identifier):
    return reply(calls=[call(identifier, name="glob", arguments='{"pattern":"*.txt"}')])


class TodoTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workdir = Path(temp.name)
        self.manager = TodoManager()
        self.stderr = io.StringIO()
        capture = redirect_stderr(self.stderr)
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def execute(self, todos, hooks=None):
        request = SimpleNamespace(
            id="todo-test",
            type="function",
            function=SimpleNamespace(
                name="todo_write", arguments=json.dumps({"todos": todos})
            ),
        )
        return execute_fixture_tool(
            request,
            self.workdir,
            hooks=hooks,
            handlers=homebrew_harness.tools.registry.create_tool_handlers(self.manager),
        )

    def run_loop(self, responses, *, hooks=None, messages=None):
        client, snapshots = fake_client(responses)
        if messages is None:
            messages = [{"role": "user", "content": "Complete the task."}]
        result = homebrew_harness.runtime.loop.agent_loop(
            client,
            "test-model",
            messages,
            self.workdir,
            hooks=hooks,
            todo_manager=self.manager,
            capabilities=Capabilities.full(memory_mode="off"),
        )
        return result, snapshots, messages

    def test_full_replacement_and_status_rendering(self):
        result = json.loads(
            self.execute(
                [
                    item(" Inspect files ", "completed"),
                    item("Edit files", "in_progress"),
                    item("Verify"),
                ]
            )
        )
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["total"], 3)
        self.assertEqual(
            result["plan"],
            "[x] Inspect files\n[>] Edit files\n[ ] Verify\n(1/3 completed)",
        )
        self.execute([item("A different task")])
        self.assertEqual(
            [todo.content for todo in self.manager.items], ["A different task"]
        )

    def test_twenty_items_are_valid_and_twenty_one_preserve_previous_state(self):
        self.manager.update([item(str(index)) for index in range(MAX_TODOS)])
        previous, revision = self.manager.items, self.manager.revision
        with self.assertRaises(ValueError):
            self.manager.update([item(str(index)) for index in range(MAX_TODOS + 1)])
        self.assertEqual(self.manager.items, previous)
        self.assertEqual(self.manager.revision, revision)

    def test_multiple_in_progress_items_are_rejected_atomically(self):
        self.manager.update([item("Existing task", "in_progress")])
        previous, revision = self.manager.items, self.manager.revision
        result = json.loads(
            self.execute([item("First", "in_progress"), item("Second", "in_progress")])
        )
        self.assertIn("error", result)
        self.assertEqual(self.manager.items, previous)
        self.assertEqual(self.manager.revision, revision)

    def test_invalid_items_leave_plan_and_revision_unchanged(self):
        self.manager.update([item("Keep this plan")])
        previous, revision = self.manager.items, self.manager.revision
        invalid = [
            [item("  ")],
            [item(42)],
            [item("x", "done")],
            [item("x", True)],
            [{"content": "x"}],
            [{"status": "pending"}],
            ["not an item"],
            [{"content": "x", "status": "pending", "extra": "unexpected"}],
        ]
        for todos in invalid:
            with self.subTest(todos=todos), self.assertRaises(ValueError):
                self.manager.update(todos)
            self.assertEqual(self.manager.items, previous)
            self.assertEqual(self.manager.revision, revision)

    def test_only_native_array_input_is_accepted(self):
        todos = [item("Native")]
        self.assertEqual(json.loads(self.execute(todos))["total"], 1)
        for value in (json.dumps(todos), repr(todos)):
            self.assertIn("error", json.loads(self.execute(value)))

    def test_serialized_input_never_evaluates_code(self):
        marker = self.workdir / "must-not-exist.txt"
        expression = f"__import__('pathlib').Path({str(marker)!r}).write_text('unsafe')"
        self.assertIn("error", json.loads(self.execute(expression)))
        self.assertFalse(marker.exists())
        self.assertEqual(self.manager.revision, 0)

    def test_empty_array_explicitly_clears_the_plan(self):
        self.execute([item("Finished", "completed")])
        result = json.loads(self.execute([]))
        self.assertEqual(
            result, {"todos": [], "completed": 0, "total": 0, "plan": "No todos."}
        )
        self.assertEqual(self.manager.items, ())

    def test_saved_items_do_not_alias_the_input(self):
        todos = [item("Original")]
        self.manager.update(todos)
        todos[0]["content"] = "Changed outside the manager"
        self.assertEqual(self.manager.items[0].content, "Original")
        with self.assertRaises(AttributeError):
            self.manager.items[0].status = "completed"

    def test_plan_updates_need_no_approval_and_create_no_files(self):
        with patch("homebrew_harness.security.permissions.ask_user") as approve:
            result = json.loads(self.execute([item("Plan only")]))
        self.assertEqual(result["total"], 1)
        approve.assert_not_called()
        self.assertEqual(list(self.workdir.iterdir()), [])

    def test_full_plan_is_visible_beyond_the_generic_preview_limit(self):
        todos = [item(f"Step {index:02d}: " + "details " * 8) for index in range(20)]
        result = json.loads(self.execute(todos))
        self.assertGreater(len(result["plan"]), 500)
        self.assertIn("[todo] Current plan\n" + result["plan"], self.stderr.getvalue())
        self.assertIn("Step 19:", self.stderr.getvalue())

    def test_rendering_escapes_control_characters(self):
        self.manager.update([item("标题\nnext\x1b[2J")])
        rendered = self.manager.render()
        self.assertNotIn("\x1b", rendered)
        self.assertIn("\\n", rendered)
        self.assertIn("\\u001b", rendered)

    def test_failed_or_blocked_updates_do_not_display_a_new_plan(self):
        self.execute([item("Keep")])
        self.stderr.truncate(0)
        self.stderr.seek(0)
        self.assertIn(
            "error",
            json.loads(
                self.execute([item("x", "in_progress"), item("y", "in_progress")])
            ),
        )
        hooks = homebrew_harness.runtime.hooks.create_hooks()
        hooks.register_hook(
            "PreToolUse",
            lambda event: (
                "Plan changes blocked." if event.name == "todo_write" else None
            ),
        )
        self.assertEqual(
            self.execute([item("Replace")], hooks), "Plan changes blocked."
        )
        self.assertNotIn("[todo] Current plan", self.stderr.getvalue())
        self.assertEqual(self.manager.items[0].content, "Keep")

    def test_nested_hook_arguments_are_frozen_and_cannot_change_the_plan(self):
        for mutate in (
            lambda event: event.arguments["todos"][0].__setitem__(
                "status", "completed"
            ),
            lambda event: event.arguments["todos"].append(item("extra")),
        ):
            with self.subTest(mutate=mutate):
                hooks = homebrew_harness.runtime.hooks.create_hooks()
                hooks.register_hook("PreToolUse", mutate)
                result = json.loads(self.execute([item("Pending")], hooks))
                self.assertEqual(result["hook"], "PreToolUse")
                self.assertEqual(self.manager.revision, 0)
        arguments = {"todos": [item("Original")]}
        event = ToolEvent("todo_write", arguments, self.workdir, "snapshot")
        arguments["todos"][0]["content"] = "Modified"
        self.assertEqual(event.arguments["todos"][0]["content"], "Original")

    def test_reminder_follows_third_batch_after_all_tool_results(self):
        hooks, prompts, messages = (
            homebrew_harness.runtime.hooks.create_hooks(),
            Mock(return_value=None),
            [],
        )
        hooks.register_hook("UserPromptSubmit", prompts)
        homebrew_harness.runtime.hooks.submit_prompt(
            messages, "Inspect files.", self.workdir, hooks
        )
        third = reply(
            calls=[
                call("three-a", name="glob", arguments='{"pattern":"*.txt"}'),
                call("three-b", name="glob", arguments='{"pattern":"*.py"}'),
            ]
        )
        _, snapshots, _ = self.run_loop(
            [glob_round("one"), glob_round("two"), third, reply("Done.")],
            hooks=hooks,
            messages=messages,
        )
        self.assertEqual(snapshots[3][-1], {"role": "user", "content": TODO_REMINDER})
        self.assertEqual(
            [message["tool_call_id"] for message in snapshots[3][-3:-1]],
            ["three-a", "three-b"],
        )
        self.assertFalse(
            any(message.get("content") == TODO_REMINDER for message in snapshots[2])
        )
        prompts.assert_called_once()

    def test_multiple_tool_calls_in_one_response_count_as_one_round(self):
        batch = reply(
            calls=[
                call(str(index), name="glob", arguments='{"pattern":"*"}')
                for index in range(4)
            ]
        )
        _, _, history = self.run_loop([batch, reply("Done.")])
        self.assertFalse(
            any(message.get("content") == TODO_REMINDER for message in history)
        )

    def test_reminders_repeat_every_three_stale_rounds(self):
        _, snapshots, history = self.run_loop(
            [*[glob_round(str(index)) for index in range(6)], reply("Done.")]
        )
        self.assertEqual(snapshots[3][-1]["content"], TODO_REMINDER)
        self.assertEqual(snapshots[6][-1]["content"], TODO_REMINDER)
        self.assertEqual(
            sum(message.get("content") == TODO_REMINDER for message in history), 2
        )

    def test_successful_todo_update_resets_the_reminder_counter(self):
        _, snapshots, history = self.run_loop(
            [
                glob_round("one"),
                glob_round("two"),
                todo_round("plan", [item("Continue")]),
                glob_round("four"),
                glob_round("five"),
                glob_round("six"),
                reply("Done."),
            ]
        )
        self.assertFalse(
            any(message.get("content") == TODO_REMINDER for message in snapshots[3])
        )
        self.assertEqual(snapshots[6][-1]["content"], TODO_REMINDER)
        self.assertEqual(
            sum(message.get("content") == TODO_REMINDER for message in history), 1
        )

    def test_failed_todo_update_does_not_reset_the_counter(self):
        self.manager.update([item("Keep")])
        _, snapshots, _ = self.run_loop(
            [
                glob_round("one"),
                glob_round("two"),
                todo_round(
                    "invalid", [item("x", "in_progress"), item("y", "in_progress")]
                ),
                reply("Done."),
            ]
        )
        self.assertEqual(snapshots[3][-1]["content"], TODO_REMINDER)
        self.assertIn("error", json.loads(snapshots[3][-2]["content"]))
        self.assertEqual(self.manager.revision, 1)

    def test_blocked_todo_update_does_not_reset_the_counter(self):
        hooks = homebrew_harness.runtime.hooks.create_hooks()
        hooks.register_hook(
            "PreToolUse",
            lambda event: "Blocked plan." if event.name == "todo_write" else None,
        )
        _, snapshots, _ = self.run_loop(
            [
                glob_round("one"),
                glob_round("two"),
                todo_round("blocked", [item("Task")]),
                reply("Done."),
            ],
            hooks=hooks,
        )
        self.assertEqual(snapshots[3][-1]["content"], TODO_REMINDER)
        self.assertEqual(snapshots[3][-2]["content"], "Blocked plan.")
        self.assertEqual(self.manager.revision, 0)

    def test_next_user_request_keeps_plan_but_resets_reminder_rounds(self):
        self.manager.update([item("Unfinished task")])
        _, _, history = self.run_loop(
            [glob_round("one"), glob_round("two"), reply("Waiting.")]
        )
        history.append({"role": "user", "content": "Continue."})
        self.run_loop([glob_round("three"), reply("Checked.")], messages=history)
        self.assertFalse(
            any(message.get("content") == TODO_REMINDER for message in history)
        )
        self.assertEqual(self.manager.items[0].content, "Unfinished task")

    def test_cli_reuses_one_manager_across_user_requests(self):
        client, _ = fake_client(
            [
                todo_round("plan", [item("Keep across turns")]),
                reply("Saved."),
                reply("Still saved."),
            ]
        )
        config = {
            name: "test-value"
            for name in homebrew_harness.configuration.CONFIG_VARIABLES
        }
        with (
            patch.dict(os.environ, config),
            patch("homebrew_harness.session_cli.load_dotenv"),
            cli_model_factory() as factory,
            patch(
                "builtins.input",
                side_effect=queued_cli_input(["Plan a task", "/queue Show the current task", "q"], lambda: client.chat.completions.create.call_count >= 3),
            ),
            patch(
                "homebrew_harness.runtime.assembly.TodoManager",
                return_value=self.manager,
            ) as manager_factory,
            redirect_stdout(io.StringIO()),
        ):
            factory.return_value.__enter__.return_value = client
            self.assertEqual(
                homebrew_harness.cli.main(["session"] +
                    ["--profile", "full"]
                    + ["--memory-mode", "off", "--cwd", str(self.workdir)]
                ),
                0,
            )
        manager_factory.assert_called_once()
        self.assertEqual(self.manager.items[0].content, "Keep across turns")
        self.assertEqual(TodoManager().items, ())


if __name__ == "__main__":
    unittest.main()
