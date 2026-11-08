"""Real temporary Git repositories exercise task binding and checkout cleanup."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.support.tools import execute_fixture_tool
import homebrew_harness.tools.registry
from homebrew_harness.orchestration.task_store import TaskStore
from homebrew_harness.orchestration.teams import TeamManager, TeammateRuntime
from tests.support.clients import call, reply
from homebrew_harness.orchestration.worktrees import WorktreeManager


class WorktreeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.git("init", "-q")
        (self.root / "README.md").write_text("Initial fixture\n")
        (self.root / ".gitignore").write_text(
            ".tasks/\n.worktrees/\n.mailboxes/\n.harness/\n"
        )
        self.git("add", "README.md", ".gitignore")
        self.git(
            "-c",
            "user.name=Harness Fixture",
            "-c",
            "user.email=harness@example.invalid",
            "commit",
            "-qm",
            "fixture",
        )
        self.store = TaskStore(self.root)
        self.manager = WorktreeManager(self.root, self.store)

    def git(self, *args, cwd=None):
        return subprocess.run(
            ["git", *args],
            cwd=cwd or self.root,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )

    def bound(self, name="feature"):
        task = self.store.create("Feature", "Edit the assigned checkout")
        result = self.manager.create(name, task.id)
        return self.store.load(task.id), Path(result["path"])

    def test_create_binds_only_after_git_registration_and_claim_uses_checkout(self):
        task, path = self.bound()
        self.assertEqual(task.worktree, "feature")
        self.assertEqual(self.manager.resolve("feature"), path)
        self.assertEqual(
            self.git("branch", "--show-current", cwd=path).stdout.strip(),
            "codex/wt/feature",
        )
        owner = TaskStore(self.root, owner="worker")
        claimed = owner.claim(task.id)
        self.assertEqual(owner.workspace(claimed), path)
        self.assertEqual(owner.load(task.id).worktree, "feature")

    def test_missing_or_rebranched_binding_blocks_claim_without_root_fallback(self):
        task, path = self.bound()
        self.git("switch", "-c", "other", cwd=path)
        with self.assertRaises(ValueError):
            TaskStore(self.root, owner="worker").claim(task.id)
        self.assertEqual(self.store.load(task.id).status, "pending")

    def test_invalid_names_existing_branches_and_symlinks_do_not_mutate_tasks(self):
        task = self.store.create("Work")
        for name in ("../outside", "bad name", ".hidden", "a..b", "/absolute"):
            with self.assertRaises(ValueError):
                self.manager.create(name, task.id)
        self.git("branch", "codex/wt/existing")
        with self.assertRaises(ValueError):
            self.manager.create("existing", task.id)
        outside = self.root / "outside"
        outside.mkdir()
        self.manager.root.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.manager.create("linked", task.id)
        self.assertEqual(self.store.load(task.id).worktree, None)
        self.assertEqual(list(outside.iterdir()), [])

    def test_git_failure_after_creation_retains_artifacts_and_unbound_task(self):
        task = self.store.create("Work")
        original = self.manager._git

        def report_error_after_success(args, **kwargs):
            result = original(args, **kwargs)
            if args[:2] == ["worktree", "add"]:
                result.returncode = 1
            return result

        with (
            patch.object(self.manager, "_git", side_effect=report_error_after_success),
            self.assertRaisesRegex(ValueError, "Partial"),
        ):
            self.manager.create("partial", task.id)
        self.assertTrue(self.manager.resolve("partial").exists())
        self.assertIsNone(self.store.load(task.id).worktree)

    def test_failed_binding_keeps_checkout_and_branch_for_recovery(self):
        task = self.store.create("Work")
        with (
            patch.object(self.store, "_write", side_effect=OSError("Failure")),
            self.assertRaisesRegex(ValueError, "binding failed"),
        ):
            self.manager.create("partial", task.id)
        self.assertTrue(self.manager.resolve("partial").exists())
        self.assertIsNone(self.store.load(task.id).worktree)

    def test_removal_rejects_active_tasks_leases_and_dirty_files_and_keeps_branch(self):
        task, path = self.bound()
        with self.assertRaises(ValueError):
            self.manager.remove("feature")
        self.store.claim(task.id)
        self.store.complete(task.id)
        with self.assertRaisesRegex(ValueError, "leased"):
            self.manager.remove("feature", leased_paths=[path])
        (path / "untracked.txt").write_text("Preserve me")
        with self.assertRaisesRegex(ValueError, "changes"):
            self.manager.remove("feature")
        self.assertTrue((path / "untracked.txt").exists())
        (path / "untracked.txt").unlink()
        result = self.manager.remove("feature")
        self.assertEqual(result["branch_retained"], "codex/wt/feature")
        self.git("show-ref", "--verify", "refs/heads/codex/wt/feature")
        self.assertFalse(path.exists())
        self.assertIsNone(self.store.load(task.id).worktree)

    def test_ignored_changes_also_block_normal_removal(self):
        task, path = self.bound()
        self.store.claim(task.id)
        self.store.complete(task.id)
        (path / ".harness").mkdir()
        (path / ".harness" / "saved.txt").write_text("Preserve ignored content")
        with self.assertRaisesRegex(ValueError, "changes"):
            self.manager.remove("feature")

    def test_member_tools_use_assignment_and_keep_cwd_until_work_phase_ends(self):
        task, path = self.bound()
        team = TeamManager(self.root, lambda member: "Fixture")
        self.addCleanup(team.close)
        member = TeammateRuntime(team, "worker", "worker", "Work", False)
        team.members[member.name] = member
        member.bind(member.store.claim(task.id))

        def execute(name, arguments):
            block = (
                reply(calls=[call(name, name=name, arguments=json.dumps(arguments))])
                .choices[0]
                .message.tool_calls[0]
            )
            return json.loads(
                execute_fixture_tool(
                    block,
                    self.root,
                    handlers={
                        **homebrew_harness.tools.registry.TOOL_HANDLERS,
                        **member.handlers(),
                    },
                    team_member=member,
                )
            )

        self.assertNotIn(
            "error",
            execute("write_file", {"path": "assigned.txt", "content": "checkout only"}),
        )
        self.assertTrue((path / "assigned.txt").exists())
        self.assertFalse((self.root / "assigned.txt").exists())
        self.assertEqual(
            execute("read_file", {"path": "../../README.md"})["permission"], "deny"
        )
        self.assertEqual(
            execute("complete_task", {"task_id": task.id})["task"]["status"],
            "completed",
        )
        self.assertNotIn("error", execute("read_file", {"path": "assigned.txt"}))
        member.finish_work_turn("Done")
        self.assertIsNone(member.assignment)
        self.assertIn("error", execute("read_file", {"path": "README.md"}))

    def test_incomplete_task_record_is_rejected(self):
        task = self.store.create("Incomplete")
        saved = task.to_dict()
        saved.pop("worktree")
        self.store.runtime.put("tasks", task.id, saved)
        with self.assertRaises(ValueError):
            self.store.load(task.id)

    def test_bad_binding_does_not_starve_other_ready_tasks(self):
        bad, path = self.bound()
        self.git("switch", "-c", "other", cwd=path)
        good = self.store.create("Other ready work")
        claimant = TaskStore(self.root, owner="worker")
        self.assertEqual(claimant.claim_next().id, good.id)
        self.assertEqual(self.store.load(bad.id).status, "pending")
