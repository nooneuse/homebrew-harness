"""Permission decisions and side-effect boundaries; dangerous commands are never run."""

from __future__ import annotations
from homebrew_harness.runtime.capabilities import Capabilities

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.support.tools import execute_fixture_tool
import homebrew_harness.runtime.loop
import homebrew_harness.security.permissions
import homebrew_harness.tools.catalog
import homebrew_harness.tools.registry
import homebrew_harness.security.permissions as permissions
from tests.support.clients import conversation, call, fake_client, reply


def tool_call(name, arguments):
    return SimpleNamespace(
        type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.workdir = self.root / "workspace"
        self.workdir.mkdir()
        self.errors = io.StringIO()

    def invoke(self, name, arguments):
        with redirect_stderr(self.errors):
            return json.loads(
                execute_fixture_tool(tool_call(name, arguments), self.workdir)
            )

    def test_file_operations_inside_workspace_need_no_approval(self):
        with patch("homebrew_harness.security.permissions.ask_user") as ask:
            self.assertNotIn(
                "error",
                self.invoke("write_file", {"path": "note.txt", "content": "before"}),
            )
            self.assertNotIn(
                "error",
                self.invoke(
                    "edit_file",
                    {"path": "note.txt", "old_text": "before", "new_text": "after"},
                ),
            )
            self.assertEqual(
                self.invoke("read_file", {"path": "note.txt"})["content"], "after"
            )
            self.assertEqual(
                self.invoke("glob", {"pattern": "*.txt"})["paths"], ["note.txt"]
            )
        ask.assert_not_called()

    def test_outside_paths_are_denied_before_handler_or_approval(self):
        (self.root / "outside.txt").write_text("unchanged")
        (self.workdir / "linked.txt").symlink_to(self.root / "outside.txt")
        for path in ("../outside.txt", "linked.txt"):
            for name, arguments in (
                ("read_file", {}),
                ("write_file", {"content": "changed"}),
                ("edit_file", {"old_text": "unchanged", "new_text": "changed"}),
            ):
                handler = Mock()
                with (
                    self.subTest(path=path, tool=name),
                    patch.dict(
                        homebrew_harness.tools.registry.TOOL_HANDLERS, {name: handler}
                    ),
                    patch("homebrew_harness.security.permissions.ask_user") as ask,
                ):
                    result = self.invoke(name, {"path": path, **arguments})
                    self.assertEqual(result["permission"], "deny")
                    handler.assert_not_called()
                    ask.assert_not_called()
        self.assertEqual((self.root / "outside.txt").read_text(), "unchanged")

    def test_glob_escape_is_a_permission_denial(self):
        with patch("homebrew_harness.security.permissions.ask_user") as ask:
            self.assertEqual(
                self.invoke("glob", {"pattern": "../*"})["permission"], "deny"
            )
        ask.assert_not_called()

    def test_hard_denials_never_offer_approval_or_execute(self):
        commands = (
            "rm -rf /",
            "rm -r -f /",
            "/bin/rm -rf -- '/'",
            "rm -rf /*",
            "sudo true",
            "/usr/bin/sudo -n true",
            "echo ready; reboot",
            "shutdown now",
            "mkfs.ext4 /dev/example",
            "dd if=input of=output",
            "printf data > /dev/sda",
            "printf data > /dev/disk2",
        )
        handler = Mock()
        with (
            patch.dict(
                homebrew_harness.tools.registry.TOOL_HANDLERS, {"bash": handler}
            ),
            patch(
                "homebrew_harness.security.permissions.ask_user",
                return_value=permissions.PermissionDecision("allow", "test"),
            ) as ask,
        ):
            for command in commands:
                with self.subTest(command=command):
                    result = self.invoke("bash", {"command": command})
                    self.assertEqual(result["permission"], "deny")
            handler.assert_not_called()
            ask.assert_not_called()

    def test_nonmatching_shell_commands_still_require_approval(self):
        for command in (
            "pwd",
            "echo sudo",
            "printf reboot",
            "rm -rf /tmp/example",
            "python3 script.py",
            "sh -c 'printf hello'",
        ):
            with self.subTest(command=command):
                self.assertIsNone(permissions.check_deny_list(command))
                decision = permissions.check_rules(
                    "bash", {"command": command}, self.workdir
                )
                self.assertEqual(decision.action, "ask")

    def test_yes_allows_the_exact_command_and_displays_request(self):
        command = "printf approved > approved.txt"
        with (
            patch(
                "homebrew_harness.security.permissions.sys.stdin.isatty",
                return_value=True,
            ),
            patch("builtins.input", return_value="yes") as ask,
        ):
            result = self.invoke("bash", {"command": command})
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual((self.workdir / "approved.txt").read_text(), "approved")
        ask.assert_called_once()
        self.assertIn(json.dumps(command), self.errors.getvalue())
        self.assertIn(str(self.workdir), self.errors.getvalue())

    def test_approval_is_not_reused_for_the_next_call(self):
        handler = Mock(return_value='{"exit_code": 0}')
        with (
            patch.dict(
                homebrew_harness.tools.registry.TOOL_HANDLERS, {"bash": handler}
            ),
            patch(
                "homebrew_harness.security.permissions.sys.stdin.isatty",
                return_value=True,
            ),
            patch("builtins.input", side_effect=["Y", "n"]) as ask,
        ):
            self.assertEqual(
                self.invoke("bash", {"command": "printf first"})["exit_code"], 0
            )
            self.assertEqual(
                self.invoke("bash", {"command": "printf second"})["permission"], "deny"
            )
        self.assertEqual(ask.call_count, 2)
        handler.assert_called_once_with(command="printf first", workdir=self.workdir)

    def test_no_blank_or_unrecognized_answer_cannot_authorize(self):
        target = self.workdir / "keep.txt"
        target.write_text("keep")
        handler = Mock()
        with (
            patch.dict(
                homebrew_harness.tools.registry.TOOL_HANDLERS, {"bash": handler}
            ),
            patch(
                "homebrew_harness.security.permissions.sys.stdin.isatty",
                return_value=True,
            ),
        ):
            for answer in ("n", "", "approve", "yes to everything"):
                with (
                    self.subTest(answer=answer),
                    patch("builtins.input", return_value=answer),
                ):
                    self.assertEqual(
                        self.invoke("bash", {"command": "rm keep.txt"})["permission"],
                        "deny",
                    )
        handler.assert_not_called()
        self.assertEqual(target.read_text(), "keep")

    def test_non_terminal_denies_without_consuming_input(self):
        with (
            patch(
                "homebrew_harness.security.permissions.sys.stdin.isatty",
                return_value=False,
            ),
            patch("builtins.input", return_value="yes") as ask,
            patch.dict(
                homebrew_harness.tools.registry.TOOL_HANDLERS, {"bash": Mock()}
            ) as handlers,
        ):
            result = self.invoke("bash", {"command": "printf hello"})
            self.assertEqual(result["permission"], "deny")
            self.assertIn("terminal", result["reason"])
            handlers["bash"].assert_not_called()
        ask.assert_not_called()

    def test_eof_during_approval_denies(self):
        with (
            patch(
                "homebrew_harness.security.permissions.sys.stdin.isatty",
                return_value=True,
            ),
            patch("builtins.input", side_effect=EOFError),
            patch.dict(
                homebrew_harness.tools.registry.TOOL_HANDLERS, {"bash": Mock()}
            ) as handlers,
        ):
            self.assertEqual(
                self.invoke("bash", {"command": "printf hello"})["permission"], "deny"
            )
            handlers["bash"].assert_not_called()

    def test_ctrl_c_propagates_without_executing_the_tool(self):
        with (
            patch(
                "homebrew_harness.security.permissions.sys.stdin.isatty",
                return_value=True,
            ),
            patch("builtins.input", side_effect=KeyboardInterrupt),
            patch.dict(
                homebrew_harness.tools.registry.TOOL_HANDLERS, {"bash": Mock()}
            ) as handlers,
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.invoke("bash", {"command": "printf hello"})
            handlers["bash"].assert_not_called()

    def test_approval_display_escapes_terminal_control_characters(self):
        command = "printf '\x1b[2J'\nprintf done"
        with (
            patch(
                "homebrew_harness.security.permissions.sys.stdin.isatty",
                return_value=True,
            ),
            patch("builtins.input", return_value="n"),
        ):
            self.invoke("bash", {"command": command})
        self.assertNotIn("\x1b", self.errors.getvalue())
        self.assertIn("\\u001b", self.errors.getvalue())
        self.assertIn("\\n", self.errors.getvalue())

    def test_invalid_arguments_do_not_trigger_permission_checks(self):
        with patch("homebrew_harness.security.permissions.check_permission") as check:
            self.assertIn("error", self.invoke("bash", {"command": 42}))
            self.assertIn("error", self.invoke("read_file", {}))
        check.assert_not_called()

    def test_new_tool_without_policy_is_denied(self):
        schema = {"type": "object", "properties": {}, "required": []}
        handler = Mock()
        with (
            patch.dict(
                homebrew_harness.tools.registry.TOOL_HANDLERS, {"new_tool": handler}
            ),
            patch.dict(
                homebrew_harness.tools.catalog.TOOL_SCHEMAS, {"new_tool": schema}
            ),
        ):
            self.assertEqual(self.invoke("new_tool", {})["permission"], "deny")
        handler.assert_not_called()

    def test_denial_is_paired_and_other_tools_in_the_batch_continue(self):
        (self.workdir / "keep.txt").write_text("keep")
        client, snapshots = fake_client(
            [
                reply(
                    calls=[
                        call("denied", "rm keep.txt"),
                        call(
                            "allowed",
                            name="read_file",
                            arguments='{"path": "keep.txt"}',
                        ),
                    ]
                ),
                reply("Deletion was denied; the file still contains keep."),
            ]
        )
        history = [{"role": "user", "content": "Inspect the file."}]
        with (
            redirect_stderr(self.errors),
            patch(
                "homebrew_harness.security.permissions.sys.stdin.isatty",
                return_value=True,
            ),
            patch("builtins.input", return_value="n"),
        ):
            result = homebrew_harness.runtime.loop.agent_loop(
                client,
                "test-model",
                history,
                self.workdir,
                capabilities=Capabilities.full(memory_mode="off"),
            )
        self.assertIn("denied", result)
        results = conversation(snapshots[1])[2:]
        self.assertEqual(
            [message["tool_call_id"] for message in results], ["denied", "allowed"]
        )
        self.assertEqual(json.loads(results[0]["content"])["permission"], "deny")
        self.assertEqual(json.loads(results[1]["content"])["content"], "keep")
        self.assertEqual((self.workdir / "keep.txt").read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
