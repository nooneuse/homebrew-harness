"""Own-PR feedback retains its Work and advances through native repair policy."""

import copy
import time
import unittest
from types import SimpleNamespace

from homebrew_harness.platforms.contracts import ObjectRef, SourceEvent
from homebrew_harness.runs.spec import RunSpec
from homebrew_harness.services.communication import followup
from homebrew_harness.services.communication.spec import validate_proposal
from homebrew_harness.services.delivery.store import DeliveryStore
from homebrew_harness.services.job_contracts import JobContract
from homebrew_harness.services.communication.store import CommunicationStore
from homebrew_harness.services.protocol import fingerprint
from tests.services.communication import test_flow as fixture
from unittest.mock import patch


class OwnPRFollowup(unittest.TestCase):
    setUp = fixture.CommunicationFlow.setUp
    start = fixture.CommunicationFlow.start

    def owned(self, *, auto_repair=True):
        service = self.start(auto_intents=["own_pr_reply"], poll_seconds=43200)
        service.pause("all")
        communication = service.runtime.maintenance.communication
        communication.stop.set()
        self.assertTrue(communication.finished.wait(5))
        store = communication.store
        ms = store.ms
        delivery = SimpleNamespace(preparing=False, spec={
            "auto_ci_fix": auto_repair, "ci_fix_limit": None, "ci_fix_warning": 2,
            "authorization_seconds": None, "base_update": "manual",
        })
        delivery.store = DeliveryStore(service.runtime, ms, delivery.spec)
        ms.delivery = delivery
        subject = ObjectRef("github", "primary", "example/project", "change_request", "2", 2).data()
        with store.db.write() as conn:
            work = ms.store.enqueue(conn, service.id, "Preserve the value contract.", "own-pr-work")
            work.update(state="review", run_spec=RunSpec("Preserve the value contract.", write_paths=("src",)).data(),
                        changeset_id="changeset_fixture", authority={"scope": "code", "revoked": False,
                        "policy": fingerprint(ms.policy),
                        "requirements_revision": work["requirements_revision"], "expires_at": None})
            store.db._put(conn, "service_work", work["id"], work)
            candidate = ms.put(conn, "changesets", {
                "id": "changeset_fixture", "work_id": work["id"], "state": "merge_ready",
                "requirements_revision": work["requirements_revision"], "revision": 0,
                "policy": delivery.store.policy, "run_id": None, "change_request": subject,
                "candidate": {"commit": "a" * 40}, "quality": [], "review": {"findings": []},
            })
            action = ms.put(conn, "actions", {
                "id": "delivery_fixture", "kind": "ensure_change_request", "state": "confirmed",
                "work_id": work["id"], "changeset_id": candidate["id"],
                "plan": {"commit": "a" * 40, "body": "The candidate."},
                "result": {"subject": subject, "object": subject, "actor_id": 7},
            })
            store.link_delivery(conn, action)
        row = store.page("discussions")["items"][0]
        with store.db.write() as conn:
            row = store.get("discussions", row["id"], conn)
            row["observed_basis"] = {"state": "open", "head": "a" * 40, "base": "b" * 40}
            store.put(conn, "discussions", row)
        return service, store, service.store.work(work["id"]), candidate, row, action

    def feedback(self, store, row, identity="101", body="Please preserve zero values."):
        event = SourceEvent(ObjectRef("github", "primary", "example/project", "comment", identity),
                            ObjectRef(**row["subject"]), identity,
                            {"body": body, "actor_id": 11, "metadata": {"api_object_id": identity}})
        store.ingest("project", [event], event.subject.scope())
        return store.event_identity(event)

    def propose(self, store, row, source, *, finish=True):
        store.queue_round(store.get("discussions", row["id"]), force=True)
        job = store.ms.claim_job()
        proposal = {"next_step": "repair", "reason": "The feedback is within the accepted value contract.",
                    "alignment": "Preserve the accepted behavior.", "requirements_changed": False,
                    "repair": {"plan": "Preserve zero values and add a regression test.", "sources": [source]}}
        if finish:
            store.ms.finish(job, proposal)
        return job, proposal

    def test_publication_starts_tracking_without_waiting_for_repository_poll(self):
        service, store, work, candidate, row, action = self.owned()
        current = store.get("discussions", row["id"])
        self.assertEqual(current["work_id"], work["id"])
        self.assertEqual(current["next_poll"], 0)
        self.assertEqual(current["delivery_publication"]["head"], candidate["candidate"]["commit"])
        with store.db.write() as conn:
            store.link_delivery(conn, action)
        self.assertEqual(store.get("discussions", row["id"]), current)

    def test_feedback_admits_native_repair_without_replacing_work_or_requirements(self):
        service, store, work, candidate, row, _ = self.owned()
        source = self.feedback(store, row)
        job, _ = self.propose(store, row, source)
        self.assertEqual(job["input"]["own_pr"]["work"]["id"], work["id"])
        self.assertTrue(followup.promote(store, candidate))
        saved = service.store.work(work["id"])
        self.assertEqual(saved["requirements_revision"], work["requirements_revision"])
        self.assertEqual(saved["authority"], work["authority"])
        self.assertEqual(saved["run_spec"], work["run_spec"])
        self.assertEqual(saved["events"], [source])
        repaired = store.ms.get("changesets", candidate["id"])
        self.assertEqual(repaired["state"], "repair_approved")
        self.assertEqual(repaired["change_request"], row["subject"])
        self.assertIn(source, repaired["ci_diagnosis"]["plan"])
        self.assertFalse(followup.promote(store, repaired))
        self.assertFalse(any(r.kind == "discussion_requirements" for r in service.requests()))

    def test_manual_repair_uses_existing_scoped_request(self):
        service, store, work, candidate, row, _ = self.owned(auto_repair=False)
        self.propose(store, row, self.feedback(store, row))
        self.assertTrue(followup.promote(store, candidate))
        self.assertEqual(store.ms.get("changesets", candidate["id"])["state"], "repair_waiting")
        request = store.ms.delivery.store.repair_decision(store.ms.get("changesets", candidate["id"]))
        self.assertEqual(request["kind"], "ci_repair")
        self.assertEqual(request["state"], "pending")
        self.assertEqual(service.store.work(work["id"])["requirements_revision"], work["requirements_revision"])

    def test_new_feedback_requires_reassessment_before_repair(self):
        _, store, _, candidate, row, _ = self.owned()
        self.propose(store, row, self.feedback(store, row))
        self.feedback(store, row, "102", "Correction: preserve negative values too.")
        self.assertTrue(followup.promote(store, candidate))
        self.assertEqual(store.ms.get("changesets", candidate["id"])["state"], "merge_ready")
        current = store.get("discussions", row["id"])
        self.assertEqual(current["repair"]["state"], "needs_reassessment")
        self.assertEqual(current["state"], "ready_to_assess")

    def test_paused_work_and_unsettled_publication_do_not_start_repairs(self):
        service, store, work, candidate, row, action = self.owned()
        self.propose(store, row, self.feedback(store, row))
        service.store.update_work(service.runtime.header, work["id"], pause_requested=True)
        self.assertFalse(followup.promote(store, candidate))
        service.store.update_work(service.runtime.header, work["id"], pause_requested=False)
        with store.db.write() as conn:
            store.ms.put(conn, "actions", {**action, "state": "unknown"})
        self.assertFalse(followup.promote(store, candidate))

    def test_confirmed_repair_head_wakes_discussion_once_with_receipt(self):
        service, store, work, candidate, row, action = self.owned()
        self.propose(store, row, self.feedback(store, row))
        followup.promote(store, candidate)
        with store.db.write() as conn:
            updated = store.ms.put(conn, "changesets", {**candidate, "id": "changeset_repaired",
                "previous": candidate["id"], "candidate": {"commit": "c" * 40}, "state": "ci_waiting"})
            latest = store.db._get(conn, "service_work", work["id"])
            latest["changeset_id"] = updated["id"]
            store.db._put(conn, "service_work", work["id"], latest)
            published = store.ms.put(conn, "actions", {**action, "id": "delivery_repaired",
                "changeset_id": updated["id"], "plan": {"commit": "c" * 40, "body": "Updated candidate."}})
            store.link_delivery(conn, published)
        current = store.get("discussions", row["id"])
        self.assertEqual(current["repair"]["state"], "published")
        self.assertEqual(current["repair"]["publication_action"], published["id"])
        self.assertEqual(current["state"], "ready_to_assess")
        with store.db.write() as conn:
            store.link_delivery(conn, published)
        self.assertEqual(store.get("discussions", row["id"]), current)

    def test_reply_only_does_not_create_repair_and_uses_scoped_preauthorization(self):
        service, store, _, _, row, _ = self.owned()
        self.feedback(store, row)
        store.queue_round(store.get("discussions", row["id"]), force=True)
        job = store.ms.claim_job()
        store.ms.finish(job, {"next_step": "wait", "reason": "Explain the existing behavior.",
            "alignment": "No code change required.", "communication": {
                "kind": "post_comment", "intent": "own_pr_reply", "body": "The current implementation already preserves zero."}})
        current = store.get("discussions", row["id"])
        self.assertNotIn("repair", current)
        action = store.get("communication_actions", current["last_action"])
        self.assertEqual(action["state"], "queued")
        self.assertEqual(action["authorization"], "host_policy")
        self.assertFalse(service.requests())

    def test_repair_rejects_unowned_pr_foreign_events_and_new_requirements(self):
        _, store, _, _, row, _ = self.owned()
        job, proposal = self.propose(store, row, self.feedback(store, row), finish=False)
        foreign = copy.deepcopy(proposal)
        foreign["repair"]["sources"] = ["event_missing"]
        with self.assertRaisesRegex(ValueError, "actual feedback"):
            store.validate_evidence(job, foreign)
        with self.assertRaisesRegex(ValueError, "preserves accepted requirements"):
            validate_proposal({**proposal, "requirements_changed": True}, store.ms.policy)
        unknown = {**job, "input": {k: v for k, v in job["input"].items() if k != "own_pr"}}
        with self.assertRaisesRegex(ValueError, "published PR"):
            store.validate_evidence(unknown, proposal)

    def test_discussion_evidence_reads_only_its_own_candidate(self):
        _, store, _, candidate, row, _ = self.owned()
        job, _ = self.propose(store, row, self.feedback(store, row), finish=False)
        contract = JobContract(store.ms, job)
        contract.authorize("changesets", candidate["id"])
        with self.assertRaisesRegex(ValueError, "unrelated internal evidence"):
            contract.authorize("changesets", "changeset_another_work")
        from homebrew_harness.services.agent import ServiceAgent
        from homebrew_harness.tools.catalog import SERVICE_TOOL_DEFINITIONS

        agent = SimpleNamespace(job=job, store=store.ms)
        definitions = ServiceAgent.describe_tools(agent, SERVICE_TOOL_DEFINITIONS)
        schema = next(d["function"]["parameters"]["properties"] for d in definitions
                      if d["function"]["name"] == "service_read")
        self.assertIn("changesets", schema["kind"]["enum"])
        self.assertIn("quality_output", schema["kind"]["enum"])
        self.assertNotIn("enum", schema["identity"])

    def test_pending_feedback_blocks_service_merge_without_claiming_completion(self):
        _, store, _, candidate, row, _ = self.owned()
        self.feedback(store, row)
        with store.db.write() as conn:
            self.assertTrue(followup.pending(store, candidate, conn))
            result = store.ms.delivery.store.action(conn, candidate, "merge_change_request")
        self.assertIsNone(result)
        saved = store.ms.get("changesets", candidate["id"])
        self.assertEqual(saved["state"], "review_waiting")
        self.assertEqual(saved["error"], "pr_feedback_pending")

    def test_owned_active_pr_polls_soon_and_other_discussions_keep_their_interval(self):
        service, store, _, _, row, _ = self.owned()
        communication = service.runtime.maintenance.communication
        communication.stop.clear()
        with patch.object(communication, "freshness"):
            before = time.time()
            communication.poll(row)
            delay = store.get("discussions", row["id"])["next_poll"] - before
            self.assertGreaterEqual(delay, 120)
            self.assertLess(delay, 125)
            with store.db.write() as conn:
                foreign = store.get("discussions", row["id"], conn)
                foreign["work_id"] = None
                store.put(conn, "discussions", foreign)
            before = time.time()
            communication.poll(foreign)
            self.assertGreaterEqual(store.get("discussions", row["id"])["next_poll"] - before, 43200)

    def test_own_reply_does_not_preauthorize_an_unowned_pr(self):
        _, store, _, _, row, _ = self.owned()
        with store.db.write() as conn, self.assertRaisesRegex(ValueError, "confirmed PR and Work"):
            store.plan(conn, {**row, "work_id": None}, {
                "kind": "post_comment", "intent": "own_pr_reply", "body": "An explanation."})

    def test_reply_uses_native_outbox_and_reconciles_lost_http_receipt(self):
        service, store, _, candidate, row, _ = self.owned()
        communication = service.runtime.maintenance.communication
        self.peer.issue(pr=True)
        self.peer.issues[0].update(number=2, id=2)
        self.peer.head = candidate["candidate"]["commit"]
        communication.stop.clear()
        communication.freshness(row)
        row = store.get("discussions", row["id"])
        store.queue_round(row, force=True)
        job = store.ms.claim_job()
        store.ms.finish(job, {"next_step": "wait", "reason": "Explain the current code with no change.",
            "alignment": "Same accepted contract.", "communication": {
                "kind": "post_comment", "intent": "own_pr_reply", "body": "The current implementation preserves zero values."}})
        row = store.get("discussions", row["id"])
        action = store.get("communication_actions", row["last_action"])
        service.resume()
        self.peer.drop = "comment"
        communication.execute(action)
        uncertain = store.get("communication_actions", action["id"])
        self.assertEqual(uncertain["state"], "unknown")
        self.assertEqual(len(self.peer.writes), 1)
        self.peer.drop = None
        communication.reconcile(uncertain["id"])
        self.assertEqual(store.get("communication_actions", action["id"])["state"], "confirmed")
        self.assertEqual(len(self.peer.writes), 1)
        self.assertEqual(self.peer.writes[0][1]["number"], 2)

    def test_recovery_retains_pending_repair_and_can_discover_an_older_publication(self):
        service, store, work, candidate, row, _ = self.owned()
        self.propose(store, row, self.feedback(store, row))
        before = store.get("discussions", row["id"])
        restored = CommunicationStore(service.runtime, store.ms, store.policy)
        restored.recover()
        self.assertEqual(restored.get("discussions", row["id"]), before)
        self.assertTrue(followup.promote(restored, candidate))
        with store.db.write() as conn:
            conn.execute("DELETE FROM records WHERE section='service_discussions' AND key=?", (row["id"],))
        restored.recover()
        discovered = restored.get("discussions", row["id"])
        self.assertEqual(discovered["work_id"], work["id"])
        self.assertEqual(discovered["next_poll"], 0)

    def test_model_can_propose_repair_through_the_registered_service_loop(self):
        service, store, _, candidate, row, _ = self.owned()
        communication = service.runtime.maintenance.communication
        self.peer.issue(pr=True)
        self.peer.issues[0].update(number=2, id=2)
        self.peer.head = candidate["candidate"]["commit"]
        communication.stop.clear()
        communication.freshness(row)
        communication.prepare_review(store.get("discussions", row["id"]))
        job, proposal = self.propose(store, row, self.feedback(store, row), finish=False)
        self.peer.proposals = [proposal]
        result = service.runtime.maintenance.agent.run(job)
        self.assertEqual(result["repair"], proposal["repair"])
        store.ms.finish(job, result)
        self.assertTrue(followup.promote(store, candidate))
        request = self.peer.models[-1]
        system = "\n".join(m.get("content", "") for m in request["messages"] if m["role"] == "system")
        self.assertIn("next_step=repair", system)

    def test_followup_survives_an_additional_ci_repair_before_publication(self):
        _, store, _, candidate, row, action = self.owned()
        self.propose(store, row, self.feedback(store, row))
        followup.promote(store, candidate)
        with store.db.write() as conn:
            intermediate = store.ms.put(conn, "changesets", {
                **candidate, "id": "changeset_ci_failed", "previous": candidate["id"]})
            final = store.ms.put(conn, "changesets", {
                **candidate, "id": "changeset_ci_fixed", "previous": intermediate["id"],
                "candidate": {"commit": "d" * 40}})
            confirmed = store.ms.put(conn, "actions", {**action, "id": "delivery_ci_fixed",
                "changeset_id": final["id"], "plan": {"commit": "d" * 40, "body": "Verified fix."}})
            store.link_delivery(conn, confirmed)
        repair = store.get("discussions", row["id"])["repair"]
        self.assertEqual(repair["state"], "published")
        self.assertEqual(repair["published_head"], "d" * 40)

    def test_handled_new_feedback_changes_merge_basis_without_a_code_change(self):
        _, store, _, candidate, row, _ = self.owned()
        with store.db.connection() as conn:
            previous = followup.basis(store, candidate, conn)
        self.feedback(store, row)
        store.queue_round(store.get("discussions", row["id"]), force=True)
        job = store.ms.claim_job()
        store.ms.finish(job, {"next_step": "settle", "reason": "The explanation resolves this comment.",
                             "alignment": "No code change is needed."})
        with store.db.connection() as conn:
            self.assertFalse(followup.pending(store, candidate, conn))
            self.assertNotEqual(followup.basis(store, candidate, conn), previous)
        self.assertEqual(store.ms.get("changesets", candidate["id"])["candidate"]["commit"], "a" * 40)
