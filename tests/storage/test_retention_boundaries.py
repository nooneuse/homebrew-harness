"""External references must retain the full artifact dependency graph."""

import json
import time
import unittest
from unittest.mock import patch
from tests.storage import test_artifact_store as fixture
from homebrew_harness.storage.artifact_inventory import inspect_artifacts
from homebrew_harness.storage.history_gc import apply, recover
from homebrew_harness.storage.retention_graph import state_roots


class RetentionBoundaryTests(unittest.TestCase):
    setUp = fixture.ArtifactStoreTests.setUp
    collect = fixture.ArtifactStoreTests.collect
    publish = fixture.ArtifactStoreTests.publish

    def age(self):
        for reference in self.store.store.items("artifacts"):
            self.store.store.update_fields(
                "artifacts", reference, {"created_at": time.time() - 40 * 86400}
            )

    def pin(self, reference):
        (self.root / ".harness/artifact-pins.json").write_text(json.dumps([reference]))

    def test_external_manifest_pin_protects_blob_until_pin_removed(self):
        bundle = self.collect()
        self.publish(bundle)
        self.age()
        self.pin(bundle["reference"])
        self.assertEqual(inspect_artifacts(self.root)["candidates"], [])
        (self.root / ".harness/artifact-pins.json").unlink()
        result = apply(self.root, inspect_artifacts(self.root))
        self.assertEqual(result["deleted"], 3)
        self.assertEqual((self.root / "reports/b").read_bytes(), b"beta")

    def test_goal_reference_to_manifest_protects_full_bundle(self):
        bundle = self.collect()
        self.publish(bundle)
        self.age()
        directory = self.root / ".harness/goals"
        directory.mkdir()
        (directory / "evidence.json").write_text(
            json.dumps({"artifact": bundle["reference"]})
        )
        self.assertEqual(inspect_artifacts(self.root)["candidates"], [])

    def test_reference_closure_handles_transitive_edges_and_cycles(self):
        state = {
            "artifact_bundles": {
                "a": {
                    "state": "published",
                    "reference": ".harness/a",
                    "dependency": ".harness/b",
                },
                "b": {
                    "state": "published",
                    "reference": ".harness/b",
                    "files": [".harness/a", ".harness/bytes"],
                },
            }
        }
        self.assertEqual(
            state_roots(state, {".harness/a"}),
            {".harness/a", ".harness/b", ".harness/bytes"},
        )

    def test_new_pin_after_quarantine_restores_manifest_and_contents(self):
        bundle = self.collect()
        self.publish(bundle)
        self.age()
        with patch(
            "homebrew_harness.storage.history_gc._finish",
            side_effect=OSError("fixture crash"),
        ):
            with self.assertRaises(OSError):
                apply(self.root, inspect_artifacts(self.root))
        self.pin(bundle["reference"])
        journal = next((self.root / ".harness/gc").glob("*.json"))
        self.assertEqual(recover(self.root, journal.stem)["restored"], 3)
        self.assertEqual(
            self.store.get(bundle["bundle_id"], workdir=self.root, path="reports/b")[
                "preview"
            ],
            "beta",
        )
