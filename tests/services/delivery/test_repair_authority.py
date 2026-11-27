"""A repair approval admits one known attempt and survives unstarted recovery."""

import copy
import time
import unittest
from unittest.mock import patch

from homebrew_harness import RemoteService
from homebrew_harness.contracts import SDKError
from homebrew_harness.services.delivery.runtime import Delivery
from tests.runtime.test_interaction import eventually
from tests.services.delivery import test_target_refresh as fixture


class RepairAuthorityTests(unittest.TestCase):
    setUp = fixture.TargetRefreshTests.setUp
    start = fixture.TargetRefreshTests.start
    run_git = fixture.TargetRefreshTests.run_git
    ready = fixture.TargetRefreshTests.ready
    row = fixture.TargetRefreshTests.row
    advance_target = fixture.TargetRefreshTests.advance_target
    conflict_request = fixture.TargetRefreshTests.conflict_request

    def facts(self, row):
        return {key: copy.deepcopy(row.get(key)) for key in
                ("state", "revision", "candidate", "quality", "review", "ci_diagnosis",
                 "candidate_history", "frozen", "error", "error_info")}

    def pending(self):
        service = self.start(ci_fix_limit=0, merge={"mode": "manual", "poll_seconds": .1})
        work = service.work.submit("Integrate the existing scoped value.")
        old = eventually(lambda: self.ready(service, work), 40)
        self.advance_target("src/value.txt", "3\n")
        request = eventually(lambda: self.conflict_request(service), 40)
        service.pause("all")
        return service, work, request, old

    def test_extra_attempt_requires_explicit_admission_and_cannot_be_reused(self):
        service, work, request, old = self.pending()
        store = service.runtime.maintenance.delivery.store
        before = copy.deepcopy(self.row(service, work))
        decision = copy.deepcopy(store.get("decisions", request.ref.id))
        with RemoteService.from_root(self.control, control=True) as remote:
            observed = next(r for r in remote.requests(work_id=work.id) if r.kind == "ci_repair")
            self.assertEqual(observed.arguments["repair_attempts"], {"used": 0, "limit": 0, "extra_required": True})
            self.assertIn("one additional attempt", observed.question)
            with self.assertRaisesRegex(SDKError, "one-attempt extension"):
                remote.decide(observed, "approve", decision_id="reject-ungranted-extra")
            self.assertEqual(self.facts(self.row(service, work)), self.facts(before))
            self.assertEqual(store.get("decisions", request.ref.id), decision)
            remote.decide(observed, "approve", allow_extra_attempt=True, decision_id="allow-one-extra")
        current = self.row(service, work)
        accepted = store.get("decisions", request.ref.id)
        self.assertEqual(accepted["authorized_attempt"], 1)
        self.assertTrue(accepted["extra_attempt"])
        self.assertTrue(store.repair_allowed(current, service.store.work(work.id)))
        self.assertEqual(service.store.work(work.id).get("ci_fix_attempts", 0), 0)
        service.resume()
        repaired = eventually(lambda: self.ready(service, work), 50)
        self.assertEqual(service.store.work(work.id)["ci_fix_attempts"], 1)
        self.assertEqual(store.spec["ci_fix_limit"], 0)
        self.assertFalse(store.repair_allowed(current, service.store.work(work.id)))
        self.assertEqual(repaired["change_request"], old["change_request"])
        self.assertEqual(len(self.peer.pulls), 1)

    def test_baseline_extra_approval_can_retry_failed_preparation_once(self):
        self.peer.ci_value = {"next_action": "repair", "classification": "baseline", "reason": "The target moved.",
                              "plan": "Resolve src/value.txt to 2 and retain its verification."}
        service, work, public, old = self.pending()
        delivery = service.runtime.maintenance.delivery
        store = delivery.store
        request = store.get("decisions", public.ref.id)
        self.assertEqual(self.row(service, work)["ci_diagnosis"]["classification"], "baseline")
        service.decide(public, "approve", allow_extra_attempt=True, decision_id="accept-baseline-extra")
        original = Delivery.repair
        def failed(*args):
            raise ValueError("Synthetic preparation interruption before Run creation")
        with patch.object(Delivery, "repair", failed):
            service.resume()
            eventually(lambda: self.row(service, work)["state"] == "failed", 15)
            service.pause("all")
        prior = copy.deepcopy(self.row(service, work))
        run = service.store.work(work.id)["run_id"]
        calls = len(self.peer.model_requests)
        result = service.refresh_delivery(prior["id"], request_id="resume-accepted-baseline")
        self.assertEqual(result["state"], "repair_approved")
        self.assertEqual(self.row(service, work)["candidate_history"], prior["candidate_history"])
        self.assertEqual(service.store.work(work.id)["run_id"], run)
        self.assertEqual(len(self.peer.model_requests), calls)
        service.resume()
        eventually(lambda: self.ready(service, work), 50)
        self.assertEqual(service.store.work(work.id)["ci_fix_attempts"], 1)
        self.assertEqual(store.get("decisions", request["id"])["authorized_attempt"], 1)

    def test_expired_pending_decision_renews_without_replaying_diagnosis(self):
        service, work, request, old = self.pending()
        store = service.runtime.maintenance.delivery.store
        expired = store.update("decisions", request.ref.id, expires_at=time.time() - 1)
        before = copy.deepcopy(self.row(service, work))
        calls = len(self.peer.model_requests)
        service.refresh_delivery(before["id"], request_id="renew-pending-repair")
        current = store.get("decisions", request.ref.id)
        self.assertEqual(current["revision"], expired["revision"] + 1)
        self.assertIsNone(current["expires_at"])
        self.assertEqual(current["candidate"], expired["candidate"])
        self.assertEqual(current["state"], "pending")
        self.assertEqual(self.facts(self.row(service, work)), self.facts(before))
        self.assertEqual(len(self.peer.model_requests), calls)
        with self.assertRaises(SDKError):
            service.decide(request, "approve", allow_extra_attempt=True, decision_id="stale-ref")

    def test_inbox_refresh_keeps_ci_plan_and_extra_attempt_requirement(self):
        service, work, request, _ = self.pending()
        store = service.runtime.maintenance.delivery.store
        old = store.update("decisions", request.ref.id, expires_at=time.time() - 1)
        inbox = service.inbox(work.id, include_expired=True)
        item = next(item for item in inbox["items"] if item["kind"] == "ci_repair")
        self.assertEqual(item["state"], "expired")
        self.assertIs(item["extra_required"], True)
        result = service.refresh_expired([request.ref.id], request_id="refresh-ci-inbox")
        self.assertEqual(result["skipped"], [])
        self.assertEqual(result["refreshed"][0]["revision"], old["revision"] + 1)
        current = store.get("decisions", request.ref.id)
        self.assertEqual(current["candidate"], old["candidate"])
        self.assertEqual(current["state"], "pending")
        self.assertNotIn("authorized_attempt", current)

    def test_non_boolean_or_denied_extra_grants_cannot_mutate_state(self):
        service, work, request, old = self.pending()
        store = service.runtime.maintenance.delivery.store
        before = copy.deepcopy(self.row(service, work))
        for value in (1, "true"):
            with self.subTest(value=value), self.assertRaises(SDKError):
                service.decide(request, "approve", allow_extra_attempt=value, decision_id="invalid-extra-" + str(value))
        with self.assertRaises(SDKError):
            service.decide(request, "deny", allow_extra_attempt=True, decision_id="deny-extra")
        self.assertEqual(self.facts(self.row(service, work)), self.facts(before))
        self.assertEqual(store.get("decisions", request.ref.id)["state"], "pending")

    def test_grouped_extra_approval_keeps_the_same_one_attempt_authority(self):
        service, work, request, old = self.pending()
        batch = service.batch([request], request_id="group-repair")
        refused = service.decide_batch(batch["id"], [{"id": request.ref.id, "decision": "approve"}],
                                       request_id="group-without-extra")
        self.assertEqual(refused["results"][request.ref.id]["error_info"]["code"], "ci_repair_limit")
        accepted = service.decide_batch(batch["id"], [{"id": request.ref.id, "decision": "approve",
            "allow_extra_attempt": True}], request_id="group-one-extra")
        self.assertEqual(accepted["results"][request.ref.id]["state"], "confirmed")
        self.assertEqual(service.decide_batch(batch["id"], [{"id": request.ref.id, "decision": "approve",
            "allow_extra_attempt": True}], request_id="group-one-extra"), accepted)
        record = service.runtime.maintenance.delivery.store.get("decisions", request.ref.id)
        self.assertEqual(record["authorized_attempt"], 1)
        self.assertTrue(record["extra_attempt"])

    def test_extra_attempt_does_not_bypass_original_work_authority_renewal(self):
        service, work, request, old = self.pending()
        original_run = service.store.work(work.id)["run_id"]
        with service.store.db.write() as conn:
            current = service.store.work(work.id)
            current["authority"]["expires_at"] = time.time() - 1
            service.store.db._put(conn, "service_work", work.id, current)
        service.decide(request, "approve", allow_extra_attempt=True, decision_id="accept-extra-with-expired-scope")
        service.resume()
        renewal = eventually(lambda: next((r for r in service.requests() if r.kind == "work_authority"), None), 20)
        service.pause("all")
        current = service.store.work(work.id)
        self.assertEqual(current["run_id"], original_run)
        self.assertEqual(current.get("ci_fix_attempts", 0), 0)
        self.assertEqual(self.row(service, work)["state"], "repair_waiting_authority")
        service.decide(renewal, "approve", decision_id="renew-original-code-scope")
        self.assertEqual(service.store.work(work.id)["run_id"], original_run)
        self.assertEqual(service.runtime.maintenance.delivery.store.get("decisions", request.ref.id)["authorized_attempt"], 1)
        service.resume()
        eventually(lambda: self.ready(service, work), 50)
        self.assertEqual(service.store.work(work.id)["ci_fix_attempts"], 1)

    def test_a_changed_plan_cannot_use_its_old_extra_attempt_request(self):
        service, work, request, old = self.pending()
        store = service.runtime.maintenance.delivery.store
        current = self.row(service, work)
        store.update("changesets", current["id"], ci_diagnosis={**current["ci_diagnosis"],
            "plan": "A different bounded repair must receive its own decision."})
        before = self.facts(self.row(service, work))
        with self.assertRaises(SDKError):
            service.decide(request, "approve", allow_extra_attempt=True, decision_id="stale-plan-extra")
        self.assertEqual(self.facts(self.row(service, work)), before)
        self.assertEqual(store.get("decisions", request.ref.id)["state"], "pending")
        self.assertFalse(any(r.ref.id == request.ref.id for r in service.requests()))
