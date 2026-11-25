"""Closed PR discussions retain the choice to reply without claiming code evidence."""

import json
import unittest
from unittest.mock import patch

from homebrew_harness.platforms.contracts import ObjectRef, SourceEvent
from homebrew_harness.services.communication.reasoning import work_status
from tests.runtime.test_interaction import eventually
from tests.services.communication import test_flow, test_own_pr_followup


class TerminalDiscussionTests(unittest.TestCase):
    setUp = test_flow.CommunicationFlow.setUp
    start = test_flow.CommunicationFlow.start
    owned = test_own_pr_followup.OwnPRFollowup.owned
    feedback = test_own_pr_followup.OwnPRFollowup.feedback

    def assess_closed(self, *, reply):
        service = self.start(poll_seconds=43200)
        service.pause("all")
        communication = service.runtime.maintenance.communication
        store = communication.store
        ref = ObjectRef("github", "primary", "example/project", "change_request", "1", 1)
        basis = {"state": "closed", "head": self.peer.head, "base": self.peer.base,
                 "head_project": ref.project, "base_project": ref.project}
        event = SourceEvent(ref, ref, "closed-observation", {"state": "closed", "metadata": {"review_basis": basis}})
        store.ingest("project", [event], ref.scope(), observed=True)
        row = store.get("discussions", store.page("discussions")["items"][0]["id"])
        historical = [{"id": "old", "text": "Earlier review finding", "review_id": "review_previous"}]
        with store.db.write() as conn:
            row["projection"]["findings"] = historical
            store.put(conn, "discussions", row)
        self.assertFalse(communication.should_prepare_review(row))
        store.queue_round(row, force=True)
        job = store.ms.claim_job()
        self.assertIsNone(job["input"]["review_id"])
        self.assertEqual(job["input"]["projection"]["findings"], [])
        agent = service.runtime.maintenance.agent

        def proposal(_body):
            result = {"next_step": "wait", "reason": "Recorded PR closure.", "alignment": "Preserve history."}
            if reply:
                result["communication"] = {"kind": "post_comment", "intent": "followup",
                                           "body": "This PR is no longer open; the earlier feedback is still recorded."}
            return result

        self.peer.proposals = [proposal]
        with patch.object(agent, "code_root", side_effect=AssertionError("snapshotless job opened a code root")):
            result = agent.run(job)
        self.assertEqual(result["reason"], "Recorded PR closure.")
        self.assertEqual(bool(result.get("communication")), reply)
        request = self.peer.models[0]
        packet = json.loads(next(m["content"] for m in request["messages"]
                                 if m["role"] == "user" and '"job":' in m["content"]))
        self.assertEqual(packet["input"]["observed_pr"], basis)
        self.assertFalse(packet["input"]["code_evidence_available"])
        tools = {tool["function"]["name"] for tool in request["tools"]}
        self.assertFalse(tools & {"service_read_code", "glob", "search_text"})
        store.ms.finish(job, result)
        self.assertEqual(store.ms.job_get(job["id"])["state"], "completed")
        self.assertEqual(store.get("discussions", row["id"])["projection"]["findings"], historical)
        requests = list(service.requests())
        self.assertEqual([req.kind for req in requests], ["communication_authority"] if reply else [])
        self.assertEqual(self.peer.writes, [])
        with self.assertRaisesRegex(ValueError, "fixed review snapshot"):
            agent.code_root()
        self.assertIsNone(store.queue_round(store.get("discussions", row["id"])))
        return service, communication, row

    def test_merged_event_runs_without_automatically_preparing_code(self):
        self.peer.issue(pr=True)
        self.peer.issues[0].update(state="closed", merged=True, merge_commit_sha="4" * 40)
        service = self.start()
        store = service.runtime.maintenance.communication.store

        def assessed():
            for row in store.page("discussions")["items"]:
                current = store.get("discussions", row["id"])
                if ((current.get("observed_basis") or {}).get("merged") is True
                        and current["state"] == "waiting_participant"):
                    return current
            return None

        row = eventually(assessed, 20)
        job = store.ms.job_get(row["last_job"])
        self.assertEqual(job["state"], "completed")
        self.assertEqual(job["attempts"], 1)
        self.assertEqual(job["input"]["observed_pr"]["result_commit"], "4" * 40)
        self.assertIsNone(row["review_id"])
        self.assertFalse(store.page("review_operations")["items"])
        self.assertFalse(self.peer.writes)

    def test_model_can_choose_no_reply_without_code_snapshot(self):
        self.assess_closed(reply=False)

    def test_model_can_propose_reply_without_code_snapshot(self):
        self.assess_closed(reply=True)

    def test_operator_can_prepare_fixed_evidence_on_closed_pr_without_reopening_it(self):
        service, communication, row = self.assess_closed(reply=False)
        store = communication.store
        self.peer.issue(pr=True)
        self.peer.issues[0]["state"] = "closed"
        operation = service.control_discussion(row["id"], "prepare_review", request_id="closed-code-question")
        current = store.get("discussions", row["id"])
        self.assertTrue(communication.should_prepare_review(current))
        result = communication.prepare_review(current)
        self.assertEqual(result["id"], operation["id"])
        self.assertEqual(result["state"], "completed")
        current = store.get("discussions", row["id"])
        store.queue_round(current, force=True)
        job = store.ms.claim_job()
        agent = service.runtime.maintenance.agent
        agent.job = job
        self.assertEqual((agent.code_root() / "src/value.py").read_text(), self.peer.files["src/value.py"])
        with store.db.write() as conn, self.assertRaisesRegex(ValueError, "not a new code review"):
            store.validate_anchors(current, {"kind": "submit_review"}, conn)
        self.assertEqual(self.peer.writes, [])

    def test_recovery_retires_only_the_amendment_for_delivered_work(self):
        service, store, work, candidate, pr, _ = self.owned()
        issue = ObjectRef("github", "primary", "example/project", "issue", "1", 1)
        store.ingest("project", [SourceEvent(issue, issue, "v1", {"body": "Please add a regression."})], issue.scope())
        draft = {"text": "Add a follow-up test.", "scope": "code", "acceptance": "Test passes."}
        with store.db.write() as conn:
            row = next(r for r in store.page("discussions")["items"] if r["subject"]["kind"] == "issue")
            row = store.get("discussions", row["id"], conn)
            row.update(work_id=work["id"], state="ready_for_decision")
            row["projection"]["draft"] = draft
            store.put(conn, "discussions", row)
            decision = store.requirements_request(conn, row, {"reason": "Review feedback."})
            independent = {**decision, "id": "request_independent", "work_id": None}
            store.ms.put(conn, "decisions", independent)
            durable = store.db._get(conn, "service_work", work["id"])
            durable["state"] = "delivered"
            store.db._put(conn, "service_work", work["id"], durable)
            candidate.update(state="delivered", merge={"subject": pr["subject"], "state": "merged",
                             "head": "a" * 40, "result_commit": "c" * 40}, integration={"commit": "c" * 40})
            store.ms.put(conn, "changesets", candidate)
            with self.assertRaisesRegex(ValueError, "already been delivered"):
                store.accept_requirements(conn, row, draft, "bug")
        store.recover()
        store.recover()
        self.assertEqual(store.ms.get("decisions", decision["id"])["unavailable_reason"], "work_already_delivered")
        self.assertEqual(store.ms.get("decisions", decision["id"])["candidate"], draft)
        self.assertEqual(store.ms.get("decisions", independent["id"])["state"], "pending")
        row = store.get("discussions", row["id"])
        self.assertEqual(row["state"], "ready_to_assess")
        self.assertEqual(row["projection"]["draft"], draft)
        status = work_status(store.ms, {"input": {"work_id": work["id"], "subject": issue.data()}})
        self.assertTrue(status["merge"]["verified"])
        self.assertFalse(status["development_authorized"])
