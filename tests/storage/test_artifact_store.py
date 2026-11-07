"""Artifact ownership, create-only authorization and interruption recovery."""

import base64
import copy
import hashlib
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from homebrew_harness.execution.artifact_export import decode_export
from homebrew_harness.execution.artifact_policy import (
    ArtifactError,
    ArtifactFile,
    ArtifactProfile,
)
from homebrew_harness.execution.bash_policy import BashPolicy, bash_policy_scope
from homebrew_harness.orchestration.hard_conditions import HardVerifier
from homebrew_harness.security.safe_files import SafeFile
from homebrew_harness.storage.artifact_inventory import inspect_artifacts
from homebrew_harness.storage.artifact_store import ArtifactStore
from homebrew_harness.storage.history_gc import apply
from homebrew_harness.storage.retention_graph import state_roots
from homebrew_harness.tools.tool_outcome import ToolExecutionError


class ArtifactStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.rule = ArtifactProfile(
            "report",
            (
                ArtifactFile("output/a", "reports/a"),
                ArtifactFile("output/b", "reports/b"),
            ),
        )
        self.policy = BashPolicy(
            mode="container",
            cli="/missing/docker",
            image="sha256:" + "a" * 64,
            outputs=(self.rule,),
        )
        self.store = ArtifactStore(self.root, self.policy, owner="session-a")

    def collect(self, **kwargs):
        return self.store.collect(
            "execution",
            kwargs.pop("cwd", self.root),
            self.rule,
            [(self.rule.files[0], b"alpha\0\xff"), (self.rule.files[1], b"beta")],
            [],
            **kwargs,
        )

    def publish(self, bundle, store=None, **kwargs):
        store = store or self.store
        approval = store.approval(bundle["bundle_id"], workdir=self.root, **kwargs)
        return store.publish(
            bundle["bundle_id"], workdir=self.root, approval=approval, **kwargs
        )

    def test_collection_is_binary_safe_and_does_not_create_workspace_targets(self):
        bundle = self.collect()
        self.assertFalse((self.root / "reports").exists())
        data = self.store.get(bundle["bundle_id"], workdir=self.root, path="reports/a")
        self.assertEqual(base64.b64decode(data["preview"]), b"alpha\0\xff")
        self.assertEqual(data["encoding"], "base64")
        self.assertEqual(len(self.store.list(workdir=self.root)["bundles"]), 1)

    def test_publication_requires_approval_of_exact_manifest(self):
        bundle = self.collect()
        with self.assertRaises(ArtifactError):
            self.store.publish(bundle["bundle_id"], workdir=self.root)
        approval = self.store.approval(bundle["bundle_id"], workdir=self.root)
        approval["files"][0]["sha256"] = "0" * 64
        with self.assertRaises(ArtifactError):
            self.store.publish(
                bundle["bundle_id"], workdir=self.root, approval=approval
            )
        self.assertFalse((self.root / "reports").exists())

    def test_publication_and_idempotent_reinspection(self):
        bundle = self.collect()
        result = self.publish(bundle)
        self.assertEqual(result["state"], "published")
        self.assertEqual((self.root / "reports/a").read_bytes(), b"alpha\0\xff")
        self.assertEqual(self.publish(bundle)["state"], "published")

    def test_preflight_conflict_creates_no_other_targets_and_keeps_bundle(self):
        bundle = self.collect()
        (self.root / "reports").mkdir()
        (self.root / "reports/b").write_text("existing")
        with self.assertRaises(ArtifactError):
            self.publish(bundle)
        self.assertFalse((self.root / "reports/a").exists())
        self.assertEqual((self.root / "reports/b").read_text(), "existing")
        self.assertEqual(
            self.store.get(bundle["bundle_id"], workdir=self.root)["state"], "conflict"
        )

    def test_symlink_ancestor_is_refused_before_any_publication(self):
        bundle = self.collect()
        (self.root / "real").mkdir()
        (self.root / "reports").symlink_to(self.root / "real")
        with self.assertRaises((OSError, ValueError)):
            self.publish(bundle)
        self.assertFalse((self.root / "real/a").exists())

    def test_committed_file_then_crash_is_verified_on_resume(self):
        bundle = self.collect()
        original = SafeFile.publish

        def interrupted(target, data, *args, **kwargs):
            result = original(target, data, *args, **kwargs)
            if target.parts == ("reports", "a"):
                raise OSError("fixture after atomic create")
            return result

        with (
            patch.object(SafeFile, "publish", interrupted),
            self.assertRaises(ToolExecutionError) as error,
        ):
            self.publish(bundle)
        self.assertEqual(error.exception.outcome, "unknown")
        self.assertTrue((self.root / "reports/a").exists())
        self.assertFalse((self.root / "reports/b").exists())
        recovered = ArtifactStore(self.root, self.policy, owner="session-a")
        self.assertEqual(self.publish(bundle, recovered)["state"], "published")
        self.assertEqual((self.root / "reports/b").read_bytes(), b"beta")

    def test_interruption_before_second_intent_returns_partial(self):
        bundle = self.collect()
        save = self.store._save

        def interrupted(value):
            if (
                value["files"][0]["state"] == "published"
                and value["files"][1]["state"] == "publishing"
            ):
                raise OSError("fixture before second intent")
            return save(value)

        with (
            patch.object(self.store, "_save", interrupted),
            self.assertRaises(ToolExecutionError),
        ):
            self.publish(bundle)
        self.assertEqual((self.root / "reports/a").read_bytes(), b"alpha\0\xff")
        self.assertFalse((self.root / "reports/b").exists())
        self.assertEqual(self.publish(bundle)["state"], "published")

    def test_modified_previously_published_file_is_not_overwritten(self):
        bundle = self.collect()
        self.publish(bundle)
        (self.root / "reports/a").write_text("user edit")
        with self.assertRaises(ArtifactError):
            self.publish(bundle)
        self.assertEqual((self.root / "reports/a").read_text(), "user edit")

    def test_stale_approval_and_manifest_tampering_are_refused(self):
        bundle = self.collect()
        approval = self.store.approval(bundle["bundle_id"], workdir=self.root)
        value = self.store._load(bundle["bundle_id"])
        self.store._save(value)
        with self.assertRaises(ArtifactError):
            self.store.publish(
                bundle["bundle_id"], workdir=self.root, approval=approval
            )
        value = self.store._load(bundle["bundle_id"])
        value["files"][0]["target"] = "other"
        self.store.store.put("artifact_bundles", bundle["bundle_id"], value)
        with self.assertRaises(ArtifactError):
            self.store.get(bundle["bundle_id"], workdir=self.root)

    def test_owner_policy_task_and_workspace_boundaries(self):
        bundle = self.collect(task_id="task_12345678")
        with self.assertRaises(ArtifactError):
            ArtifactStore(self.root, self.policy).get(
                bundle["bundle_id"], workdir=self.root, task_id="task_12345678"
            )
        for store, task in (
            (self.store, None),
            (ArtifactStore(self.root, self.policy, owner="session-b"), "task_12345678"),
            (
                ArtifactStore(
                    self.root, replace(self.policy, timeout=30), owner="session-a"
                ),
                "task_12345678",
            ),
        ):
            with self.subTest(task=task), self.assertRaises(ArtifactError):
                store.approval(bundle["bundle_id"], workdir=self.root, task_id=task)
        self.assertEqual(self.store.list(workdir=self.root)["bundles"], [])
        self.assertEqual(
            len(self.store.list(workdir=self.root, task_id="task_12345678")["bundles"]),
            1,
        )

    def test_replaced_task_workspace_and_changed_resolver_are_refused(self):
        cwd = self.root / ".worktrees/w"
        cwd.mkdir(parents=True)
        bundle = self.collect(cwd=cwd, task_id="task_12345678")
        store = ArtifactStore(
            self.root,
            self.policy,
            owner="session-a",
            workspace_resolver=lambda _: self.root,
        )
        with self.assertRaises(ArtifactError):
            store.get(bundle["bundle_id"], workdir=cwd, task_id="task_12345678")
        cwd.rename(cwd.with_name("old"))
        cwd.mkdir()
        with self.assertRaises(ArtifactError):
            self.store.get(bundle["bundle_id"], workdir=cwd, task_id="task_12345678")

    def test_corrupted_blob_prevents_publication(self):
        bundle = self.collect()
        (self.root / bundle["files"][0]["reference"]).write_text(
            base64.b64encode(b"tampered").decode()
        )
        with self.assertRaises(ArtifactError):
            self.publish(bundle)
        self.assertFalse((self.root / "reports").exists())

    def test_collection_storage_failure_keeps_durable_recovery_references(self):
        original = self.store._archive

        def fail(data, kind, reference=None):
            if kind == "artifact_blob":
                raise OSError("fixture blob write")
            return original(data, kind, reference)

        with (
            patch.object(self.store, "_archive", fail),
            self.assertRaises(ToolExecutionError) as caught,
        ):
            self.collect()
        identity = caught.exception.details["bundle_id"]
        bundle = self.store.get(identity, workdir=self.root)
        self.assertEqual(bundle["state"], "collection_unknown")
        self.assertIn(bundle["reference"], state_roots(self.store.store.read()))
        with self.assertRaises(ArtifactError):
            self.publish(bundle)

    def test_goal_file_predicate_requires_host_publication(self):
        bundle = self.collect()
        spec = {
            "condition": "report exists",
            "judge": False,
            "predicates": [
                {
                    "id": "report",
                    "type": "file_sha256",
                    "path": "reports/a",
                    "sha256": hashlib.sha256(b"alpha\0\xff").hexdigest(),
                }
            ],
        }
        verifier = HardVerifier(self.root)
        self.assertFalse(verifier.check(spec)["ok"])
        self.publish(bundle)
        self.assertTrue(verifier.check(spec)["ok"])

    def test_gc_protects_pending_and_retained_references_then_reclaims_published_orphans(
        self,
    ):
        bundle = self.collect()
        future = time.time() + 60 * 86400
        self.assertFalse(inspect_artifacts(self.root, now=future)["candidates"])
        self.publish(bundle)
        self.store.store.put("checkpoints", "retain", {"ref": bundle["reference"]})
        self.assertFalse(inspect_artifacts(self.root, now=future)["candidates"])
        self.store.store.remove("checkpoints", "retain")
        plan = inspect_artifacts(self.root, now=future)
        self.assertEqual(len(plan["candidates"]), 3)
        with patch(
            "homebrew_harness.storage.artifact_inventory.time.time", return_value=future
        ):
            result = apply(self.root, plan)
        self.assertEqual(result["deleted"], 3)
        self.assertEqual((self.root / "reports/b").read_bytes(), b"beta")

    def test_paths_counts_and_delegation_do_not_expand(self):
        for path in (
            "/out",
            "../out",
            "a//b",
            ".git/x",
            ".env",
            ".harness/x",
            ".ssh/id",
            ".venv/bin/python",
            ".netrc",
            ".npmrc",
        ):
            with self.subTest(path=path), self.assertRaises(ArtifactError):
                ArtifactFile(path, "out")
        with self.assertRaises(ArtifactError):
            ArtifactProfile(
                "nested", (ArtifactFile("a", "out"), ArtifactFile("b", "out/b"))
            )
        with bash_policy_scope(self.policy), self.assertRaises(ValueError):
            with bash_policy_scope(
                replace(
                    self.policy,
                    outputs=(ArtifactProfile("extra", (ArtifactFile("x", "x"),)),),
                )
            ):
                pass

    def test_transport_rejects_undeclared_duplicate_malformed_and_oversized_files(self):
        data = b"alpha"
        row = {
            "source": "output/a",
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "data": base64.b64encode(data).decode(),
        }
        envelope = {"format": 1, "files": [row], "missing": ["output/b"]}
        self.assertEqual(decode_export(json.dumps(envelope), self.rule)[0][0][1], data)
        invalid = []
        for field, value in (
            ("source", []),
            ("source", "../x"),
            ("bytes", True),
            ("sha256", "x"),
            ("data", "!"),
        ):
            changed = copy.deepcopy(envelope)
            changed["files"][0][field] = value
            invalid.append(changed)
        changed = copy.deepcopy(envelope)
        changed["files"].append(row)
        invalid.append(changed)
        changed = copy.deepcopy(envelope)
        changed["missing"] = []
        invalid.append(changed)
        for envelope in invalid:
            with self.subTest(envelope=envelope), self.assertRaises(ArtifactError):
                decode_export(json.dumps(envelope), self.rule)

    def test_cancellation_between_files_returns_partial_then_resumes(self):
        from homebrew_harness.execution.model_gateway import ModelCancelled

        bundle = self.collect()

        def cancel_second():
            if (self.root / "reports/a").exists() and not (
                self.root / "reports/b"
            ).exists():
                raise ModelCancelled("fixture between files")

        with (
            patch("homebrew_harness.storage.artifact_store.check", cancel_second),
            self.assertRaises(ToolExecutionError) as caught,
        ):
            self.publish(bundle)
        self.assertEqual(caught.exception.outcome, "partial")
        self.assertEqual(caught.exception.details["committed"], ["reports/a"])
        self.assertFalse((self.root / "reports/b").exists())
        self.publish(bundle)
        self.assertEqual((self.root / "reports/b").read_bytes(), b"beta")

    def test_concurrent_publish_uses_one_revision_and_exclusive_file_creation(self):
        import threading

        bundle = self.collect()
        approval = self.store.approval(bundle["bundle_id"], workdir=self.root)
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def publish():
            try:
                barrier.wait(timeout=3)
                self.store.publish(
                    bundle["bundle_id"], workdir=self.root, approval=approval
                )
                results.append("published")
            except (ValueError, OSError):
                # A pre-publication filesystem/lease refusal has no committed
                # effect. Post-publication failures use ToolExecutionError.
                results.append("refused")
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=publish) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), ["published", "refused"])
        self.assertEqual((self.root / "reports/a").read_bytes(), b"alpha\0\xff")
        self.assertEqual((self.root / "reports/b").read_bytes(), b"beta")
        self.assertEqual(self.store._load(bundle["bundle_id"])["state"], "published")

    def test_listing_is_bounded_and_paginates_without_loading_file_bytes(self):
        self.collect()
        self.collect()
        first = self.store.list(workdir=self.root, limit=1)
        second = self.store.list(
            workdir=self.root, limit=1, offset=first["next_offset"]
        )
        self.assertEqual(len(first["bundles"]), 1)
        self.assertNotEqual(
            first["bundles"][0]["bundle_id"], second["bundles"][0]["bundle_id"]
        )
        self.assertIsNone(second["next_offset"])
        with self.assertRaises(ArtifactError):
            self.store.list(workdir=self.root, limit=1000)

    def test_progress_revision_compare_and_update_is_one_transaction(self):
        import threading

        bundle = self.collect()
        first = self.store._load(bundle["bundle_id"])
        second = copy.deepcopy(first)
        barrier = threading.Barrier(2)
        results = []

        def update(value):
            barrier.wait(timeout=3)
            try:
                self.store._save(value)
                results.append("saved")
            except ArtifactError:
                results.append("stale")

        threads = [
            threading.Thread(target=update, args=(value,)) for value in (first, second)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(sorted(results), ["saved", "stale"])
        self.assertEqual(
            self.store._load(bundle["bundle_id"])["revision"], bundle["revision"] + 1
        )
