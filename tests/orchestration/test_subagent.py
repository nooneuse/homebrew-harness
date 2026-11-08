"""Nested-loop isolation, shared workspace, and inherited tool policies."""

from __future__ import annotations
from homebrew_harness.runtime.capabilities import Capabilities

from homebrew_harness.runs.spec import RUN_TOOLS

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openai import APIError

import homebrew_harness.context.skill_loader

import homebrew_harness.execution.test_runner

import homebrew_harness.orchestration.teams

import homebrew_harness.runtime.assembly

import homebrew_harness.runtime.hooks

import homebrew_harness.runtime.loop

import homebrew_harness.runtime.prompts

import homebrew_harness.runtime.turns

import homebrew_harness.tools.catalog

import homebrew_harness.tools.registry
from tests.support.clients import conversation, call, reply
from homebrew_harness.context.todo import TODO_REMINDER, TodoManager


def task_call(identifier, prompt="Complete this focused task.", **extra):
    return call(
        identifier, name="task", arguments=json.dumps({"prompt": prompt, **extra})
    )


def scripted_client(responses):
    responses, requests = iter(responses), []

    def create(**kwargs):
        requests.append(copy.deepcopy(kwargs))
        response = next(responses)
        if isinstance(response, BaseException):
            raise response
        return response

    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=Mock(side_effect=create))
        )
    )
    return client, requests


class SubagentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workdir = Path(temp.name)
        self.hooks = homebrew_harness.runtime.hooks.create_hooks()
        self.plan = TodoManager()
        self.stderr = io.StringIO()
        capture = redirect_stderr(self.stderr)
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def run_parent(self, responses, messages=None):
        client, requests = scripted_client(responses)
        if messages is None:
            messages = [{"role": "user", "content": "Delegate the focused task."}]
        result = homebrew_harness.runtime.loop.agent_loop(
            client,
            "test-model",
            messages,
            self.workdir,
            hooks=self.hooks,
            todo_manager=self.plan,
            capabilities=Capabilities.full(memory_mode="off"),
        )
        return result, requests, messages

    def run_child(self, responses, **kwargs):
        client, requests = scripted_client(responses)
        result = homebrew_harness.runtime.turns.run_subagent(
            "Focused task.",
            self.workdir,
            client=client,
            model="test-model",
            hooks=self.hooks,
            **kwargs,
        )
        return result, requests

    def test_fresh_context_and_only_final_text_returns_to_parent(self):
        (self.workdir / "notes.txt").write_text("CHILD_INTERNAL_DETAIL")
        history = [
            {
                "role": "system",
                "content": homebrew_harness.runtime.prompts.build_system_prompt(
                    self.workdir,
                    skill_loader=homebrew_harness.context.skill_loader.SkillLoader(
                        self.workdir / "skills"
                    ),
                ),
            },
            {"role": "user", "content": "PARENT_ONLY_CONTEXT"},
        ]
        _, requests, history = self.run_parent(
            [
                reply(
                    calls=[
                        task_call(
                            "parent-task", "Read notes.txt and report a short finding."
                        )
                    ]
                ),
                reply(
                    calls=[
                        call(
                            "child-read",
                            name="read_file",
                            arguments='{"path":"notes.txt"}',
                        )
                    ],
                    reasoning="CHILD_REASONING_ONLY",
                ),
                reply("Concise child finding."),
                reply("Parent finished."),
            ],
            history,
        )
        self.assertEqual(len(conversation(requests[1]["messages"])), 2)
        self.assertEqual(
            conversation(requests[1]["messages"])[1],
            {"role": "user", "content": "Read notes.txt and report a short finding."},
        )
        self.assertIn(
            "PARENT_ONLY_CONTEXT",
            next(
                message["content"]
                for message in requests[1]["messages"]
                if message.get("name") == "inherited_task_constraints"
            ),
        )
        self.assertNotIn(
            "PARENT_ONLY_CONTEXT", json.dumps(conversation(requests[1]["messages"]))
        )
        self.assertIn("CHILD_INTERNAL_DETAIL", json.dumps(requests[2]["messages"]))
        self.assertIn("CHILD_REASONING_ONLY", json.dumps(requests[2]["messages"]))
        self.assertNotIn("CHILD_INTERNAL_DETAIL", json.dumps(history))
        self.assertNotIn("CHILD_REASONING_ONLY", json.dumps(history))
        self.assertNotIn("child-read", json.dumps(history))
        results = [message for message in history if message["role"] == "tool"]
        self.assertEqual(len(results), 1)
        self.assertEqual(
            json.loads(results[0]["content"])["result"], "Concise child finding."
        )
        self.assertEqual(json.loads(results[0]["content"])["outcome"], "completed")
        self.assertTrue(all(request["model"] == "test-model" for request in requests))

    def test_child_cannot_delegate_or_update_parent_plan_even_if_it_invents_calls(self):
        self.plan.update([{"content": "Parent plan", "status": "in_progress"}])
        original, revision = self.plan.items, self.plan.revision
        _, requests, _ = self.run_parent(
            [
                reply(calls=[task_call("outer")]),
                reply(
                    calls=[
                        task_call("recursive"),
                        call(
                            "change-plan", name="todo_write", arguments='{"todos":[]}'
                        ),
                    ]
                ),
                reply("Those tools were unavailable."),
                reply("Finished."),
            ]
        )
        parent_tools = {tool["function"]["name"] for tool in requests[0]["tools"]}
        child_tools = {tool["function"]["name"] for tool in requests[1]["tools"]}
        self.assertEqual(
            parent_tools,
            {
                tool["function"]["name"]
                for tool in homebrew_harness.tools.catalog.TOOLS
                if tool["function"]["name"]
                not in (
                    {
                        *homebrew_harness.orchestration.teams.TEAM_TOOL_NAMES,
                        *homebrew_harness.execution.test_runner.TEST_TOOL_NAMES,
                        "connect_mcp",
                        "workflow",
                        "list_artifacts",
                        "get_artifact",
                        "publish_artifacts",
                        *RUN_TOOLS,
                    }
                    - {"create_worktree"}
                )
            },
        )
        self.assertEqual(
            child_tools,
            set(homebrew_harness.tools.catalog.SUB_TOOL_NAMES)
            - set(homebrew_harness.execution.test_runner.TEST_TOOL_NAMES)
            - {
                "check_background",
                "list_artifacts",
                "get_artifact",
                "publish_artifacts",
            },
        )
        self.assertNotIn("task", child_tools)
        self.assertNotIn("todo_write", child_tools)
        self.assertTrue(
            RUN_TOOLS.isdisjoint(
                parent_tools | child_tools
            )
        )
        for result in requests[2]["messages"][-2:]:
            self.assertEqual(json.loads(result["content"])["error"], "Unknown tool.")
        self.assertEqual(len(requests), 4)
        self.assertEqual(self.plan.items, original)
        self.assertEqual(self.plan.revision, revision)

    def test_each_task_in_a_batch_gets_a_fresh_child_conversation(self):
        _, requests, history = self.run_parent(
            [
                reply(calls=[task_call("a", "Task A"), task_call("b", "Task B")]),
                reply("Result A"),
                reply("Result B"),
                reply("Combined."),
            ]
        )
        self.assertEqual(requests[1]["messages"][-1]["content"], "Task A")
        self.assertEqual(requests[2]["messages"][-1]["content"], "Task B")
        self.assertEqual(len(conversation(requests[2]["messages"])), 2)
        self.assertNotIn("Result A", json.dumps(requests[2]["messages"]))
        self.assertEqual(
            [
                (m["tool_call_id"], json.loads(m["content"])["result"])
                for m in history
                if m["role"] == "tool"
            ],
            [("a", "Result A"), ("b", "Result B")],
        )

    def test_child_file_change_is_visible_to_next_parent_tool_synchronously(self):
        _, requests, history = self.run_parent(
            [
                reply(
                    calls=[
                        task_call("create"),
                        call(
                            "parent-read",
                            name="read_file",
                            arguments='{"path":"child.txt"}',
                        ),
                    ]
                ),
                reply(
                    calls=[
                        call(
                            "child-write",
                            name="write_file",
                            arguments='{"path":"child.txt","content":"shared"}',
                        )
                    ]
                ),
                reply("Created child.txt."),
                reply("Parent verified the file."),
            ]
        )
        self.assertEqual((self.workdir / "child.txt").read_text(), "shared")
        self.assertEqual(
            json.loads(requests[3]["messages"][-1]["content"])["content"], "shared"
        )
        self.assertEqual(
            [m["tool_call_id"] for m in history if m["role"] == "tool"],
            ["create", "parent-read"],
        )

    def test_child_rounds_do_not_create_todos_or_trigger_parent_reminders(self):
        self.plan.update([{"content": "Keep parent plan", "status": "pending"}])
        responses = [reply(calls=[task_call("outer")])]
        responses.extend(
            reply(calls=[call(f"g{index}", name="glob", arguments='{"pattern":"*"}')])
            for index in range(4)
        )
        responses.extend([reply("Child done."), reply("Parent done.")])
        with patch(
            "homebrew_harness.runtime.assembly.TodoManager",
            side_effect=AssertionError("Unexpected child plan"),
        ):
            _, requests, _ = self.run_parent(responses)
        self.assertFalse(
            any(
                message.get("content") == TODO_REMINDER
                for request in requests
                for message in request["messages"]
            )
        )
        self.assertEqual(self.plan.revision, 1)

    def test_blocking_outer_task_prevents_any_child_model_call(self):
        self.hooks.register_hook(
            "PreToolUse",
            lambda event: "Delegation blocked." if event.name == "task" else None,
        )
        _, requests, history = self.run_parent(
            [reply(calls=[task_call("outer")]), reply("Delegation denied.")]
        )
        self.assertEqual(len(requests), 2)
        self.assertEqual(history[-2]["content"], "Delegation blocked.")

    def test_parent_and_child_share_hooks_without_duplicate_user_input_events(self):
        prompts, events, stops = Mock(return_value=None), [], []
        self.hooks.register_hook("UserPromptSubmit", prompts)
        self.hooks.register_hook(
            "PreToolUse", lambda event: events.append(("pre", event.name))
        )
        self.hooks.register_hook(
            "PostToolUse", lambda event, output: events.append(("post", event.name))
        )
        self.hooks.register_hook("Stop", lambda messages: stops.append(id(messages)))
        history = [
            {
                "role": "system",
                "content": homebrew_harness.runtime.prompts.build_system_prompt(
                    self.workdir,
                    skill_loader=homebrew_harness.context.skill_loader.SkillLoader(
                        self.workdir / "skills"
                    ),
                ),
            }
        ]
        homebrew_harness.runtime.hooks.submit_prompt(
            history, "Delegate a read.", self.workdir, self.hooks
        )
        self.run_parent(
            [
                reply(calls=[task_call("outer")]),
                reply(
                    calls=[call("child-glob", name="glob", arguments='{"pattern":"*"}')]
                ),
                reply("Child done."),
                reply("Parent done."),
            ],
            history,
        )
        self.assertEqual(
            events,
            [("pre", "task"), ("pre", "glob"), ("post", "glob"), ("post", "task")],
        )
        prompts.assert_called_once()
        self.assertEqual(len(stops), 2)
        self.assertNotEqual(stops[0], stops[1])

    def test_custom_hook_can_block_child_file_changes(self):
        self.hooks.register_hook(
            "PreToolUse",
            lambda event: "Read-only task." if event.name == "write_file" else None,
        )
        _, requests, _ = self.run_parent(
            [
                reply(calls=[task_call("outer")]),
                reply(
                    calls=[
                        call(
                            "write",
                            name="write_file",
                            arguments='{"path":"blocked.txt","content":"x"}',
                        )
                    ]
                ),
                reply("Write blocked."),
                reply("Parent acknowledged."),
            ]
        )
        self.assertEqual(requests[2]["messages"][-1]["content"], "Read-only task.")
        self.assertFalse((self.workdir / "blocked.txt").exists())

    def test_child_bash_still_requires_terminal_approval(self):
        (self.workdir / "keep.txt").write_text("keep")
        handler = Mock()
        with (
            patch(
                "homebrew_harness.security.permissions.sys.stdin.isatty",
                return_value=False,
            ),
            patch("builtins.input", return_value="yes") as approve,
            patch.dict(
                homebrew_harness.tools.registry.TOOL_HANDLERS, {"bash": handler}
            ),
        ):
            _, requests = self.run_child(
                [reply(calls=[call("delete", "rm keep.txt")]), reply("Denied.")]
            )
        self.assertEqual(
            json.loads(requests[1]["messages"][-1]["content"])["permission"], "deny"
        )
        handler.assert_not_called()
        approve.assert_not_called()
        self.assertEqual((self.workdir / "keep.txt").read_text(), "keep")

    def test_hard_denials_apply_inside_child_without_offering_approval(self):
        with (
            patch("homebrew_harness.security.permissions.ask_user") as approve,
            patch.dict(
                homebrew_harness.tools.registry.TOOL_HANDLERS, {"bash": Mock()}
            ) as handlers,
        ):
            _, requests = self.run_child(
                [reply(calls=[call("forbidden", "sudo true")]), reply("Blocked.")]
            )
            self.assertEqual(
                json.loads(requests[1]["messages"][-1]["content"])["permission"], "deny"
            )
            handlers["bash"].assert_not_called()
        approve.assert_not_called()

    def test_approved_child_bash_runs_in_shared_working_directory(self):
        with (
            patch(
                "homebrew_harness.security.permissions.sys.stdin.isatty",
                return_value=True,
            ),
            patch("builtins.input", return_value="y") as approve,
        ):
            result, _ = self.run_child(
                [
                    reply(calls=[call("write", "printf approved > approved.txt")]),
                    reply("Written."),
                ]
            )
        approve.assert_called_once()
        self.assertEqual(json.loads(result)["result"], "Written.")
        self.assertEqual((self.workdir / "approved.txt").read_text(), "approved")

    def test_child_stop_hook_can_continue_without_returning_premature_text(self):
        checks = iter(["Read proof.txt before finishing.", None])

        def stop(messages):
            if "focused subagent" in messages[0]["content"]:
                return next(checks)
            return None

        self.hooks.register_hook("Stop", stop)
        (self.workdir / "proof.txt").write_text("proof")
        _, requests, history = self.run_parent(
            [
                reply(calls=[task_call("outer")]),
                reply("CHILD_PREMATURE"),
                reply(
                    calls=[
                        call("read", name="read_file", arguments='{"path":"proof.txt"}')
                    ]
                ),
                reply("Verified child result."),
                reply("Parent finished."),
            ],
            [
                {
                    "role": "system",
                    "content": homebrew_harness.runtime.prompts.build_system_prompt(
                        self.workdir,
                        skill_loader=homebrew_harness.context.skill_loader.SkillLoader(
                            self.workdir / "skills"
                        ),
                    ),
                },
                {"role": "user", "content": "Delegate."},
            ],
        )
        self.assertEqual(
            requests[2]["messages"][-1]["content"], "Read proof.txt before finishing."
        )
        self.assertNotIn("CHILD_PREMATURE", json.dumps(history))
        self.assertEqual(
            json.loads(history[-2]["content"])["result"], "Verified child result."
        )

    def test_child_stop_failure_becomes_a_parent_tool_error(self):
        def stop(messages):
            if "focused subagent" in messages[0]["content"]:
                raise RuntimeError("private-test-value")
            return None

        self.hooks.register_hook("Stop", stop)
        result, _, history = self.run_parent(
            [
                reply(calls=[task_call("outer")]),
                reply("Candidate child answer."),
                reply("Child failed."),
            ]
        )
        self.assertEqual(result, "Child failed.")
        self.assertIn("Stop hook failed", json.loads(history[-2]["content"])["error"])
        self.assertNotIn(
            "private-test-value", json.dumps(history) + self.stderr.getvalue()
        )

    def test_child_api_failure_is_paired_and_private_details_are_not_forwarded(self):
        error = APIError(
            "private-test-value", request=Mock(), body={"detail": "private-test-value"}
        )
        result, _, history = self.run_parent(
            [reply(calls=[task_call("outer")]), error, reply("The subtask failed.")]
        )
        self.assertEqual(result, "The subtask failed.")
        self.assertEqual(history[-2]["tool_call_id"], "outer")
        self.assertIn("APIError", json.loads(history[-2]["content"])["error"])
        self.assertNotIn(
            "private-test-value", json.dumps(history) + self.stderr.getvalue()
        )

    def test_child_turn_limit_reports_failure_and_keeps_existing_file_changes(self):
        result, requests = self.run_child(
            [
                reply(
                    calls=[
                        call(
                            "write",
                            name="write_file",
                            arguments='{"path":"partial.txt","content":"saved"}',
                        )
                    ]
                ),
            ],
            max_turns=1,
        )
        self.assertIn("unfinished", json.loads(result)["error"])
        self.assertEqual(len(requests), 1)
        self.assertEqual((self.workdir / "partial.txt").read_text(), "saved")

    def test_task_arguments_cannot_override_host_settings_or_be_empty(self):
        invalid = [task_call("empty", "  ")]
        invalid.extend(
            task_call(name, **{name: value})
            for name, value in (
                ("model", "override"),
                ("workdir", "outside"),
                ("max_turns", 100),
                ("messages", []),
            )
        )
        with patch("homebrew_harness.runtime.turns.run_subagent") as child:
            _, requests, history = self.run_parent(
                [reply(calls=invalid), reply("Arguments rejected.")]
            )
        child.assert_not_called()
        self.assertEqual(len(requests), 2)
        self.assertTrue(
            all(
                "error" in json.loads(message["content"])
                for message in history
                if message["role"] == "tool"
            )
        )

    def test_truncated_child_output_is_not_executed(self):
        result, _ = self.run_child(
            [
                reply(
                    calls=[
                        call(
                            "write",
                            name="write_file",
                            arguments='{"path":"bad.txt","content":"bad"}',
                        )
                    ],
                    finish_reason="length",
                )
            ]
        )
        self.assertIn("incomplete", json.loads(result)["error"])
        self.assertFalse((self.workdir / "bad.txt").exists())

    def test_empty_child_final_text_is_an_error(self):
        result, _ = self.run_child([reply(None)])
        self.assertIn("without final text", json.loads(result)["error"])

    def test_interrupt_is_not_swallowed_as_an_ordinary_child_failure(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_child([KeyboardInterrupt()])


if __name__ == "__main__":
    unittest.main()
