"""Real post-commit file failures must stop the tool batch and retain facts."""

import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch
from homebrew_harness import HarnessSession
from homebrew_harness.runtime.runtime_state import RecoveryRequired
from homebrew_harness.tools.files import run_write
from homebrew_harness.tools.tool_outcome import ToolExecutionError
from tests.support.clients import fake_client, reply, call


class PublicationOutcomeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        for capture in (redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            capture.__enter__()
            self.addCleanup(capture.__exit__, None, None, None)

    def execute(self, edit=False):
        if edit:
            (self.root / "first.txt").write_text("before")
        args = (
            {"path": "first.txt", "old_text": "before", "new_text": "committed"}
            if edit
            else {"path": "first.txt", "content": "committed"}
        )
        client, _ = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "first",
                            name="edit_file" if edit else "write_file",
                            arguments=json.dumps(args),
                        ),
                        call(
                            "next",
                            name="write_file",
                            arguments='{"path":"next.txt","content":"must not run"}',
                        ),
                    ]
                )
            ]
        )
        session = HarnessSession(client, "fixture", self.root)
        self.addCleanup(lambda: session.wait_closed(5))
        self.addCleanup(session.close)
        original = os.fsync
        fault = []
        identity = self.root.stat().st_ino

        def fail(fd):
            info = os.fstat(fd)
            if (
                stat.S_ISDIR(info.st_mode)
                and info.st_ino == identity
                and (self.root / "first.txt").read_text() == "committed"
                and not fault
            ):
                fault.append(True)
                raise OSError("fixture directory fsync")
            return original(fd)

        with patch("os.fsync", fail), self.assertRaises(RecoveryRequired):
            session.run_user("Write fixture")
        self.assertEqual((self.root / "first.txt").read_text(), "committed")
        self.assertFalse((self.root / "next.txt").exists())
        first = json.loads(
            next(
                m["content"]
                for m in session.messages
                if m.get("tool_call_id") == "first"
            )
        )
        self.assertEqual(first["outcome"], "unknown")
        self.assertTrue(first["details"]["published"])
        self.assertEqual(len(first["details"]["sha256"]), 64)
        self.assertEqual(len(session.recovery_operations()), 1)

    def test_new_file_fsync_failure_stops_following_tool(self):
        self.execute()

    def test_replacement_fsync_failure_stops_following_tool(self):
        self.execute(edit=True)

    def test_failure_before_publication_remains_plain_os_error(self):
        with (
            patch("os.fchmod", side_effect=OSError("fixture prepare")),
            self.assertRaises(OSError) as caught,
        ):
            run_write("file", "content", self.root)
        self.assertNotIsInstance(caught.exception, ToolExecutionError)
        self.assertFalse((self.root / "file").exists())

    def test_post_commit_temp_cleanup_failure_is_partial(self):
        original = os.unlink

        def fail(path, *args, **kwargs):
            if (
                str(path).startswith(".harness-write-")
                and (self.root / "file").exists()
            ):
                raise OSError("fixture temp cleanup")
            return original(path, *args, **kwargs)

        with patch("os.unlink", fail), self.assertRaises(ToolExecutionError) as caught:
            run_write("file", "content", self.root)
        self.assertEqual(caught.exception.outcome, "partial")
        self.assertTrue(caught.exception.details["durability_confirmed"])
        self.assertEqual((self.root / "file").read_text(), "content")
