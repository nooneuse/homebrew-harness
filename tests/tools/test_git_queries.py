"""Targeted Git queries select changes independently of display limits."""

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from homebrew_harness import HarnessSession
from homebrew_harness.tools.queries import git_diff, git_status
from tests.support.clients import call, fake_client, reply


class GitQueryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.git("init", "-q")

    def git(self, *args):
        return subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                *args,
            ],
            cwd=self.root,
            env={
                **os.environ,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
            },
            capture_output=True,
            text=True,
            check=True,
        )

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")

    def test_explicit_last_file_diff_through_dispatch_after_display_cutoff(self):
        for n in range(220):
            (self.root / f"file{n:03d}.txt").write_text("before\n")
        self.commit()
        for n in range(220):
            (self.root / f"file{n:03d}.txt").write_text("after\n")
        self.assertEqual(len(json.loads(git_status(workdir=self.root))["files"]), 200)
        client, snapshots = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "diff",
                            name="git_diff",
                            arguments='{"paths":["file219.txt"]}',
                        )
                    ]
                ),
                reply("Done"),
            ]
        )
        with (
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
            HarnessSession(client, "fixture", self.root) as session,
        ):
            session.run_user("Inspect this change")
        self.assertTrue(session.wait_closed(3))
        result = json.loads(
            next(m["content"] for m in snapshots[-1] if m["role"] == "tool")
        )
        self.assertIn("+after", result["diff"])
        self.assertFalse(result["truncated"])
        self.assertTrue(json.loads(git_diff(workdir=self.root))["truncated"])

    def test_untracked_noise_does_not_block_literal_tracked_path(self):
        path = "z [literal]* name.txt"
        (self.root / path).write_text("before\n")
        self.commit()
        (self.root / path).write_text("after\n")
        for n in range(300):
            (self.root / f"noise{n:03d}").write_text("untracked")
        result = json.loads(git_diff(workdir=self.root, paths=[path]))
        self.assertIn("+after", result["diff"])
        self.assertFalse(result["truncated"])
        untracked = json.loads(git_diff(workdir=self.root, paths=["noise299"]))
        self.assertEqual(untracked["untracked"], ["noise299"])
        self.assertFalse(untracked["truncated"])
        mixed = json.loads(git_diff(workdir=self.root, paths=[path, "noise299"]))
        self.assertIn("+after", mixed["diff"])
        self.assertEqual(mixed["untracked"], ["noise299"])

    def test_rename_original_can_be_selected_but_private_origin_cannot(self):
        (self.root / "before.txt").write_text("public marker\n")
        private = self.root / ".harness/protocol-state/hidden.txt"
        private.parent.mkdir(parents=True)
        private.write_text("private marker\n")
        self.commit()
        self.git("mv", "before.txt", "after.txt")
        self.git("mv", ".harness/protocol-state/hidden.txt", "exposed.txt")
        for selected in (["before.txt"], ["after.txt"]):
            result = json.loads(git_diff(workdir=self.root, paths=selected))
            self.assertIn("public marker", result["diff"])
            self.assertFalse(result["truncated"])
        self.assertNotIn(
            "private marker", git_diff(workdir=self.root, paths=["exposed.txt"])
        )

    def test_incomplete_metadata_is_not_a_negative_diff(self):
        (self.root / "one.txt").write_text("before")
        (self.root / "two.txt").write_text("before")
        self.commit()
        (self.root / "one.txt").write_text("after")
        (self.root / "two.txt").write_text("after")
        with (
            patch("homebrew_harness.tools.queries.GIT_METADATA_LIMIT", 1),
            self.assertRaisesRegex(ValueError, "metadata"),
        ):
            git_diff(workdir=self.root, paths=["two.txt"])
