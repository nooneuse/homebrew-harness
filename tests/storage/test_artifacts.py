import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from homebrew_harness.storage.artifact_inventory import inspect_artifacts
from homebrew_harness.context.compaction import ArchiveStore
from homebrew_harness.storage.durable_runtime import DurableStore
from homebrew_harness.storage.history_gc import apply, recover


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = DurableStore(self.root)

    def old(self):
        path = ArchiveStore(self.root, "old").save("tool-results", "result")
        self.store.update_fields(
            "artifacts", path, {"created_at": time.time() - 40 * 86400}
        )
        return path

    def test_empty_inventory_is_read_only(self):
        self.assertEqual(inspect_artifacts(self.root)["files"], [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_preview_preserves_unregistered_and_all_user_content(self):
        old = self.old()
        user = self.root / "source.py"
        user.write_text("user")
        unregistered = self.root / ".harness/transcripts"
        unregistered.mkdir()
        (unregistered / ("a" * 32 + ".jsonl")).write_text("unregistered")
        before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        plan = inspect_artifacts(self.root)
        self.assertEqual([item["path"] for item in plan["candidates"]], [old])
        self.assertEqual(
            before,
            {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()},
        )

    def test_checkpoint_pin_and_unknown_record_protect_archive(self):
        path = self.old()
        self.store.put(
            "operations",
            "unknown",
            {"checkpoint": "c", "state": "unknown", "output": path},
        )
        self.assertEqual(inspect_artifacts(self.root)["candidates"], [])
        self.store.remove("operations", "unknown")
        (self.root / ".harness/artifact-pins.json").write_text(json.dumps([path]))
        self.assertEqual(inspect_artifacts(self.root)["candidates"], [])

    def test_new_reference_or_changed_content_invalidates_preview(self):
        path = self.old()
        plan = inspect_artifacts(self.root)
        self.store.put("checkpoints", "new", {"archives": {"saved": [path]}})
        with self.assertRaises(ValueError):
            apply(self.root, plan)
        self.store.remove("checkpoints", "new")
        (self.root / path).write_text("changed")
        with self.assertRaises(ValueError):
            apply(self.root, plan)

    def test_expired_orphan_cleanup_and_recovery_are_idempotent(self):
        path = self.old()
        result = apply(self.root, inspect_artifacts(self.root))
        self.assertFalse((self.root / path).exists())
        self.assertEqual(result["deleted"], 1)
        self.assertTrue(recover(self.root, result["run_id"])["already_complete"])

    def test_interruption_after_quarantine_is_recoverable_and_new_pin_restores(self):
        path = self.old()
        plan = inspect_artifacts(self.root)
        with (
            patch(
                "homebrew_harness.storage.history_gc._finish",
                side_effect=RuntimeError("crash"),
            ),
            self.assertRaises(RuntimeError),
        ):
            apply(self.root, plan)
        journal = next((self.root / ".harness/gc").glob("*.json"))
        (self.root / ".harness/artifact-pins.json").write_text(json.dumps([path]))
        result = recover(self.root, journal.stem)
        self.assertEqual(result["restored"], 1)
        self.assertEqual((self.root / path).read_text(), "result")

    def test_symlink_and_scan_limit_fail_closed(self):
        path = self.old()
        (self.root / path).unlink()
        (self.root / path).symlink_to(self.root / "source")
        with self.assertRaises(ValueError):
            inspect_artifacts(self.root)
        (self.root / path).unlink()
        self.old()
        with self.assertRaises(ValueError):
            inspect_artifacts(self.root, max_files=1)
