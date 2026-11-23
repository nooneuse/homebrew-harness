"""Discussion repair admission drives a native Run and updates the original PR."""

import unittest

from homebrew_harness.platforms.contracts import ObjectRef, SourceEvent
from homebrew_harness.services.communication.store import CommunicationStore
from homebrew_harness.services.communication.spec import validate
from tests.runtime.test_interaction import eventually
from tests.services.delivery import test_flow, test_merge_flow


class PRFeedbackDelivery(unittest.TestCase):
    setUp = test_flow.FlowTests.setUp
    run_git = test_flow.FlowTests.run_git
    start = test_flow.FlowTests.start
    ready = test_merge_flow.MergeFlowTests.ready

    def test_feedback_repairs_same_work_and_pr_then_observes_actual_merge(self):
        service = self.start(auto_ci_fix=True, merge={"mode": "manual", "poll_seconds": .1})
        work = service.work.submit("Implement the bounded value and preserve its tests.")
        original = eventually(lambda: self.ready(service, work), 180)
        service.pause("all")
        before = service.store.work(work.id)
        ms = service.runtime.maintenance.store
        source = {"name": "project", "provider": "github", "instance": "primary",
                  "project": "example/project", "api_url_env": "API", "token_env": "TOKEN"}
        policy = validate({"mode": "local", "projects": [source]}, [source])
        communication = CommunicationStore(service.runtime, ms, policy)
        ms.communication = communication
        action = next(a for a in service.runtime.maintenance.delivery.rows("actions")
                      if a["kind"] == "ensure_change_request" and a["state"] == "confirmed")
        with ms.db.write() as conn:
            communication.link_delivery(conn, action)
        discussion = communication.page("discussions")["items"][0]
        observed = service.runtime.maintenance.delivery.adapter.metadata(original["change_request"])
        with ms.db.write() as conn:
            row = communication.get("discussions", discussion["id"], conn)
            row["observed_basis"] = observed
            communication.put(conn, "discussions", row)
        subject = ObjectRef(**original["change_request"])
        event = SourceEvent(ObjectRef("github", "primary", "example/project", "comment", "117"),
                            subject, "review-117", {"body": "Set the documented value to 2 and retain its tests.", "actor_id": 17})
        communication.ingest("project", [event], subject.scope())
        communication.queue_round(communication.get("discussions", row["id"]), force=True)
        job = ms.claim_job()
        self.assertEqual(job["kind"], "discussion")
        ms.finish(job, {"next_step": "repair", "reason": "Address the review within the original value contract.",
                       "alignment": "Retain the original scope and tests.", "repair": {
                           "plan": "Set src/value.txt to 2 and preserve the fixed tests.",
                           "sources": [communication.event_identity(event)]}})
        self.assertEqual(service.store.work(work.id)["requirements_revision"], before["requirements_revision"])
        service.resume()

        def repaired():
            current = self.ready(service, work)
            return current if current and current["id"] != original["id"] else None

        current = eventually(repaired, 180)
        saved = service.store.work(work.id)
        self.assertNotEqual(saved["run_id"], before["run_id"])
        self.assertEqual(saved["requirements_revision"], before["requirements_revision"])
        self.assertEqual(saved["authority"], before["authority"])
        self.assertEqual(current["branch"], original["branch"])
        self.assertEqual(current["change_request"], original["change_request"])
        self.assertEqual(len(self.peer.pulls), 1)
        self.assertEqual(self.run_git("--git-dir=" + str(self.remote), "show",
                                     current["branch"] + ":src/value.txt"), b"2\n")
        followup = communication.get("discussions", row["id"])
        self.assertEqual(followup["repair"]["state"], "published")
        self.assertEqual(followup["repair"]["published_head"], current["candidate"]["commit"])
        self.assertEqual(followup["state"], "ready_to_assess")
        self.assertEqual(work.snapshot().state, "review")
        self.peer.merge_pull(1)
        eventually(lambda: work.snapshot().state == "delivered", 160)
