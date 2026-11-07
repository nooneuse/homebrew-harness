"""Host discard, retained content and indexed model-facing artifact pages."""

import json
import time
import unittest
from unittest.mock import patch

from homebrew_harness.execution.execution_leases import ExecutionLeases
from homebrew_harness.storage.artifact_inventory import inspect_artifacts
from homebrew_harness.storage.artifact_store import (
    ArtifactError,
    discard,
    discard_preview,
    host_artifact_list,
)
from homebrew_harness.storage.history_gc import apply
from homebrew_harness.storage.sqlite_store import artifact_partition
from tests.storage import test_artifact_store as fixture


class ArtifactHostManagementTests(unittest.TestCase):
    setUp = fixture.ArtifactStoreTests.setUp
    collect = fixture.ArtifactStoreTests.collect
    publish = fixture.ArtifactStoreTests.publish

    def age(self):
        for ref in self.store.store.items("artifacts"):
            self.store.store.update_fields(
                "artifacts", ref, {"created_at": time.time() - 40 * 86400}
            )

    def test_discard_releases_only_unreferenced_contents_and_is_idempotent(self):
        bundle = self.collect()
        self.age()
        plan = discard_preview(self.root, bundle["bundle_id"])
        self.assertEqual(inspect_artifacts(self.root)["candidates"], [])
        self.assertEqual(discard(self.root, plan)["state"], "discarded")
        self.assertTrue(discard(self.root, plan)["already_discarded"])
        with self.assertRaises(ArtifactError):
            self.publish(bundle)
        self.assertEqual(apply(self.root, inspect_artifacts(self.root))["deleted"], 3)
        self.assertEqual(
            self.store.get(bundle["bundle_id"], workdir=self.root)["state"], "discarded"
        )

    def test_discard_never_rolls_back_published_host_files(self):
        bundle = self.collect()
        self.publish(bundle)
        self.age()
        discard(self.root, discard_preview(self.root, bundle["bundle_id"]))
        apply(self.root, inspect_artifacts(self.root))
        self.assertEqual((self.root / "reports/a").read_bytes(), b"alpha\0\xff")

    def test_discarded_manifest_still_follows_external_pin(self):
        bundle = self.collect()
        self.age()
        discard(self.root, discard_preview(self.root, bundle["bundle_id"]))
        (self.root / ".harness/artifact-pins.json").write_text(
            json.dumps([bundle["reference"]])
        )
        self.assertEqual(inspect_artifacts(self.root)["candidates"], [])

    def test_stale_discard_plan_and_live_session_are_refused(self):
        bundle = self.collect()
        plan = discard_preview(self.root, bundle["bundle_id"])
        value = self.store._load(bundle["bundle_id"])
        self.store._save(value)
        with self.assertRaises(ArtifactError):
            discard(self.root, plan)
        leases = ExecutionLeases(self.root)
        try:
            leases.acquire("session")
            with self.assertRaises(ValueError):
                discard(self.root, discard_preview(self.root, bundle["bundle_id"]))
        finally:
            leases.close()

    def test_model_page_is_scope_indexed_and_materializes_only_page_rows(self):
        bundle = self.collect()
        for _ in range(4):
            self.collect(task_id="task_aaaaaaaa")
        with patch.object(
            self.store.store,
            "items",
            side_effect=AssertionError("no full table materialization"),
        ):
            page = self.store.list(workdir=self.root, limit=1)
        self.assertEqual(
            [b["bundle_id"] for b in page["bundles"]], [bundle["bundle_id"]]
        )
        self.assertIsNone(page["next_offset"])
        record = self.store._load(bundle["bundle_id"])
        with self.store.store.backend().connection() as conn:
            query = conn.execute(
                "EXPLAIN QUERY PLAN SELECT value FROM records WHERE section='artifact_bundles' AND owner=? ORDER BY rowid DESC LIMIT 2",
                (artifact_partition(record),),
            ).fetchall()
        self.assertTrue(any("records_owner" in row[-1] for row in query), query)
        self.assertFalse(any("TEMP B-TREE" in row[-1] for row in query), query)
        self.assertEqual(len(host_artifact_list(self.root, limit=2)["bundles"]), 2)

    def test_host_management_tools_are_not_model_tools(self):
        from homebrew_harness.tools.catalog import TOOL_SCHEMAS

        self.assertNotIn("discard_artifact", TOOL_SCHEMAS)
        self.assertNotIn("artifact_discard", TOOL_SCHEMAS)

    def test_publish_verifies_blob_content_once_after_approval(self):
        bundle = self.collect()
        approval = self.store.approval(bundle["bundle_id"], workdir=self.root)
        with patch.object(
            self.store, "_read_blob", wraps=self.store._read_blob
        ) as reads:
            self.store.publish(
                bundle["bundle_id"], workdir=self.root, approval=approval
            )
        self.assertEqual(reads.call_count, 2)
