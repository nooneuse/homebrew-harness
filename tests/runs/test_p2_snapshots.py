import os
from pathlib import Path
import unittest

from homebrew_harness.execution.source_snapshot import (
    snapshot,
    prefix_patterns,
    TestRunnerError,
)
from homebrew_harness.runs import RunSpec
from tests.runs import test_managed_team as fixture
from tests.runs import test_p1_inputs as inputs


class NarrowSnapshotTests(unittest.TestCase):
    setUp = fixture.ManagedTeamTests.setUp
    setup_run = fixture.ManagedTeamTests.setup_run
    actor_store = inputs.InputDeliveryTests.actor_store

    def large_repository(self):
        directory = self.root / "unrelated"
        directory.mkdir()
        for index in range(5001):
            (directory / ("file" + str(index) + ".txt")).write_text("unrelated content")
        import subprocess

        for args in (("add", "unrelated"), ("commit", "-qm", "large fixture")):
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=fixture",
                    "-c",
                    "user.email=fixture@invalid",
                    "-c",
                    "commit.gpgsign=false",
                    "-c",
                    "core.hooksPath=/dev/null",
                    *args,
                ],
                cwd=self.root,
                check=True,
                capture_output=True,
            )

    def test_large_repository_runs_real_dependent_team_with_small_content_limit(self):
        self.large_repository()
        original = self.setup_run

        def narrow(spec):
            data = spec.data()
            data["total_seconds"] = 120
            data["snapshot_limits"] = {
                "max_files": 8,
                "max_bytes": 1024,
                "max_paths": 10000,
            }
            return original(RunSpec.from_dict(data))

        self.setup_run = narrow
        inputs.InputDeliveryTests.test_full_dependent_team_reads_handoff_and_publishes_both_outputs(
            self
        )

    def prepared(self):
        c, s, _ = self.setup_run(inputs.input_spec())
        c.prepare()
        s.intent.update(c.spec.prompt, new_task=True)
        own = self.actor_store(c, s, "alpha")
        task = own.claim(c.team_control.task_for("alpha").id)
        return c, s, own.workspace(task), task

    def test_outside_scope_edit_is_detected_even_with_same_size_and_mtime(self):
        self.large_repository()
        c, s, root, task = self.prepared()
        path = root / "unrelated/file0.txt"
        before = path.stat()
        path.write_text("different content")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.assertRaisesRegex(ValueError, "outside"):
            c.transfers._changes(task)

    def test_ignored_files_cannot_hide_new_outside_scope_outputs(self):
        c, s, root, task = self.prepared()
        # Git excludes this file; the metadata witness must still detect it.
        excluded = Path(
            c.session.workspace.worktrees._git(
                ["rev-parse", "--git-path", "info/exclude"], cwd=root
            ).stdout.strip()
        )
        if not excluded.is_absolute():
            excluded = root / excluded
        excluded.write_text("unexpected.txt\n")
        (root / "unexpected.txt").write_text("unexpected output")
        status = c.session.workspace.worktrees._git(
            ["status", "--porcelain", "--untracked-files=all"], cwd=root
        ).stdout
        self.assertNotIn("unexpected.txt", status)
        with self.assertRaisesRegex(ValueError, "outside"):
            c.transfers._changes(task)

    def test_index_flags_and_symlinks_are_refused(self):
        c, s, root, task = self.prepared()
        c.session.workspace.worktrees._git(
            ["update-index", "--assume-unchanged", "beta.txt"], cwd=root
        )
        with self.assertRaisesRegex(TestRunnerError, "assume-unchanged"):
            c.transfers._changes(task)
        c.session.workspace.worktrees._git(
            ["update-index", "--no-assume-unchanged", "beta.txt"], cwd=root
        )
        (root / "outside-link").symlink_to(root / "beta.txt")
        with self.assertRaises(TestRunnerError):
            c.transfers._changes(task)

    def test_declared_large_source_still_requires_explicit_content_limits(self):
        self.large_repository()
        data = inputs.input_spec().data()
        data["stages"][0]["source_paths"] = ["unrelated"]
        c, s, _ = self.setup_run(RunSpec.from_dict(data))
        with self.assertRaisesRegex(TestRunnerError, "max_files"):
            c.prepare()

    def test_literal_prefixes_and_inventory_limits_are_explicit(self):
        (self.root / "literal[0].txt").write_text("literal")
        current = snapshot(self.root, prefix_patterns(("literal[0].txt",)))
        self.assertEqual(set(current), {"literal[0].txt"})
        with self.assertRaisesRegex(TestRunnerError, "max_paths"):
            snapshot(self.root, prefix_patterns(("literal[0].txt",)), max_paths=1)
        for limits in ({"max_files": True}, {"max_bytes": -1}, {"unexpected": 2}):
            with self.assertRaises(ValueError):
                RunSpec("Task", snapshot_limits=limits)

    def test_declared_read_only_source_change_invalidates_accepted_evidence(self):
        data = inputs.input_spec().data()
        data["stages"][0]["source_paths"] = ["beta.txt"]
        c, s, _ = self.setup_run(RunSpec.from_dict(data))
        c.prepare()
        s.intent.update(c.spec.prompt, new_task=True)
        own = self.actor_store(c, s, "alpha")
        task = own.claim(c.team_control.task_for("alpha").id)
        root = own.workspace(task)
        (root / "alpha.txt").write_text("alpha done")
        accepted, _ = own.complete(task.id)
        self.assertTrue(c.stages.valid(accepted))
        (root / "beta.txt").write_text("changed source dependency")
        self.assertFalse(c.stages.valid(accepted))

    def test_single_config_read_keeps_worktree_filters_and_inactive_includes_blocked(
        self,
    ):
        import subprocess
        from unittest.mock import patch
        from homebrew_harness.execution.background import ShellRunner
        from homebrew_harness.orchestration.worktrees import WorktreeManager

        manager = WorktreeManager(self.root, None)
        original = ShellRunner.run
        queries = []

        def record(runner, command, *args, **kwargs):
            if "config" in command:
                queries.append(command)
            return original(runner, command, *args, **kwargs)

        with patch.object(ShellRunner, "run", record):
            manager._git(["status", "--porcelain"])
        self.assertEqual(len(queries), 1)
        self.assertIn("--no-includes", queries[0])
        self.assertIn("--get-regexp", queries[0])
        manager._git(["config", "--local", "extensions.worktreeConfig", "true"])
        manager._git(["config", "--worktree", "filter.fixture.process", "false"])
        with self.assertRaisesRegex(ValueError, "checkout filters"):
            manager._git(["status", "--porcelain"])
        subprocess.run(
            ["git", "config", "--worktree", "--unset", "filter.fixture.process"],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        manager._git(
            ["config", "--local", "includeIf.onbranch:inactive.path", "missing"]
        )
        with self.assertRaisesRegex(ValueError, "config includes"):
            manager._git(["status", "--porcelain"])
