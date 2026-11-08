import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from homebrew_harness.execution.execution_leases import ExecutionLeases
from homebrew_harness.runs import RunSpec, RunStore
from homebrew_harness.runs.maintenance import (
    backup_run,
    verify_backup,
    write_export,
    require_space,
)
from homebrew_harness.runs.spec import RunStopped
from homebrew_harness.tools.queries import search_text, git_status, git_diff


class ManagementTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.root = self.base / "workspace"
        self.root.mkdir()
        self.store = RunStore(self.root)
        self.addCleanup(self.store.close)

    def run_record(self, status="completed"):
        value = self.store.create(RunSpec("Preserve constraints in export"))
        value.update(status=status, cleanup_complete=True)
        self.store.runtime.put("runs", value["id"], value)
        return value

    def test_disk_floor_is_runtime_control_without_model_call(self):
        with patch(
            "shutil.disk_usage", return_value=shutil._ntuple_diskusage(100, 99, 1)
        ):
            with self.assertRaises(RunStopped):
                self.store.create(RunSpec("work", min_free_bytes=2))
            require_space(self.root, 0)
        self.assertFalse(self.store.runtime.backend().exists())

    def test_export_and_backup_preserve_database_and_attachment_manifest(self):
        value = self.run_record()
        attachment = self.root / ".harness/transcripts/reference.txt"
        attachment.parent.mkdir(parents=True)
        attachment.write_text("original evidence")
        result = write_export(self.store, value["id"], self.base / "run.json")
        self.assertTrue(result["exported"])
        self.assertEqual(
            json.loads((self.base / "run.json").read_text())["run"]["spec"]["prompt"],
            value["spec"]["prompt"],
        )
        result = backup_run(self.store, value["id"], self.base / "backup")
        self.assertTrue(result["backed_up"])
        self.assertTrue(verify_backup(self.base / "backup")["valid"])
        (
            self.base / "backup/attachments/.harness/transcripts/reference.txt"
        ).write_text("tampered")
        with self.assertRaisesRegex(ValueError, "manifest"):
            verify_backup(self.base / "backup")

    def test_backup_rejects_live_leases_and_running_status(self):
        value = self.run_record("running")
        with self.assertRaisesRegex(ValueError, "supervisor"):
            backup_run(self.store, value["id"], self.base / "backup")
        value["status"] = "paused"
        self.store.runtime.put("runs", value["id"], value)
        leases = ExecutionLeases(self.root)
        self.addCleanup(leases.close)
        with leases.use("managed-supervisor"), self.assertRaises(ValueError):
            backup_run(self.store, value["id"], self.base / "backup")
        self.assertFalse((self.base / "backup").exists())

    def test_output_cannot_overwrite_or_escape_through_symlink(self):
        value = self.run_record()
        for dest in (self.root / "export.json", self.base / "exists.json"):
            dest.write_text("keep")
            with self.assertRaises(ValueError):
                write_export(self.store, value["id"], dest)
            self.assertEqual(dest.read_text(), "keep")
        (self.base / "link").symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(ValueError):
            write_export(self.store, value["id"], self.base / "link/export.json")

    def test_literal_search_filters_private_files_and_symlinks(self):
        (self.root / "code.py").write_text("first\nneedle here\n")
        (self.root / ".env").write_text("needle secret")
        (self.root / "link.py").symlink_to(self.root / ".env")
        result = json.loads(search_text("needle", workdir=self.root))
        self.assertEqual(
            result["matches"], [{"path": "code.py", "line": 2, "text": "needle here"}]
        )
        with self.assertRaises(ValueError):
            search_text("needle", workdir=self.root, pattern="../*")

    def test_git_queries_are_bounded_and_exclude_private_changes(self):
        def git(*args):
            return subprocess.run(
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
                capture_output=True,
                check=True,
            )

        git("init", "-q")
        (self.root / "code.txt").write_text("old")
        git("add", "code.txt")
        git("commit", "-qm", "fixture")
        (self.root / "code.txt").write_text("new")
        (self.root / ".env").write_text("private")
        status = json.loads(git_status(workdir=self.root))
        self.assertEqual([r["path"] for r in status["files"]], ["code.txt"])
        diff = json.loads(git_diff(workdir=self.root))
        self.assertIn("+new", diff["diff"])
        with self.assertRaises(ValueError):
            git_diff(workdir=self.root, paths=[".env"])

    def test_backup_restores_into_new_inspection_directory(self):
        from homebrew_harness.runs.maintenance import restore_backup

        value = self.run_record("paused")
        attachment = self.root / ".harness/transcripts/keep.txt"
        attachment.parent.mkdir(parents=True)
        attachment.write_text("retained evidence")
        backup_run(self.store, value["id"], self.base / "backup")
        result = restore_backup(self.base / "backup", self.base / "restored")
        self.assertTrue(result["inspection_required"])
        restored = RunStore(self.base / "restored")
        self.addCleanup(restored.close)
        self.assertEqual(restored.get(value["id"])["status"], "blocked")
        self.assertTrue(restored.runtime.get_meta("restore_required"))
        self.assertEqual(
            (self.base / "restored/.harness/transcripts/keep.txt").read_text(),
            "retained evidence",
        )
        self.assertEqual(self.store.get(value["id"])["status"], "paused")
