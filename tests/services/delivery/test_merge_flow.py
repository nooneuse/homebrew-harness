"""PR-first delivery with real platform HTTP, native Git and operator decisions."""

import time
import unittest
from unittest.mock import patch

from tests.services.delivery import test_flow as fixture
from tests.runtime.test_interaction import eventually


class MergeFlowTests(unittest.TestCase):
    setUp = fixture.FlowTests.setUp
    run_git = fixture.FlowTests.run_git
    start = fixture.FlowTests.start
    pending_target = fixture.FlowTests.pending_target

    def ready(self, service, work):
        row = next(
            (
                r
                for r in service.runtime.maintenance.delivery.rows("changesets")
                if r["id"] == service.store.work(work.id).get("changeset_id")
            ),
            None,
        )
        if row and row["state"] in {"failed", "quality_failed"}:
            raise AssertionError(
                str({k: row.get(k) for k in ("state", "error", "error_detail")})
            )
        return row if row and row["state"] == "merge_ready" else None

    def manual(self):
        service = self.start(merge={"poll_seconds": 0.1})
        work = service.work.submit("Implement the bounded value.")
        row = eventually(lambda: self.ready(service, work), 45)
        return service, work, row

    def test_manual_is_default_and_does_not_create_or_execute_merge_authority(self):
        service, work, row = self.manual()
        self.assertEqual(service.runtime.spec.delivery["merge"]["mode"], "manual")
        self.assertFalse(service.requests())
        self.assertFalse(
            any(
                a["kind"] == "merge_change_request"
                for a in service.runtime.maintenance.delivery.rows("actions")
            )
        )
        self.assertEqual(
            self.run_git("--git-dir=" + str(self.remote), "rev-parse", "main")
            .decode()
            .strip(),
            row["candidate"]["target_base"],
        )
        with self.assertRaises(Exception):
            service.plan_delivery(row["id"], "merge_change_request")
        calls = len(self.peer.model_requests)
        with patch.object(
            service.runtime.maintenance.delivery,
            "execute",
            wraps=service.runtime.maintenance.delivery.execute,
        ):
            time.sleep(0.3)
        self.assertEqual(len(self.peer.model_requests), calls)
        merged = self.peer.merge_pull(1)
        eventually(lambda: work.snapshot().state == "delivered", 40)
        saved = service.delivery(row["id"])
        self.assertEqual(saved["merge"]["actor_id"], 17)
        self.assertEqual(saved["integration"]["commit"], merged)
        self.assertNotEqual(merged, row["candidate"]["commit"])
        self.assertFalse(any(kind == "merge" for kind, _ in self.peer.writes))

    def test_manual_waiting_does_not_expire_with_the_ci_execution_window(self):
        service, work, row = self.manual()
        store = service.runtime.maintenance.delivery.store
        store.update("changesets", row["id"], ci_started=time.time() - 100000)
        self.assertEqual(service.delivery(row["id"])["state"], "merge_ready")
        self.peer.merge_pull(1, method="squash")
        eventually(lambda: work.snapshot().state == "delivered", 40)
        self.assertEqual(
            service.delivery(row["id"])["integration"]["proof"], "squash_tree"
        )

    def authorized(self, method):
        service = self.start(
            merge={"mode": "service_approved", "method": method, "poll_seconds": 0.1}
        )
        work = service.work.submit("Implement the bounded value.")
        request = eventually(lambda: self.pending_target(service), 45)
        row = service.runtime.maintenance.delivery.rows("changesets")[-1]
        service.decide(request, "approve", decision_id="merge-" + method)
        eventually(lambda: work.snapshot().state == "delivered", 40)
        saved = service.delivery(row["id"])
        self.assertNotEqual(
            saved["candidate"]["commit"], saved["integration"]["commit"]
        )
        self.assertEqual(saved["merge"]["method"], method)
        self.assertEqual(saved["merge"]["actor_id"], 12)

    def test_authorized_native_squash_verifies_actual_result(self):
        self.authorized("squash")

    def test_authorized_native_rebase_verifies_actual_result(self):
        self.authorized("rebase")

    def test_target_advance_before_manual_merge_is_included_in_actual_sync(self):
        service, work, row = self.manual()
        repo = "--git-dir=" + str(self.remote)
        before = row["candidate"]["target_base"]
        # A maintainer advances the target independently using another file.
        (self.workspace / "upstream.txt").write_text("upstream change\n")
        self.run_git("add", "upstream.txt", cwd=self.workspace)
        self.run_git("commit", "-qm", "upstream change", cwd=self.workspace)
        later = self.run_git("rev-parse", "HEAD", cwd=self.workspace).decode().strip()
        self.run_git(repo, "fetch", str(self.workspace), "HEAD")
        self.run_git(repo, "update-ref", "refs/heads/main", later, before)
        eventually(
            lambda: service.delivery(row["id"])["state"] == "integration_required", 10
        )
        self.peer.merge_pull(1)
        eventually(lambda: work.snapshot().state == "delivered", 40)
        self.assertEqual(
            service.delivery(row["id"])["integration"]["target_base"], later
        )
        self.assertEqual(
            (self.workspace / "upstream.txt").read_text(), "upstream change\n"
        )

    def test_lost_merge_response_is_reconciled_without_another_write(self):
        service = self.start()
        work = service.work.submit("Implement the bounded value.")
        request = eventually(lambda: self.pending_target(service), 45)
        delivery = service.runtime.maintenance.delivery
        self.peer.drop = "merge"
        with patch(
            "homebrew_harness.services.delivery.merge_flow.observe", return_value=False
        ):
            service.decide(request, "approve", decision_id="merge")
            action = eventually(
                lambda: next(
                    (
                        a
                        for a in delivery.rows("actions")
                        if a["kind"] == "merge_change_request"
                        and a["state"] == "unknown"
                    ),
                    None,
                ),
                20,
            )
            self.assertEqual(sum(kind == "merge" for kind, _ in self.peer.writes), 1)
            confirmed = service.inspect_delivery(
                action["id"], request_id="inspect-merge"
            )
            self.assertEqual(confirmed["state"], "confirmed")
            self.assertEqual(
                confirmed,
                service.inspect_delivery(action["id"], request_id="inspect-merge"),
            )
        eventually(lambda: work.snapshot().state == "delivered", 40)
        self.assertEqual(sum(kind == "merge" for kind, _ in self.peer.writes), 1)

    def test_missing_native_rules_keeps_merge_unavailable_without_fallback(self):
        self.peer.protection = {
            "required_status_checks": {"contexts": ["CI"], "strict": False},
            "required_pull_request_reviews": {},
            "enforce_admins": {"enabled": True},
        }
        service = self.start()
        work = service.work.submit("Implement the bounded value.")
        row = eventually(
            lambda: next(
                (
                    r
                    for r in service.runtime.maintenance.delivery.rows("changesets")
                    if r.get("error") == "native_merge_policy_unverified"
                ),
                None,
            ),
            45,
        )
        self.assertFalse(service.requests())
        self.assertEqual(work.snapshot().state, "review")
        self.assertEqual(
            self.run_git("--git-dir=" + str(self.remote), "rev-parse", "main")
            .decode()
            .strip(),
            row["candidate"]["target_base"],
        )
        self.assertFalse(any(kind == "merge" for kind, _ in self.peer.writes))

    def test_closed_unmerged_pr_is_not_delivery_and_can_be_observed_reopened(self):
        service, work, row = self.manual()
        self.peer.pulls[0]["state"] = "closed"
        eventually(
            lambda: service.delivery(row["id"])["state"] == "closed_unmerged", 10
        )
        self.assertEqual(work.snapshot().state, "review")
        self.peer.pulls[0]["state"] = "open"
        eventually(lambda: self.ready(service, work), 15)
        self.peer.merge_pull(1)
        eventually(lambda: work.snapshot().state == "delivered", 40)

    def test_explicit_refresh_rechecks_a_new_base_and_reuses_the_same_pr(self):
        service, work, row = self.manual()
        repo = "--git-dir=" + str(self.remote)
        before = row["candidate"]["target_base"]
        (self.workspace / "upstream.txt").write_text("upstream\n")
        self.run_git("add", "upstream.txt", cwd=self.workspace)
        self.run_git("commit", "-qm", "upstream", cwd=self.workspace)
        base = self.run_git("rev-parse", "HEAD", cwd=self.workspace).decode().strip()
        self.run_git(repo, "fetch", str(self.workspace), "HEAD")
        self.run_git(repo, "update-ref", "refs/heads/main", base, before)
        eventually(
            lambda: service.delivery(row["id"])["state"] == "integration_required", 10
        )
        service.refresh_delivery(row["id"], request_id="refresh-base")
        refreshed = eventually(lambda: self.ready(service, work), 45)
        self.assertEqual(len(self.peer.pulls), 1)
        self.assertEqual(refreshed["candidate"]["target_base"], base)
        self.assertNotEqual(
            refreshed["candidate"]["commit"], row["candidate"]["commit"]
        )
        self.peer.merge_pull(1)
        eventually(lambda: work.snapshot().state == "delivered", 40)
