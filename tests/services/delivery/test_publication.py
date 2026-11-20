"""Public wording recovery preserves reviewed code and external action history."""

import copy
import json
import threading
import unittest

from tests.runtime.test_interaction import eventually
from tests.services.delivery import test_flow as fixture


class Publication(unittest.TestCase):
    setUp = fixture.FlowTests.setUp
    start = fixture.FlowTests.start
    run_git = fixture.FlowTests.run_git
    pending_target = fixture.FlowTests.pending_target

    draft = {"title": "Restore value handling", "body": "FROM TEST AGENT\n\nRestore the value. Fixed unit-tests passed."}

    def held(self):
        self.peer.publication_started = threading.Event()
        self.peer.publication_hold = threading.Event()
        self.peer.publication_value = self.draft
        self.addCleanup(self.peer.publication_hold.set)

    def row(self, service):
        return service.runtime.maintenance.delivery.rows("changesets")[-1]

    def test_missing_wording_is_drafted_without_repeating_code_review_or_tests(self):
        self.held()
        guidance = "Public bodies must begin FROM TEST AGENT and describe confirmed changes."
        service = self.start(auto_actions=["publish_branch"], maintenance={"instructions": {"review": guidance}},
                             merge={"mode": "manual"})
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
        payloads = [json.loads(m["content"]) for r in self.peer.model_requests for m in r["messages"]
                    if m.get("role") == "user" and m.get("content", "").startswith('{"job":')]
        prepared = next(p for p in payloads if p["job"]["kind"] == "publication")
        self.assertEqual(prepared["policy"]["instructions"], {"review": guidance})
        service.decide(request, "approve", decision_id="approve-generated-wording")
        eventually(lambda: len(self.peer.pulls) == 1, 15)
        actions = service.records("actions")["items"]
        branches = [action for action in actions if action["kind"] == "publish_branch"]
        self.assertEqual(len(branches), 1)
        self.assertEqual(branches[0]["attempts"], 1)
        self.assertEqual(self.peer.pulls[0]["body"].split("\n\n<!--", 1)[0], self.draft["body"])
        self.assertEqual(service.store.work(work.id)["run_id"], before["run_id"])

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
        self.assertEqual(sum(action["attempts"] for action in service.records("actions")["items"]
                             if action["kind"] == "publish_branch"), 1)

    def test_operator_referral_waits_for_new_current_instructions(self):
        self.peer.publication_value = {"human_required": True, "reason": "Confirm the public writing policy."}
        service = self.start(auto_actions=["publish_branch"], merge={"mode": "manual"})
        work = service.work.submit("Restore the value.")
        row = eventually(lambda: self.row(service) if service.records("changesets")["items"]
                         and self.row(service)["state"] == "publication_attention" else None, 25)
        self.assertIn("Confirm the public writing policy", str(service.health()["issues"]))
        self.assertTrue(service.health()["waiting_for_operator"])
        before = copy.deepcopy(row)
        self.peer.publication_value = self.draft
        work.context("Use FROM TEST AGENT with the confirmed changes and tests.")
        eventually(lambda: self.pending_target(service), 25)
        self.assertEqual(self.row(service)["quality"], before["quality"])
        self.assertEqual(self.row(service)["review"], before["review"])

    def test_denied_current_action_exposes_its_exact_review_reason(self):
        self.peer.review_extra = {"publication": self.draft}
        service = self.start(auto_actions=["publish_branch"], merge={"mode": "manual"})
        work = service.work.submit("Restore the value.")
        request = eventually(lambda: self.pending_target(service), 25)
        service.pause("all")
        service.decide(request, "deny", decision_id="deny-publication")
        self.host.store.put("approval_reviews", "denial-fixture", {
            "id": "denial-fixture", "checkpoint": "service:" + service.id,
            "request_id": request.ref.id, "state": "denied",
            "verdict": {"decision": "deny", "reason": "Required public prefix is missing."}})
        issue = next(issue for issue in service.health()["issues"] if issue["code"] == "delivery_blocked")
        self.assertEqual(issue["message"], "Required public prefix is missing.")
        self.assertEqual(issue["request_id"], request.ref.id)
        self.assertEqual(issue["action_id"], request.arguments["action_id"])
        self.assertTrue(service.health()["waiting_for_operator"])
        self.assertEqual(work.progress()["delivery"]["denied_request_id"], request.ref.id)

    def test_failed_wording_job_has_native_retry_with_existing_quality(self):
        self.peer.publication_value = {"not_a_draft": True}
        service = self.start(auto_actions=["publish_branch"], merge={"mode": "manual"},
                             maintenance={"job_turns": 1, "job_attempts": 1})
        service.work.submit("Restore the value.")
        row = eventually(lambda: self.row(service) if service.records("changesets")["items"]
                         and self.row(service)["state"] == "publication_failed" else None, 25)
        before = copy.deepcopy(row)
        job = next(job for job in service.records("jobs")["items"] if job["kind"] == "publication")
        self.assertEqual(job["state"], "failed")
        self.peer.publication_value = self.draft
        service.retry_job(job["id"], request_id="retry-wording")
        eventually(lambda: self.pending_target(service), 25)
        self.assertEqual(self.row(service)["quality"], before["quality"])
        self.assertEqual(self.row(service)["review"], before["review"])

    def test_started_or_unknown_external_effect_blocks_replacement(self):
        from homebrew_harness.services.delivery.publication import accept

        self.held()
        service = self.start(auto_actions=["publish_branch"], merge={"mode": "manual"})
        service.work.submit("Restore the value.")
        self.assertTrue(self.peer.publication_started.wait(20))
        store = service.runtime.maintenance.delivery.store
        row = self.row(service)
        job = next(job for job in service.records("jobs")["items"] if job["kind"] == "publication")
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
            service = self.start(auto_actions=["publish_branch"], merge={"mode": "manual"})
            work = service.work.submit("Restore the value.")
            self.assertTrue(self.peer.publication_started.wait(20))
            before = copy.deepcopy(self.row(service))
            work.context("Describe the parser change in the public title.")
            self.peer.publication_value = {**self.draft, "title": "Restore parser value handling"}
            self.peer.publication_hold.set()
            eventually(lambda: self.pending_target(service), 25)
        self.assertEqual(self.row(service)["publication"]["title"], "Restore parser value handling")
        self.assertEqual(self.row(service)["quality"], before["quality"])
        self.assertEqual(self.row(service)["review"], before["review"])
        observed = [m.get("content", "") for r in self.peer.model_requests for m in r["messages"]]
        self.assertTrue(any("changed during reasoning" in str(message) for message in observed))
