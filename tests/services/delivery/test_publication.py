"""Public wording recovery preserves reviewed code and external action history."""

import copy
import json
import threading
import unittest
from dataclasses import replace

from homebrew_harness.contracts import SDKError
from homebrew_harness.services.delivery.publication import basis
from tests.runtime.test_interaction import eventually
from tests.services.delivery import test_flow as fixture


class Publication(unittest.TestCase):
    setUp = fixture.FlowTests.setUp
    start = fixture.FlowTests.start
    run_git = fixture.FlowTests.run_git
    pending_target = fixture.FlowTests.pending_target

    draft = {
        "title": "Restore value handling",
        "body": "FROM TEST AGENT\n\nRestore the value. Fixed unit-tests passed.",
    }

    def held(self):
        self.peer.publication_started = threading.Event()
        self.peer.publication_hold = threading.Event()
        self.peer.publication_value = self.draft
        self.addCleanup(self.peer.publication_hold.set)

    def row(self, service):
        return service.runtime.maintenance.delivery.rows("changesets")[-1]

    def test_missing_wording_is_drafted_without_repeating_code_review_or_tests(self):
        self.held()
        guidance = (
            "Public bodies must begin FROM TEST AGENT and describe confirmed changes."
        )
        service = self.start(
            auto_actions=["publish_branch"],
            maintenance={"instructions": {"review": guidance}},
            merge={"mode": "manual"},
        )
        work = service.work.submit("Restore the documented value.")
        self.assertTrue(self.peer.publication_started.wait(20))
        before = copy.deepcopy(self.row(service))
        self.peer.publication_hold.set()
        request = eventually(lambda: self.pending_target(service), 20)
        after = self.row(service)
        self.assertEqual(after["review"], before["review"])
        self.assertEqual(after["quality"], before["quality"])
        self.assertEqual(after["frozen"], before["frozen"])
        self.assertEqual(after["publication"], self.draft)
        self.assertNotIn("publication", after["review"])
        jobs = service.records("jobs")["items"]
        self.assertEqual(sum(job["kind"] == "review" for job in jobs), 1)
        self.assertEqual(sum(job["kind"] == "publication" for job in jobs), 1)
        payloads = [
            json.loads(m["content"])
            for r in self.peer.model_requests
            for m in r["messages"]
            if m.get("role") == "user" and m.get("content", "").startswith('{"job":')
        ]
        prepared = next(p for p in payloads if p["job"]["kind"] == "publication")
        self.assertEqual(prepared["policy"]["instructions"], {"review": guidance})
        service.decide(request, "approve", decision_id="approve-generated-wording")
        eventually(lambda: len(self.peer.pulls) == 1, 15)
        actions = service.records("actions")["items"]
        branches = [action for action in actions if action["kind"] == "publish_branch"]
        self.assertEqual(len(branches), 1)
        self.assertEqual(branches[0]["attempts"], 1)
        self.assertEqual(
            self.peer.pulls[0]["body"].split("\n\n<!--", 1)[0], self.draft["body"]
        )
        self.assertEqual(service.store.work(work.id)["run_id"], before["run_id"])

    def test_opt_in_drafts_and_publishes_wording_before_merge_approval(self):
        self.held()
        service = self.start(auto_publish_pr_text=True)
        work = service.work.submit("Restore the documented value.")
        try:
            if not self.peer.publication_started.wait(60):
                self.fail(f"Publication job did not start: {service.health()['issues']}")
            self.assertEqual(self.row(service)["state"], "publication_drafting")
            self.assertNotIn("publication", self.row(service)["review"])
            self.assertFalse(self.peer.pulls)
            self.assertFalse(
                any(
                    action["kind"] == "ensure_change_request"
                    for action in service.records("actions")["items"]
                )
            )
        finally:
            self.peer.publication_hold.set()

        request = eventually(lambda: self.pending_target(service), 60)
        delivery = service.runtime.maintenance.delivery
        merge = delivery.store.get("actions", request.arguments["action_id"])
        self.assertEqual(merge["kind"], "merge_change_request")
        self.assertEqual(merge["state"], "awaiting_approval")
        self.assertIsNone(merge["authorization"])
        self.assertEqual(merge["attempts"], 0)
        self.assertFalse(any(kind == "merge" for kind, _ in self.peer.writes))

        self.assertEqual(self.row(service)["publication"], self.draft)
        jobs = [
            job for job in service.records("jobs")["items"] if job["kind"] == "publication"
        ]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["state"], "completed")
        actions = [
            action
            for action in delivery.rows("actions")
            if action["kind"] == "ensure_change_request"
        ]
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["state"], "confirmed")
        self.assertEqual(actions[0]["authorization"], "host_policy")
        self.assertIs(actions[0]["plan"].get("draft", False), False)
        self.assertEqual(len(self.peer.pulls), 1)
        self.assertEqual(self.peer.pulls[0]["title"], self.draft["title"])
        self.assertEqual(
            self.peer.pulls[0]["body"].split("\n\n<!--", 1)[0], self.draft["body"]
        )
        pull_writes = [data for kind, data in self.peer.writes if kind == "pull"]
        self.assertEqual(len(pull_writes), 1)
        self.assertIs(pull_writes[0].get("draft", False), False)

        service.decide(request, "approve", decision_id="approve-merge")
        eventually(lambda: work.snapshot().state == "delivered", 80)
        self.assertEqual(sum(kind == "merge" for kind, _ in self.peer.writes), 1)

    def test_denied_wording_gets_a_new_exact_approval_and_keeps_confirmed_branch(self):
        self.held()
        bad = {"title": "Restore value", "body": "An unprefixed draft."}
        self.peer.review_extra = {"publication": bad}
        service = self.start(auto_actions=["publish_branch"], merge={"mode": "manual"})
        service.work.submit("Restore value handling.")
        request = eventually(lambda: self.pending_target(service), 25)
        store = service.runtime.maintenance.delivery.store
        old = copy.deepcopy(store.get("actions", request.arguments["action_id"]))
        before = copy.deepcopy(self.row(service))
        service.decide(request, "deny", decision_id="reject-public-wording")
        self.assertTrue(self.peer.publication_started.wait(15))
        self.peer.publication_hold.set()
        corrected = eventually(lambda: self.pending_target(service), 20)
        self.assertNotEqual(corrected.ref.id, request.ref.id)
        current = self.row(service)
        self.assertEqual(current["candidate"], before["candidate"])
        self.assertEqual(current["review"], before["review"])
        self.assertEqual(current["quality"], before["quality"])
        self.assertEqual(current["frozen"], before["frozen"])
        rejected = store.get("actions", old["id"])
        self.assertEqual(rejected["state"], "denied")
        self.assertEqual(rejected["plan"], old["plan"])
        self.assertEqual(rejected["attempts"], 0)
        replacement = store.get("actions", corrected.arguments["action_id"])
        self.assertEqual(replacement["plan"]["body"], self.draft["body"])
        service.decide(corrected, "approve", decision_id="approve-corrected-wording")
        eventually(lambda: len(self.peer.pulls) == 1, 15)
        self.assertEqual(
            sum(
                action["attempts"]
                for action in service.records("actions")["items"]
                if action["kind"] == "publish_branch"
            ),
            1,
        )

    def test_operator_referral_waits_for_new_current_instructions(self):
        self.peer.publication_value = {
            "human_required": True,
            "reason": "Confirm the public writing policy.",
        }
        service = self.start(auto_actions=["publish_branch"], merge={"mode": "manual"})
        work = service.work.submit("Restore the value.")
        row = eventually(
            lambda: (
                self.row(service)
                if service.records("changesets")["items"]
                and self.row(service)["state"] == "publication_attention"
                else None
            ),
            25,
        )
        self.assertIn(
            "Confirm the public writing policy", str(service.health()["issues"])
        )
        self.assertTrue(service.health()["waiting_for_operator"])
        question = next(r for r in service.requests() if r.kind == "delivery_information")
        before = copy.deepcopy(row)
        self.peer.publication_value = self.draft
        work.context("Use FROM TEST AGENT with the confirmed changes and tests.")
        eventually(lambda: self.pending_target(service), 25)
        self.assertEqual(self.row(service)["quality"], before["quality"])
        self.assertEqual(self.row(service)["review"], before["review"])
        self.assertNotIn(question.ref.id, [r.ref.id for r in service.requests()])
        with self.assertRaisesRegex(SDKError, "stale"):
            service.decide(question, "answer", text="Outdated clarification.")

    def test_answer_publication_question_redrafts_without_new_authority(self):
        reason = "Use title A or B?"
        answer = "Use the second one"
        self.peer.publication_value = {"human_required": True, "reason": reason}
        service = self.start(auto_actions=["publish_branch"], merge={"mode": "manual"})
        work = service.work.submit("Restore the value.")
        question = eventually(
            lambda: next(
                (r for r in service.requests(work.id) if r.kind == "delivery_information"), None
            ),
            25,
        )
        service.pause("all")
        before = self.row(service)
        work_before = service.store.work(work.id)
        self.assertEqual(before["state"], "publication_attention")
        self.assertEqual(before["error"], "publication_needs_operator")
        self.assertEqual(question.question, reason)
        self.assertEqual(question.arguments, {
            "work_id": work.id, "changeset_id": before["id"], "stage": "publication",
        })
        self.assertIsNone(question.expires_at)
        self.assertTrue(question.ref.durable)
        store = service.runtime.maintenance.store
        self.assertEqual(store.get("decisions", question.ref.id)["basis"], before["publication_basis"])
        with self.assertRaisesRegex(SDKError, "requires an operator answer"):
            service.decide(question, "approve")
        with self.assertRaisesRegex(SDKError, "operator answer must be nonempty"):
            service.decide(question, "answer", text="  ")
        with self.assertRaises(SDKError):
            service.decide(replace(question.ref, revision=question.ref.revision + 1), "answer", text=answer)
        self.assertEqual(service.store.work(work.id), work_before)

        result = service.decide(question, "answer", text=answer, decision_id="clarify-publication")
        self.assertEqual(result["state"], "answered")
        self.assertEqual(result, service.decide(question, "answer", text=answer, decision_id="clarify-publication"))
        saved = store.get("decisions", question.ref.id)
        self.assertEqual(saved["answer"], answer)
        self.assertEqual(saved["revision"], question.ref.revision + 1)
        self.assertNotIn(question.ref.id, [r.ref.id for r in service.requests()])
        after = self.row(service)
        self.assertEqual(after["state"], "review_branch_ready")
        self.assertEqual(after["revision"], before["revision"] + 1)
        for key in ("error", "error_detail", "error_info", "publication_basis"):
            self.assertIsNone(after[key])
        clarification = {
            "stage": "publication",
            "request_id": question.ref.id,
            "basis": before["publication_basis"],
            "question": reason,
            "question_origin": "model",
            "answer": answer,
            "answer_origin": "operator",
        }
        self.assertEqual(after["clarifications"], [clarification])
        # Work context is identical in these two probes; the row's structured
        # provenance alone must also affect the publication basis.
        delivery = service.runtime.maintenance.delivery.store
        with delivery.db.connection() as conn:
            self.assertNotEqual(
                basis(delivery, after, None, conn),
                basis(delivery, {**after, "clarifications": []}, None, conn),
            )
        clarified = service.store.work(work.id)
        note = (
            f"Delivery clarification (publication):\n"
            f"Question (model): {reason}\nAnswer (operator): {answer}"
        )
        self.assertEqual(
            clarified["execution_context"], [*work_before.get("execution_context", []), note]
        )
        self.assertEqual(clarified["revision"], work_before["revision"] + 1)
        for key in ("requirements_revision", "authority", "run_id", "changeset_id"):
            self.assertEqual(clarified[key], work_before[key])

        self.peer.publication_value = self.draft
        service.resume()
        target = eventually(lambda: self.pending_target(service), 25)
        self.assertFalse(self.peer.pulls)
        after = self.row(service)
        for key in ("candidate", "quality", "review", "frozen"):
            self.assertEqual(after[key], before[key])
        self.assertEqual(after["publication"], self.draft)
        self.assertEqual(after["clarifications"], [clarification])
        self.assertNotEqual(after["reasoning_job_id"], before["reasoning_job_id"])
        self.assertNotEqual(after["publication_basis"], before["publication_basis"])
        self.assertEqual(
            after["publication_basis"], service.job(after["reasoning_job_id"])["input"]["basis"]
        )
        payloads = [
            json.loads(message["content"])
            for request in self.peer.model_requests
            for message in request["messages"]
            if message.get("role") == "user" and message.get("content", "").startswith('{"job":')
        ]
        draft = next(p for p in payloads if p["job"]["id"] == after["reasoning_job_id"])
        self.assertEqual(draft["input"]["clarifications"], [clarification])
        self.assertIn(note, draft["input"]["work_context"]["execution_context"])
        self.assertEqual(target.kind, "delivery_authority")

    def test_denied_current_action_exposes_its_exact_review_reason(self):
        self.peer.review_extra = {"publication": self.draft}
        service = self.start(auto_actions=["publish_branch"], merge={"mode": "manual"})
        work = service.work.submit("Restore the value.")
        request = eventually(lambda: self.pending_target(service), 25)
        service.pause("all")
        service.decide(request, "deny", decision_id="deny-publication")
        self.host.store.put(
            "approval_reviews",
            "denial-fixture",
            {
                "id": "denial-fixture",
                "checkpoint": "service:" + service.id,
                "request_id": request.ref.id,
                "state": "denied",
                "verdict": {
                    "decision": "deny",
                    "reason": "Required public prefix is missing.",
                },
            },
        )
        issue = next(
            issue
            for issue in service.health()["issues"]
            if issue["code"] == "delivery_blocked"
        )
        self.assertEqual(issue["message"], "Required public prefix is missing.")
        self.assertEqual(issue["request_id"], request.ref.id)
        self.assertEqual(issue["action_id"], request.arguments["action_id"])
        self.assertTrue(service.health()["waiting_for_operator"])
        self.assertEqual(
            work.progress()["delivery"]["denied_request_id"], request.ref.id
        )

    def test_failed_wording_job_has_native_retry_with_existing_quality(self):
        self.peer.publication_value = {"not_a_draft": True}
        service = self.start(
            auto_actions=["publish_branch"],
            merge={"mode": "manual"},
            maintenance={"job_turns": 1, "job_attempts": 1},
        )
        service.work.submit("Restore the value.")
        row = eventually(
            lambda: (
                self.row(service)
                if service.records("changesets")["items"]
                and self.row(service)["state"] == "publication_failed"
                else None
            ),
            25,
        )
        before = copy.deepcopy(row)
        job = next(
            job
            for job in service.records("jobs")["items"]
            if job["kind"] == "publication"
        )
        self.assertEqual(job["state"], "failed")
        self.peer.publication_value = self.draft
        service.retry_job(job["id"], request_id="retry-wording")
        eventually(lambda: self.pending_target(service), 25)
        self.assertEqual(self.row(service)["quality"], before["quality"])
        self.assertEqual(self.row(service)["review"], before["review"])
        after = service.job(job["id"])
        self.assertGreater(after["attempts"], job["attempts"])
        self.assertEqual(after["retry_generation"], 1)
        self.assertTrue(after["retry_history"][0]["error"])
        self.assertTrue(
            any(
                m.get("name") == "retry_attempt"
                for r in self.peer.model_requests
                for m in r["messages"]
            )
        )

    def test_started_or_unknown_external_effect_blocks_replacement(self):
        from homebrew_harness.services.delivery.publication import accept

        self.held()
        service = self.start(auto_actions=["publish_branch"], merge={"mode": "manual"})
        service.work.submit("Restore the value.")
        self.assertTrue(self.peer.publication_started.wait(20))
        store = service.runtime.maintenance.delivery.store
        row = self.row(service)
        job = next(
            job
            for job in service.records("jobs")["items"]
            if job["kind"] == "publication"
        )
        with store.db.write() as conn:
            action = store.action(conn, row, "ensure_change_request")
            for state in ("started", "unknown"):
                action["state"] = state
                store.put(conn, "actions", action)
                before = copy.deepcopy(row)
                with self.assertRaisesRegex(ValueError, "accepted external effect"):
                    accept(store, row, job, self.draft, conn)
                self.assertEqual(row, before)
            action["state"] = "superseded"
            store.put(conn, "actions", action)

    def test_current_instruction_change_rejects_old_draft_and_reevaluates(self):
        from unittest.mock import patch
        from examples import delivery_fixture

        self.held()
        original = delivery_fixture.completion
        counter = 0

        def completion(*args, **kwargs):
            nonlocal counter
            value = original(*args, **kwargs)
            counter += 1
            for call in value["choices"][0]["message"].get("tool_calls", []):
                call["id"] += str(counter)
            return value

        with patch.object(delivery_fixture, "completion", completion):
            service = self.start(
                auto_actions=["publish_branch"], merge={"mode": "manual"}
            )
            work = service.work.submit("Restore the value.")
            self.assertTrue(self.peer.publication_started.wait(20))
            before = copy.deepcopy(self.row(service))
            work.context("Describe the parser change in the public title.")
            self.peer.publication_value = {
                **self.draft,
                "title": "Restore parser value handling",
            }
            self.peer.publication_hold.set()
            eventually(lambda: self.pending_target(service), 25)
        self.assertEqual(
            self.row(service)["publication"]["title"], "Restore parser value handling"
        )
        self.assertEqual(self.row(service)["quality"], before["quality"])
        self.assertEqual(self.row(service)["review"], before["review"])
        observed = [
            m.get("content", "")
            for r in self.peer.model_requests
            for m in r["messages"]
        ]
        self.assertTrue(
            any("changed during reasoning" in str(message) for message in observed)
        )
