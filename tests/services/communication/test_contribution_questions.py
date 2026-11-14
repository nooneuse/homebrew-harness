"""Contributor invitations are public information, with independent work authority."""

import unittest

from homebrew_harness.services.communication.reasoning import public, reply
from homebrew_harness.services.communication.spec import validate_proposal
from tests.services.communication import test_reasoning as fixture


class ContributionQuestions(unittest.TestCase):
    setUp = fixture.DiscussionReasoning.setUp
    start = fixture.DiscussionReasoning.start
    models = fixture.DiscussionReasoning.models
    active = fixture.DiscussionReasoning.active

    def proposal(self, step="develop"):
        return {
            "next_step": step,
            "reason": "The feature is clear; optional collaboration can be arranged separately.",
            "alignment": "Fits the project goals.",
            "questions": [{
                "id": "offer-pr", "text": "Would you like to contribute a PR?",
                "recipient": "requester", "status": "open", "sources": [],
            }],
            "draft": {"text": "Add the bounded requested behavior.", "scope": "code",
                      "acceptance": "The registered checks confirm the requested behavior.",
                      "out_of_scope": "No unrelated API or infrastructure redesign."},
        }

    def communication(self, ids=None):
        return {"communication": {
            "kind": "post_comment", "intent": "followup",
            "body": "Would you like to contribute a PR? No pressure if you lack time; work can be arranged through the maintenance process.",
            "question_ids": ["offer-pr"] if ids is None else ids,
        }}

    def setup_round(self, step="develop"):
        self.models()
        service = self.start(model_profile="reply")
        service.pause("all")
        store, job = self.active(service, body="Please add the feature described here. Its desired behavior is fully specified.")
        proposal = self.proposal(step)
        proposal["questions"][0]["sources"] = job["input"]["events"]
        self.peer.proposals = [proposal, self.communication()]
        return service, store, job

    def test_open_invitation_can_accompany_ready_requirement(self):
        proposal = self.proposal()
        validate_proposal(proposal, {"scopes": {"code": {}}})
        self.assertEqual(public({"proposal": proposal})["public_questions"], proposal["questions"])
        self.assertEqual(proposal["next_step"], "develop")

    def test_two_models_invite_unvolunteered_author_without_granting_work(self):
        service, store, job = self.setup_round()
        result = service.runtime.maintenance.agent.run(job)
        self.assertEqual([r["model"] for r in self.peer.models], ["planner", "writer"])
        self.assertEqual(result["next_step"], "develop")
        self.assertEqual(result["questions"][0]["recipient"], "requester")
        self.assertIn("offer-pr", str(self.peer.models[1]["messages"]))
        store.ms.finish(job, result)
        kinds = {r.kind for r in service.requests()}
        self.assertEqual(kinds, {"discussion_requirements", "communication_authority"})
        self.assertEqual(service.work.list()["items"], [])
        self.assertEqual(self.peer.writes, [])

    def test_resource_decision_stays_internal_beside_public_invitation(self):
        service, store, job = self.setup_round("operator")
        proposal = self.peer.proposals[0]
        proposal["questions"].append({
            "id": "ci-access", "text": "Can the operator provision the required runner access?",
            "recipient": "maintainer", "status": "open", "sources": job["input"]["events"],
        })
        result = service.runtime.maintenance.agent.run(job)
        self.assertEqual(result["communication"]["question_ids"], ["offer-pr"])
        saved = {"proposal": proposal}
        self.assertEqual([q["id"] for q in public(saved)["public_questions"]], ["offer-pr"])
        invalid = self.communication(["ci-access"])
        with self.assertRaisesRegex(ValueError, "open requester"):
            reply(saved, invalid, store.ms, job)
        store.ms.finish(job, result)
        self.assertIn("discussion_information", {r.kind for r in service.requests()})
        self.assertEqual(self.peer.writes, [])

    def test_drafter_cannot_invent_contribution_question(self):
        service, store, job = self.setup_round()
        plan = {"proposal": {**self.proposal(), "questions": []}}
        with self.assertRaisesRegex(ValueError, "open requester"):
            reply(plan, self.communication(), store.ms, job)

    def followup(self, willing):
        service, store, job = self.setup_round("discuss")
        agent = service.runtime.maintenance.agent
        store.ms.finish(job, agent.run(job))
        body = "I can contribute a PR and expect it next week." if willing else "I do not have time to contribute a PR."
        store, later = self.active(service, body=body, identity=2, kind="comment")
        proposal = self.proposal("wait" if willing else "develop")
        proposal["questions"][0].update(status="resolved", sources=later["input"]["events"])
        proposal["facts"] = [{"id": "contribution-intent", "text": body,
                                "sources": later["input"]["events"]}]
        self.peer.proposals = [proposal, self.communication([])]
        result = agent.run(later)
        store.ms.finish(later, result)
        self.assertEqual(result["questions"][0]["status"], "resolved")
        self.assertEqual(result["facts"][0]["text"], body)
        self.assertEqual(service.work.list()["items"], [])
        self.assertEqual(self.peer.writes, [])
        return service, result

    def test_declined_invitation_does_not_decline_requirement(self):
        service, result = self.followup(False)
        self.assertEqual(result["next_step"], "develop")
        self.assertIn("discussion_requirements", {r.kind for r in service.requests()})

    def test_volunteering_is_a_fact_not_work_authority(self):
        service, result = self.followup(True)
        self.assertEqual(result["next_step"], "wait")
        self.assertNotIn("discussion_requirements", {r.kind for r in service.requests()})
