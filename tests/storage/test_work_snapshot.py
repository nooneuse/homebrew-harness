"""Source handoffs stay immutable and use independent retention bundles."""

import tempfile
import unittest
from pathlib import Path

from homebrew_harness.storage.artifact_store import host_artifact_list
from homebrew_harness.storage.durable_runtime import DurableStore
from homebrew_harness.storage.retention_graph import state_roots
from homebrew_harness.storage.work_snapshot import freeze, verify


class WorkSnapshot(unittest.TestCase):
    def test_retained_manifest_protects_source_without_entering_publication_inventory(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "source.txt").write_text("finished implementation")
            value = freeze(
                root,
                "handoff_" + "1" * 32,
                {"max_files": 10, "max_bytes": 10000, "max_paths": 100},
            )
            self.assertEqual(host_artifact_list(root)["bundles"], [])
            store = DurableStore(root)
            bundle = store.get("work_source_bundles", "handoff_" + "1" * 32)
            state = {"work_source_bundles": {bundle["id"]: bundle}}
            self.assertEqual(state_roots(state), set())
            state["run_work_progress"] = {"progress": {"source": value}}
            self.assertIn(bundle["files"][0], state_roots(state))
            (root / "source.txt").write_text("future work")
            self.assertEqual(verify(value), value)
            copied = Path(value["workspace"]) / "source.txt"
            copied.chmod(0o600)
            copied.write_text("corrupt copy")
            with self.assertRaises(ValueError):
                verify(value)
