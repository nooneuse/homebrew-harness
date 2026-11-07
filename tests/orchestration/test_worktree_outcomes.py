"""Real checkout effects must reach the shared inspection gate with exact facts."""

import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

import homebrew_harness.context.compaction
from homebrew_harness.runtime.capabilities import Capabilities
from homebrew_harness.storage.durable_runtime import RecoveryRequired
from homebrew_harness.runtime.session import HarnessSession
from tests.support.clients import fake_client, reply, call
from homebrew_harness.tools.tool_outcome import ToolExecutionError


class WorktreeOutcomeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        for context in (redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)
        self.git("init", "-q")
        (self.root / ".gitignore").write_text(".harness/\n.tasks/\n.worktrees/\n")
        (self.root / "source.txt").write_text("fixture")
        self.git("add", ".")
        self.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        )

    def git(self, *args):
        return subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args],
            cwd=self.root,
            env={
                "PATH": os.defpath,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
            },
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )

    def session(self, **kwargs):
        client, _ = fake_client([])
        session = HarnessSession(
            client,
            "fixture",
            self.root,
            shutdown_timeout=0.05,
            capabilities=Capabilities({"worktrees"}),
            **kwargs,
        )
        self.addCleanup(lambda: session.wait_closed(3))
        self.addCleanup(session.close)
        session.run_user("/usage")
        return session, client

    def prepare(self, session, client, *, name="fixture", later="write_file"):
        task = session.workspace.store.create("Fixture")
        second = call(
            "later",
            name=later,
            arguments='{"path":"later.txt","content":"unexpected"}'
            if later == "write_file"
            else '{"path":"source.txt"}',
        )
        client.chat.completions.create.side_effect = [
            reply(
                calls=[
                    call(
                        "wt",
                        name="create_worktree",
                        arguments=json.dumps({"name": name, "task_id": task.id}),
                    ),
                    second,
                ]
            ),
            reply("done"),
        ]
        return task

    def result(self, session):
        return json.loads(
            next(
                message["content"]
                for message in session.messages
                if message.get("tool_call_id") == "wt"
            )
        )

    def assert_gate(self, session, client):
        self.assertFalse((self.root / "later.txt").exists())
        self.assertEqual(client.chat.completions.create.call_count, 1)
        later = json.loads(
            next(
                message["content"]
                for message in session.messages
                if message.get("tool_call_id") == "later"
            )
        )
        self.assertFalse(later["executed"])
        self.assertTrue(session.recovery_operations())
        homebrew_harness.context.compaction.ContextCompactor.groups(session.messages)

    def test_binding_failure_before_commit_retains_checkout_and_blocks_batch(self):
        session, client = self.session()
        task = self.prepare(session, client)
        original = session.workspace.store._write

        def write(value):
            if value.worktree is not None:
                raise OSError("fixture before binding publication")
            return original(value)

        with patch.object(session.workspace.store, "_write", write):
            with self.assertRaises(RecoveryRequired):
                session.run_user("Create the fixture checkout")
        output = self.result(session)
        self.assertEqual(output["outcome"], "unknown")
        self.assertEqual(output["details"]["binding"], "unknown")
        self.assertEqual(output["details"]["phase"], "binding")
        self.assertEqual(output["details"]["task_id"], task.id)
        self.assertEqual(output["details"]["checkout"], "verified")
        self.assertTrue((self.root / ".worktrees/fixture/source.txt").exists())
        self.assertIsNone(session.workspace.store.load(task.id).worktree)
        self.assert_gate(session, client)

    def test_binding_error_after_commit_never_claims_the_task_is_unbound(self):
        session, client = self.session()
        task = self.prepare(session, client)
        original = session.workspace.store._write

        def write(value):
            original(value)
            if value.worktree is not None:
                raise OSError("fixture after binding publication")

        with patch.object(session.workspace.store, "_write", write):
            with self.assertRaises(RecoveryRequired):
                session.run_user("Create the fixture checkout")
        self.assertEqual(session.workspace.store.load(task.id).worktree, "fixture")
        output = self.result(session)
        self.assertEqual(output["outcome"], "unknown")
        self.assertEqual(output["details"]["binding"], "unknown")
        self.assertNotIn("unbound", output["error"])
        self.assert_gate(session, client)

    def test_git_nonzero_with_retained_artifacts_reports_partial_without_binding(self):
        session, client = self.session()
        task = self.prepare(session, client)
        original = session.workspace.worktrees._git

        def git(args, **kwargs):
            result = original(args, **kwargs)
            if args[:2] == ["worktree", "add"]:
                result.returncode = 1
            return result

        with patch.object(session.workspace.worktrees, "_git", git):
            with self.assertRaises(RecoveryRequired):
                session.run_user("Create fixture")
        output = self.result(session)
        self.assertEqual(output["outcome"], "partial")
        self.assertEqual(output["details"]["binding"], "not_attempted")
        self.assertTrue(output["details"]["observed"]["path_exists"])
        self.assertIsNone(session.workspace.store.load(task.id).worktree)
        self.assert_gate(session, client)

    def test_accepted_git_exception_retains_unknown_phase_and_identity(self):
        session, client = self.session()
        task = self.prepare(session, client)
        original = session.workspace.worktrees._git

        def git(args, **kwargs):
            result = original(args, **kwargs)
            if args[:2] == ["worktree", "add"]:
                raise OSError("fixture lost result after accepted Git")
            return result

        with patch.object(session.workspace.worktrees, "_git", git):
            with self.assertRaises(RecoveryRequired):
                session.run_user("Create fixture")
        output = self.result(session)
        self.assertEqual(output["outcome"], "unknown")
        self.assertEqual(output["details"]["phase"], "checkout")
        self.assertEqual(output["details"]["task_id"], task.id)
        self.assertEqual(output["details"]["binding"], "not_attempted")
        self.assertTrue((self.root / ".worktrees/fixture").exists())
        self.assert_gate(session, client)

    def test_no_artifact_git_failure_keeps_ordinary_error_and_allows_read(self):
        session, client = self.session()
        self.prepare(session, client, later="read_file")
        original = session.workspace.worktrees._git

        def git(args, **kwargs):
            if args[:2] == ["worktree", "add"]:
                return subprocess.CompletedProcess(args, 1, "", "fixture failure")
            return original(args, **kwargs)

        with patch.object(session.workspace.worktrees, "_git", git):
            self.assertEqual(session.run_user("Create fixture"), "done")
        self.assertNotIn("outcome", self.result(session))
        self.assertFalse((self.root / ".worktrees/fixture").exists())
        self.assertFalse(session._recovery_blocked)
        self.assertEqual(
            json.loads(
                next(
                    m["content"]
                    for m in session.messages
                    if m.get("tool_call_id") == "later"
                )
            )["content"],
            "fixture",
        )

    def test_dirty_workspace_and_invalid_name_are_zero_effect_errors(self):
        for name, dirty in (("../escape", False), ("fixture", True)):
            with self.subTest(name=name):
                session, client = self.session()
                self.prepare(session, client, name=name, later="read_file")
                if dirty:
                    (self.root / "source.txt").write_text("modified")
                session.run_user("Create fixture")
                self.assertNotIn("outcome", self.result(session))
                self.assertFalse(session._recovery_blocked)
                self.assertFalse((self.root / ".worktrees/fixture").exists())
                session.close()
                self.assertTrue(session.wait_closed(3))

    def test_unknown_result_survives_restart_and_inspection_retains_original_details(
        self,
    ):
        first, client = self.session()
        task = self.prepare(first, client)
        original = first.workspace.store._write

        def write(value):
            if value.worktree is not None:
                raise OSError("fixture binding failure")
            return original(value)

        with patch.object(first.workspace.store, "_write", write):
            with self.assertRaises(RecoveryRequired):
                first.run_user("Create fixture")
        row = first.recovery_operations()[0]
        details = self.result(first)["details"]
        sid = first.session_id
        first.close()
        self.assertTrue(first.wait_closed(3))
        restored, second = self.session(resume_session=sid)
        with self.assertRaises(RecoveryRequired):
            restored.run_user("/recovery continue")
        restored.run_user(
            "/recovery inspect " + row["operation_id"] + " " + str(row["revision"])
        )
        restored.run_user("/recovery continue")
        stored = restored.runtime_store.get("operations", row["operation_id"])
        self.assertEqual(stored["state"], "inspected")
        self.assertEqual(json.loads(stored["output"])["outcome"], "unknown")
        self.assertEqual(json.loads(stored["output"])["details"], details)
        self.assertTrue((self.root / ".worktrees/fixture/source.txt").exists())
        self.assertIsNone(restored.workspace.store.load(task.id).worktree)
        second.chat.completions.create.assert_not_called()

    def test_git_launch_failure_is_known_zero_effect_and_allows_read(self):
        session, client = self.session()
        self.prepare(session, client, later="read_file")
        original = subprocess.Popen

        def popen(arguments, *args, **kwargs):
            if (
                isinstance(arguments, list)
                and "worktree" in arguments
                and "add" in arguments
            ):
                raise OSError("fixture before Popen accepted the command")
            return original(arguments, *args, **kwargs)

        with patch("homebrew_harness.execution.background.subprocess.Popen", popen):
            self.assertEqual(session.run_user("Create fixture"), "done")
        self.assertNotIn("outcome", self.result(session))
        self.assertFalse((self.root / ".worktrees/fixture").exists())
        self.assertFalse(session._recovery_blocked)
        self.assertEqual(
            json.loads(
                next(
                    m["content"]
                    for m in session.messages
                    if m.get("tool_call_id") == "later"
                )
            )["content"],
            "fixture",
        )

    def test_artifact_access_failure_after_git_is_unknown_and_blocks_batch(self):
        session, client = self.session()
        self.prepare(session, client)
        original = session.workspace.worktrees._git
        exists = Path.exists
        finished = []
        target = self.root / ".worktrees/fixture"

        def git(args, **kwargs):
            result = original(args, **kwargs)
            if args[:2] == ["worktree", "add"]:
                result.returncode = 1
                finished.append(True)
            return result

        def denied(path):
            if path == target and finished:
                raise PermissionError("fixture artifact access unavailable")
            return exists(path)

        with (
            patch.object(session.workspace.worktrees, "_git", git),
            patch.object(Path, "exists", denied),
        ):
            with self.assertRaises(RecoveryRequired):
                session.run_user("Create fixture")
        output = self.result(session)
        self.assertEqual(output["outcome"], "unknown")
        self.assertEqual(output["details"]["error_type"], "PermissionError")
        self.assertEqual(output["details"]["observed"], {})
        self.assertTrue(target.exists())
        self.assert_gate(session, client)
