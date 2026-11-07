"""Protocol and execution boundaries for the shared loop; no network required."""

from __future__ import annotations
from homebrew_harness.runtime.capabilities import Capabilities

import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openai import APIError
from openai.types.chat import ChatCompletion

import homebrew_harness.cli

import homebrew_harness.configuration

import homebrew_harness.runtime.errors

import homebrew_harness.runtime.loop

import homebrew_harness.tools.registry

import homebrew_harness.tools.shell
from homebrew_harness.security.permissions import PermissionDecision


from tests.support.clients import reply, call, fake_client, conversation


class AgentLoopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workdir = Path(self.temp.name)
        self.history = [{"role": "user", "content": "Complete the task."}]

    def run_loop(self, client, **kwargs):
        # These tests exercise message protocol; test_permissions checks the gates.
        with (
            redirect_stderr(io.StringIO()),
            patch(
                "homebrew_harness.security.permissions.ask_user",
                return_value=PermissionDecision("allow", "Test approval"),
            ),
        ):
            return homebrew_harness.runtime.loop.agent_loop(
                client,
                "test-model",
                self.history,
                self.workdir,
                **kwargs,
                capabilities=Capabilities.full(memory_mode="off"),
            )

    def test_text_response_stops_without_an_empty_tool_message(self):
        # The actual calls, not finish_reason alone, determine whether to execute tools.
        client, snapshots = fake_client([reply("Done.", finish_reason="tool_calls")])
        with patch.dict(
            homebrew_harness.tools.registry.TOOL_HANDLERS, {"bash": Mock()}
        ) as handlers:
            self.assertEqual(self.run_loop(client), "Done.")
            handlers["bash"].assert_not_called()
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(
            [m["role"] for m in conversation(self.history)], ["user", "assistant"]
        )

    def test_multiple_tools_are_paired_and_reasoning_is_preserved(self):
        client, snapshots = fake_client(
            [
                reply(
                    calls=[
                        call("first", "printf first"),
                        call("second", "printf second"),
                    ],
                    finish_reason="tool_calls",
                    reasoning="test reasoning field",
                ),
                reply("Both commands completed."),
            ]
        )
        run = Mock(side_effect=["first result", "second result"])
        with patch.dict(homebrew_harness.tools.registry.TOOL_HANDLERS, {"bash": run}):
            self.assertEqual(self.run_loop(client), "Both commands completed.")
        self.assertEqual(
            [c.kwargs["command"] for c in run.call_args_list],
            ["printf first", "printf second"],
        )
        self.assertEqual(
            [m["role"] for m in conversation(snapshots[1])],
            ["user", "assistant", "tool", "tool"],
        )
        self.assertEqual(
            conversation(snapshots[1])[1]["reasoning_content"], "test reasoning field"
        )
        self.assertEqual(
            [(m["tool_call_id"], m["content"]) for m in conversation(snapshots[1])[2:]],
            [("first", "first result"), ("second", "second result")],
        )

    def test_invalid_arguments_are_returned_then_model_can_correct_them(self):
        client, snapshots = fake_client(
            [
                reply(calls=[call("bad", arguments="{broken")]),
                reply(calls=[call("corrected", "printf corrected")]),
                reply("Fixed."),
            ]
        )
        run = Mock(return_value="corrected output")
        with patch.dict(homebrew_harness.tools.registry.TOOL_HANDLERS, {"bash": run}):
            self.assertEqual(self.run_loop(client), "Fixed.")
        run.assert_called_once_with(command="printf corrected", workdir=self.workdir)
        self.assertIn("error", json.loads(snapshots[1][-1]["content"]))
        self.assertEqual(snapshots[1][-1]["tool_call_id"], "bad")
        self.assertEqual(snapshots[2][-1]["tool_call_id"], "corrected")

    def test_unknown_tool_and_invalid_command_shapes_do_not_execute(self):
        invalid = [
            call("unknown", "echo hi", name="unknown"),
            call("list", arguments="[]"),
            call("empty", "  "),
            call("type", arguments='{"command": 123}'),
            call("extra", arguments='{"command": "echo hi", "extra": true}'),
        ]
        client, _ = fake_client([reply(calls=invalid), reply("Stopped.")])
        run = Mock()
        with patch.dict(homebrew_harness.tools.registry.TOOL_HANDLERS, {"bash": run}):
            self.run_loop(client)
        run.assert_not_called()
        results = [m for m in self.history if m["role"] == "tool"]
        self.assertEqual(len(results), len(invalid))
        self.assertTrue(all("error" in json.loads(m["content"]) for m in results))

    def test_truncated_response_does_not_execute_or_append_partial_calls(self):
        client, _ = fake_client(
            [reply(calls=[call("partial", "touch unwanted")], finish_reason="length")]
        )
        run = Mock()
        with (
            patch.dict(homebrew_harness.tools.registry.TOOL_HANDLERS, {"bash": run}),
            self.assertRaises(homebrew_harness.runtime.errors.AgentError),
        ):
            self.run_loop(client)
        run.assert_not_called()
        self.assertEqual(len(conversation(self.history)), 1)

    def test_turn_limit_keeps_completed_tool_pairs(self):
        client, snapshots = fake_client([reply(calls=[call("last", "printf done")])])
        with (
            patch.dict(
                homebrew_harness.tools.registry.TOOL_HANDLERS,
                {"bash": Mock(return_value="done")},
            ),
            self.assertRaisesRegex(
                homebrew_harness.runtime.errors.AgentError, "unfinished"
            ),
        ):
            self.run_loop(client, max_turns=1)
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(self.history[-1]["tool_call_id"], "last")

    def test_mixed_file_tools_execute_in_order_and_return_matching_results(self):
        client, snapshots = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "write",
                            name="write_file",
                            arguments=json.dumps(
                                {"path": "note.txt", "content": "old"}
                            ),
                        ),
                        call(
                            "edit",
                            name="edit_file",
                            arguments=json.dumps(
                                {
                                    "path": "note.txt",
                                    "old_text": "old",
                                    "new_text": "new",
                                }
                            ),
                        ),
                        call(
                            "read",
                            name="read_file",
                            arguments=json.dumps({"path": "note.txt"}),
                        ),
                        call(
                            "find",
                            name="glob",
                            arguments=json.dumps({"pattern": "*.txt"}),
                        ),
                    ]
                ),
                reply("Updated and checked."),
            ]
        )
        self.assertEqual(self.run_loop(client), "Updated and checked.")
        results = conversation(snapshots[1])[2:]
        self.assertEqual(
            [m["tool_call_id"] for m in results], ["write", "edit", "read", "find"]
        )
        self.assertEqual(json.loads(results[2]["content"])["content"], "new")
        self.assertEqual(json.loads(results[3]["content"])["paths"], ["note.txt"])

    def test_one_tool_failure_does_not_drop_other_results_in_the_batch(self):
        client, snapshots = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "missing",
                            name="read_file",
                            arguments='{"path": "missing.txt"}',
                        ),
                        call(
                            "write",
                            name="write_file",
                            arguments='{"path": "exists.txt", "content": "ok"}',
                        ),
                    ]
                ),
                reply("One file was missing; the other was written."),
            ]
        )
        self.run_loop(client)
        results = conversation(snapshots[1])[2:]
        self.assertEqual([m["tool_call_id"] for m in results], ["missing", "write"])
        self.assertIn("error", json.loads(results[0]["content"]))
        self.assertEqual((self.workdir / "exists.txt").read_text(), "ok")

    def test_real_shell_returns_unicode_stderr_and_nonzero_exit(self):
        result = json.loads(
            homebrew_harness.tools.shell.run_bash(
                "printf '你好'; printf 'failed' >&2; exit 7", self.workdir
            )
        )
        self.assertEqual(result, {"exit_code": 7, "stdout": "你好", "stderr": "failed"})

    def test_real_shell_uses_requested_working_directory(self):
        homebrew_harness.tools.shell.run_bash(
            "printf 'hello' > result.txt", self.workdir
        )
        self.assertEqual((self.workdir / "result.txt").read_text(), "hello")

    def test_shell_does_not_inherit_model_credentials(self):
        with patch.dict(
            os.environ,
            {
                name: "private-test-value"
                for name in homebrew_harness.configuration.PRIVATE_ENV_VARIABLES
            },
        ):
            result = json.loads(
                homebrew_harness.tools.shell.run_bash(
                    "printf '%s' \"${OPENAI_API_KEY-unset}:${OPENAI_BASE_URL-unset}:${DASHSCOPE_API_KEY-unset}\"",
                    self.workdir,
                )
            )
        self.assertEqual(result["stdout"], "unset:unset:unset")

    def test_shell_timeout_is_a_tool_error(self):
        # Bash builtins only: this timeout test leaves no child processes behind.
        result = json.loads(
            homebrew_harness.tools.shell.run_bash(
                "while :; do :; done", self.workdir, timeout=0.05
            )
        )
        self.assertIn("timed out", result["error"])

    def test_output_is_bounded(self):
        result = json.loads(
            homebrew_harness.tools.shell.run_bash("printf '%12000s' x", self.workdir)
        )
        self.assertIn("[output truncated]", result["stdout"])
        self.assertLess(len(result["stdout"]), 10_100)


class ConfigurationTests(unittest.TestCase):
    def test_missing_configuration_does_not_construct_client(self):
        error_output = io.StringIO()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("homebrew_harness.cli.load_dotenv"),
            patch("homebrew_harness.cli.OpenAI") as client,
            redirect_stderr(error_output),
        ):
            self.assertEqual(
                homebrew_harness.cli.main(["--profile", "full"] + ["test task"]), 1
            )
        client.assert_not_called()
        self.assertIn("Missing configuration", error_output.getvalue())

    def test_provider_error_body_is_not_printed(self):
        config = {
            name: "private-test-value"
            for name in homebrew_harness.configuration.CONFIG_VARIABLES
        }
        error = APIError(
            "private-test-value", request=Mock(), body={"detail": "private-test-value"}
        )
        error_output = io.StringIO()
        with (
            patch.dict(os.environ, config),
            patch("homebrew_harness.cli.load_dotenv"),
            patch("homebrew_harness.cli.OpenAI", side_effect=error),
            redirect_stderr(error_output),
        ):
            self.assertEqual(
                homebrew_harness.cli.main(["--profile", "full"] + ["test task"]), 1
            )
        self.assertIn("APIError", error_output.getvalue())
        self.assertNotIn("private-test-value", error_output.getvalue())


if __name__ == "__main__":
    unittest.main()
