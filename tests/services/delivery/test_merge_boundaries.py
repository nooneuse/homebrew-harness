"""Accepted merge facts, current Work fences and transactional completion."""

import unittest
from unittest.mock import patch

from homebrew_harness.platforms.merges import receipt
from tests.runtime.test_interaction import eventually
from tests.services.delivery import test_merge_flow as fixture


class MergeBoundaries(unittest.TestCase):
    setUp = fixture.MergeFlowTests.setUp
    start = fixture.MergeFlowTests.start
    run_git = fixture.MergeFlowTests.run_git
    ready = fixture.MergeFlowTests.ready
    manual = fixture.MergeFlowTests.manual

    def record_merged(self, service, row):
        delivery = service.runtime.maintenance.delivery
        self.peer.merge_pull(1)
        observed = delivery.adapter.metadata(row["change_request"])
        delivery.store.record_merge(
            row["id"],
            receipt(row["change_request"], observed, row["candidate"]["commit"]),
        )
        return delivery

    def test_pause_retains_merged_fact_and_requires_resume_for_work_completion(self):
        service, work, row = self.manual()
        service.pause("all")
        delivery = self.record_merged(service, row)
        self.assertEqual(work.snapshot().state, "review")
        self.assertEqual(
            delivery.store.get("changesets", row["id"])["state"], "merged_verifying"
        )
        self.assertEqual(service.snapshot().state, "paused")
        service.resume()
        eventually(lambda: work.snapshot().state == "delivered", 40)
        self.assertFalse(any(kind == "merge" for kind, _ in self.peer.writes))

    def test_merged_old_requirements_do_not_complete_the_revised_work(self):
        service, work, row = self.manual()
        service.pause("all")
        work.revise("Preserve an additional acceptance requirement.")
        delivery = self.record_merged(service, row)
        service.resume()
        saved = eventually(
            lambda: (
                delivery.store.get("changesets", row["id"])
                if delivery.store.get("changesets", row["id"])["state"] == "delivered"
                else None
            ),
            40,
        )
        self.assertTrue(saved["requirements_superseded"])
        self.assertNotEqual(work.snapshot().state, "delivered")
        self.assertEqual(work.snapshot().input_revision, 1)
        self.assertEqual(
            saved["merge"]["result_commit"], saved["integration"]["commit"]
        )

    def test_completion_rolls_back_work_changeset_and_activity_together(self):
        service, work, row = self.manual()
        delivery = service.runtime.maintenance.delivery
        original = delivery.ms.db._put
        fired = []

        def fail(conn, section, key, value):
            if section == "service_changesets" and value["state"] == "delivered":
                fired.append(True)
                raise OSError("Simulated integration completion persistence failure.")
            return original(conn, section, key, value)

        with patch.object(delivery.ms.db, "_put", fail):
            self.peer.merge_pull(1)
            eventually(
                lambda: (
                    delivery.store.get("changesets", row["id"])["state"]
                    == "verification_blocked"
                ),
                40,
            )
            self.assertTrue(fired)
            self.assertEqual(work.snapshot().state, "review")
            self.assertFalse(
                delivery.store.get("changesets", row["id"]).get("integration")
            )
            self.assertFalse(
                (service.store.work(work.id).get("workspace_sync") or {}).get("state")
            )
        service.refresh_delivery(row["id"], request_id="retry-verification")
        eventually(lambda: work.snapshot().state == "delivered", 40)
        self.assertFalse(any(kind == "merge" for kind, _ in self.peer.writes))

    def test_read_outage_waits_without_model_calls_and_recovers(self):
        service, work, row = self.manual()
        delivery = service.runtime.maintenance.delivery
        calls = len(self.peer.model_requests)
        with patch.object(
            delivery.adapter,
            "metadata",
            side_effect=TimeoutError("Temporary platform read failure"),
        ):
            saved = eventually(
                lambda: (
                    delivery.store.get("changesets", row["id"])
                    if delivery.store.get("changesets", row["id"])
                    .get("read_errors", {})
                    .get("pr")
                    else None
                ),
                10,
            )
            self.assertEqual(saved["state"], "merge_ready")
            self.assertEqual(len(self.peer.model_requests), calls)
        self.peer.merge_pull(1)
        eventually(lambda: work.snapshot().state == "delivered", 40)
        self.assertFalse(delivery.store.get("changesets", row["id"]).get("read_errors"))

    def test_missing_required_review_retains_merge_fact_without_reporting_delivery(
        self,
    ):
        service = self.start(
            merge={"poll_seconds": 0.1}, reviewer_ids=[17], required_approvals=1
        )
        work = service.work.submit("Implement the bounded value.")
        delivery = service.runtime.maintenance.delivery
        row = eventually(
            lambda: next(
                (
                    r
                    for r in delivery.rows("changesets")
                    if r["state"] == "review_waiting"
                ),
                None,
            ),
            45,
        )
        self.peer.merge_pull(1)
        saved = eventually(
            lambda: (
                delivery.store.get("changesets", row["id"])
                if delivery.store.get("changesets", row["id"])["state"]
                == "verification_blocked"
                else None
            ),
            20,
        )
        self.assertEqual(saved["merge"]["state"], "merged")
        self.assertEqual(work.snapshot().state, "review")

    def test_refreshed_source_does_not_overwrite_unreviewed_external_head(self):
        service, work, row = self.manual()
        repo = "--git-dir=" + str(self.remote)
        tree = (
            self.run_git(repo, "rev-parse", row["candidate"]["target_base"] + "^{tree}")
            .decode()
            .strip()
        )
        unknown = (
            self.run_git(
                repo,
                "commit-tree",
                tree,
                "-p",
                row["candidate"]["commit"],
                "-m",
                "unreviewed change",
            )
            .decode()
            .strip()
        )
        self.run_git(repo, "update-ref", "refs/heads/" + row["branch"], unknown)
        eventually(
            lambda: service.delivery(row["id"])["state"] == "integration_required", 10
        )
        with self.assertRaises(Exception):
            service.refresh_delivery(row["id"], request_id="reject-foreign-head")
        self.assertEqual(
            self.run_git(repo, "rev-parse", row["branch"]).decode().strip(), unknown
        )
        self.assertEqual(work.snapshot().state, "review")
