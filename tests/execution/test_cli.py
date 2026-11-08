"""The unified CLI selects one execution path without help-time side effects."""

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from homebrew_harness.cli import main, parser
from tests.support.paths import SRC_ROOT


class UnifiedCLITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_no_subcommand_dispatches_interactive_session_with_defaults(self):
        with patch("homebrew_harness.session_cli.execute", return_value=7) as execute:
            self.assertEqual(main([]), 7)
        args, command = execute.call_args.args
        self.assertEqual(args.entrypoint, "session")
        self.assertIsNone(args.prompt)
        self.assertEqual(args.cwd, Path.cwd())
        self.assertEqual(args.max_turns, 20)
        self.assertEqual(command.prog, "homebrew-harness session")

    def test_default_session_preserves_explicit_shared_locations(self):
        with patch("homebrew_harness.session_cli.execute", return_value=0) as execute:
            self.assertEqual(
                main(["--cwd", str(self.root), "--env-file", "host.env"]), 0
            )
        args = execute.call_args.args[0]
        self.assertIsNone(args.prompt)
        self.assertEqual(args.cwd, self.root)
        self.assertEqual(args.env_file, Path("host.env"))

    def test_explicit_session_dispatches_one_shot_and_its_options(self):
        with patch("homebrew_harness.session_cli.execute", return_value=2) as execute:
            self.assertEqual(main(["session", "--max-turns", "3", "task"]), 2)
        args = execute.call_args.args[0]
        self.assertEqual(args.prompt, "task")
        self.assertEqual(args.max_turns, 3)

    def test_common_locations_survive_each_subparser_default(self):
        for operation in (
            ["session"],
            ["run", "status", "example"],
            ["workflow", "list"],
            ["admin", "storage-status"],
        ):
            for before, after in ((True, False), (False, True), (True, True)):
                with self.subTest(operation=operation, before=before, after=after):
                    prefix = (
                        ["--cwd", "first", "--env-file", "first.env"] if before else []
                    )
                    suffix = (
                        ["--cwd", "last", "--env-file", "last.env"] if after else []
                    )
                    args = parser().parse_args(prefix + operation + suffix)
                    self.assertEqual(args.cwd, Path("last" if after else "first"))
                    self.assertEqual(
                        args.env_file, Path("last.env" if after else "first.env")
                    )

    def test_common_locations_work_between_group_and_operation(self):
        args = parser().parse_args(
            ["--cwd", "root", "run", "--cwd", "group", "status", "example"]
        )
        self.assertEqual(args.cwd, Path("group"))
        args = parser().parse_args(
            [
                "run",
                "--env-file",
                "group.env",
                "status",
                "example",
                "--env-file",
                "operation.env",
            ]
        )
        self.assertEqual(args.env_file, Path("operation.env"))

    def test_feature_dispatch_preserves_operation_and_group_options(self):
        for argv, target in (
            (
                [
                    "run",
                    "--models-config",
                    "models.json",
                    "start",
                    "--spec",
                    "run.json",
                ],
                "runs.cli",
            ),
            (["workflow", "--timeout-seconds", "5", "list"], "workflow_cli"),
            (["admin", "checkpoint-retire-preview", "--keep-recent", "7"], "admin"),
        ):
            with (
                self.subTest(argv=argv),
                patch(
                    "homebrew_harness." + target + ".execute", return_value=0
                ) as execute,
            ):
                self.assertEqual(main(argv), 0)
                self.assertEqual(execute.call_args.args[0].entrypoint, argv[0])
                self.assertFalse(hasattr(execute.call_args.args[0], "prompt"))

    def test_unsupported_root_and_cross_feature_options_are_errors(self):
        for argv in (
            ["a task"],
            ["--profile", "full"],
            ["run"],
            ["workflow"],
            ["admin"],
            ["admin", "gc-preview", "--live"],
            ["workflow", "--resume-session"],
        ):
            with (
                self.subTest(argv=argv),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as error,
            ):
                parser().parse_args(argv)
            self.assertEqual(error.exception.code, 2)

    def test_session_can_receive_a_prompt_named_like_a_subcommand(self):
        args = parser().parse_args(["session", "--", "run"])
        self.assertEqual(args.prompt, "run")

    def test_admin_help_only_lists_relevant_options(self):
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as error:
            main(["admin", "storage-status", "--help"])
        self.assertEqual(error.exception.code, 0)
        self.assertNotIn("--live", output.getvalue())
        self.assertNotIn("--keep-recent", output.getvalue())
        self.assertIn("--cwd", output.getvalue())

    def subprocess_cli(self, *argv):
        code = (
            "import sys;sys.path.insert(0,"
            + repr(str(SRC_ROOT))
            + ");from homebrew_harness.cli import main;raise SystemExit(main())"
        )
        return subprocess.run(
            [sys.executable, "-I", "-B", "-c", code, *argv],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )

    def test_fresh_help_loads_no_sdk_or_runtime_and_changes_no_workspace(self):
        code = """import json,sys
sys.path.insert(0, SOURCE)
from homebrew_harness.cli import parser
p=parser()
print(json.dumps([m for m in sys.modules if m.startswith(('openai', 'httpx', 'dotenv', 'homebrew_harness.execution', 'homebrew_harness.storage', 'homebrew_harness.runtime.session'))]))
""".replace("SOURCE", repr(str(SRC_ROOT)))
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", code],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        self.assertEqual(json.loads(result.stdout), [])
        for argv in (
            [],
            ["session"],
            ["run"],
            ["run", "start"],
            ["workflow"],
            ["workflow", "resume"],
            ["admin"],
            ["admin", "benchmark"],
        ):
            with self.subTest(argv=argv):
                result = self.subprocess_cli(*argv, "--help")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("homebrew-harness", result.stdout)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_offline_commands_need_no_environment_or_workspace_state(self):
        for argv in (
            ["workflow", "list"],
            ["admin", "storage-status"],
            ["run", "status", "missing"],
        ):
            with self.subTest(argv=argv):
                result = self.subprocess_cli("--env-file", "nonexistent.env", *argv)
                self.assertEqual(
                    result.returncode, 1 if argv[0] == "run" else 0, result.stderr
                )
                self.assertNotIn("Missing configuration", result.stderr)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_private_worker_keeps_required_identity_and_host_config_flags(self):
        import homebrew_harness.runs.worker as worker

        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            worker.main(["--cwd", "workspace", "--run-id", "id"])
        self.assertEqual(error.exception.code, 2)
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as error:
            worker.main(["--help"])
        self.assertEqual(error.exception.code, 0)
        for flag in (
            "--cwd",
            "--run-id",
            "--owner",
            "--generation",
            "--env-file",
            "--models-config",
            "--test-config",
            "--bash-config",
        ):
            self.assertIn(flag, output.getvalue())
