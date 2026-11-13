"""Early proposal feedback and durable host-application failures."""

import copy
import json
import unittest
from unittest.mock import patch

from homebrew_harness.services.job_contracts import JobContract
from tests.runtime.test_interaction import eventually
from tests.services.communication import test_boundaries as fixture


class ProposalValidation(unittest.TestCase):
    setUp = fixture.CommunicationBoundaries.setUp
    start = fixture.CommunicationBoundaries.start
    discussion = fixture.CommunicationBoundaries.discussion
    managed = fixture.CommunicationBoundaries.managed

    def active_job(self):
        service, did, communication = self.managed()
        store = communication.store
        with store.db.write() as conn:
            row = store.get("discussions", did, conn)
            row.update(state="ready_to_assess", analysis_epoch=row["analysis_epoch"] + 1)
            store.put(conn, "discussions", row)
        job = store.queue_round(row, force=True)
        ms = store.ms
        with ms.db.write() as conn:
            job.update(state="running", attempts=1, generation=ms.header["generation"])
            ms.put(conn, "jobs", job)
        agent = service.runtime.maintenance.agent
        agent.job, agent.proposal = job, None
        return service, store, job, agent

    def test_invalid_sources_and_issue_findings_are_correctable_before_finish(self):
        service, store, job, agent = self.active_job()
        before = service.discussion(job["target"])
        candidate = {"next_step": "wait", "reason": "Await clarification.", "alignment": "Within scope."}
        invalid = copy.deepcopy(candidate)
        invalid["facts"] = [{"id": "platform", "text": "Platform observation.", "sources": ["src/runtime.py"]}]
        with self.assertRaisesRegex(ValueError, "unknown source"):
            agent.propose(json.dumps(invalid))
        self.assertIsNone(agent.proposal)
        invalid = copy.deepcopy(candidate)
        invalid["findings"] = [{"id": "platform", "text": "Platform observation."}]
        with self.assertRaisesRegex(ValueError, "fixed review snapshot"):
            agent.propose(json.dumps(invalid))
        self.assertIsNone(agent.proposal)
        event = store.history(before)[0]["id"]
        candidate["facts"] = [{"id": "platform", "text": "Observation in src/runtime.py.", "sources": [event]}]
        receipt = json.loads(agent.propose(json.dumps(candidate)))
        self.assertFalse(receipt["persisted"])
        self.assertFalse(receipt["authorized"])
        self.assertEqual(service.discussion(job["target"]), before)
        self.assertEqual(store.ms.finish(job, agent.proposal)["state"], "completed")
        self.assertEqual(self.peer.writes, [])

    def test_finish_rechecks_evidence_and_rolls_back_without_publication(self):
        service, store, job, agent = self.active_job()
        event = store.history(service.discussion(job["target"]))[0]["id"]
        candidate = {"next_step": "wait", "reason": "Read source.", "alignment": "Within scope.", "facts": [{"id": "fact", "text": "Observed fact.", "sources": [event]}]}
        agent.propose(json.dumps(candidate))
        with store.db.write() as conn:
            row = store.ms.get("events", event, conn)
            row["echo"] = "test-action"
            store.ms.put(conn, "events", row)
        before = service.discussion(job["target"])
        with self.assertRaisesRegex(ValueError, "unknown source"):
            store.ms.finish(job, candidate)
        self.assertEqual(service.discussion(job["target"]), before)
        self.assertEqual(store.ms.job_get(job["id"])["state"], "running")
        self.assertEqual(self.peer.writes, [])

    def test_worker_preserves_redacted_unapplied_result_and_failure_stage(self):
        from homebrew_harness.security.data_boundary import register_private_values

        private = "synthetic-private-diagnostic-value"
        register_private_values(private)
        candidate = {"next_step": "wait", "reason": "Await clarification.", "alignment": "Within scope."}
        self.peer.issue()
        self.peer.proposals = [candidate]
        with patch.object(JobContract, "apply", side_effect=ValueError("Host validation: " + private)):
            service = self.start()
            job = eventually(lambda: next((j for j in service.records("jobs")["items"] if j.get("error_stage") == "apply"), None), 10)
            record = service.job(job["id"])
        self.assertEqual(record["unapplied_result"], candidate)
        self.assertEqual(record["error"], "ValueError")
        self.assertEqual(record["error_detail"], "Host validation: [redacted]")
        self.assertTrue(job["unapplied_result_available"])
        self.assertNotIn("unapplied_result", job)
        self.assertEqual(self.peer.writes, [])
