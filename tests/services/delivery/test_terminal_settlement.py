"""A manual merge retires outstanding branch and PR approvals, not effect receipts."""

import copy
import unittest

from homebrew_harness.services.delivery.merge_flow import observe
from homebrew_harness.services.protocol import request_key
from tests.runtime.test_interaction import eventually
from tests.services.delivery import test_merge_flow


class TerminalDeliveryTests(unittest.TestCase):
    setUp = test_merge_flow.MergeFlowTests.setUp
    run_git = test_merge_flow.MergeFlowTests.run_git
    start = test_merge_flow.MergeFlowTests.start
    ready = test_merge_flow.MergeFlowTests.ready
    manual = test_merge_flow.MergeFlowTests.manual

    def test_manual_merge_retires_same_pr_actions_before_delivering_work(self):
        service, work, candidate = self.manual()
        service.pause("all")
        delivery = service.runtime.maintenance.delivery
        store = delivery.store
        with store.db.write() as conn:
            branch = store.action(conn, candidate, "publish_branch", draft=True)
            pr = store.action(conn, candidate, "ensure_change_request", draft=True,
                              publication={"title": "Refreshed package", "body": "Same PR"})
            branch_request = store.request(conn, candidate, branch)
            pr_request = store.request(conn, candidate, pr)
            # Use real action records with synthetic durable states to exercise
            # recovery without firing a second write at the platform.
            unrelated = {**copy.deepcopy(pr), "id": "delivery_other", "changeset_id": "changeset_other"}
            other_candidate = {**candidate, "id": "changeset_other", "work_id": "work_other",
                               "branch": "harness/other", "change_request": {**candidate["change_request"], "id": "2", "number": 2}}
            unrelated["work_id"] = other_candidate["work_id"]
            store.put(conn, "changesets", other_candidate)
            store.put(conn, "actions", unrelated)
            uncertain = {**copy.deepcopy(branch), "id": "delivery_uncertain", "state": "unknown"}
            store.put(conn, "actions", uncertain)
        merged = self.peer.merge_pull(1)
        fresh = store.update("changesets", candidate["id"], next_merge=0)
        self.assertTrue(observe(delivery, fresh))
        self.assertEqual(store.get("changesets", candidate["id"])["state"], "merged_verifying")
        self.assertEqual(work.snapshot().state, "review")
        for action, request in ((branch, branch_request), (pr, pr_request)):
            with self.subTest(action=action["kind"]):
                saved = store.get("actions", action["id"])
                self.assertEqual(saved["state"], "superseded")
                self.assertEqual(saved["superseded_by"]["result_commit"], merged)
                self.assertEqual(store.get("decisions", request["id"])["state"], "superseded")
                self.assertEqual(saved["plan"], action["plan"])
                with store.db.write() as conn, self.assertRaisesRegex(ValueError, "already merged"):
                    store.request(conn, store.get("changesets", candidate["id"], conn), action, renew=True)
        self.assertEqual(store.get("actions", unrelated["id"])["state"], unrelated["state"])
        self.assertEqual(store.get("actions", uncertain["id"])["state"], "unknown")
        with store.db.write() as conn:
            with self.assertRaisesRegex(ValueError, "already merged"):
                store.action(conn, candidate, "publish_branch")
            # A pre-upgrade pending action discovered after restart should be
            # superseded even though the merge observation has already landed.
            legacy = {**pr, "id": "delivery_legacy"}
            store.put(conn, "actions", legacy)
            request_id = "request_" + request_key("delivery-request", store.id, legacy["id"])
            store.put(conn, "decisions", {**pr_request, "id": request_id, "action_id": legacy["id"]})
            with self.assertRaisesRegex(ValueError, "already merged"):
                store.decide(conn, {**pr_request, "id": request_id, "action_id": legacy["id"]},
                             {"revision": pr_request["revision"], "decision": "approve"}, "pre-recovery")
            store.put(conn, "actions", {**legacy, "state": "queued"})
            # This synthetic unrelated ChangeSet has no actual Work; remove the
            # fixture row so the background workflow does not try to own it.
            conn.execute("DELETE FROM records WHERE section='service_changesets' AND key=?", (other_candidate["id"],))
            conn.execute("DELETE FROM records WHERE section='service_actions' AND key=?", (unrelated["id"],))
        with self.assertRaisesRegex(ValueError, "already merged"):
            store.start_action(legacy)
        service.stop()
        self.assertTrue(service.wait_closed(15))
        restarted = self.host.services.start(identity=service.id)
        self.addCleanup(lambda: (restarted.close(), restarted.wait_closed(15)))
        store = restarted.runtime.maintenance.delivery.store
        eventually(lambda: store.get("actions", legacy["id"])["state"] == "superseded", 20)
        store.recover_terminal()
        self.assertEqual(store.get("decisions", request_id)["revision"], pr_request["revision"] + 1)
        self.assertEqual(store.get("actions", uncertain["id"])["state"], "unknown")
        eventually(lambda: restarted.store.work(work.id)["state"] == "delivered", 120)
        self.assertEqual(restarted.delivery(candidate["id"])["integration"]["commit"], merged)
        self.assertEqual(len(self.peer.pulls), 1)
        self.assertFalse(any(kind == "merge" for kind, _ in self.peer.writes))
