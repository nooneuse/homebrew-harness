from tests.support.services import ready_service
import os
import signal
import unittest
from unittest.mock import patch

from tests.services import test_maintenance as fixture
from tests.runtime.test_interaction import eventually
from homebrew_harness.services.launcher import live


class MaintenanceRecovery(unittest.TestCase):
    setUp = fixture.MaintenanceTests.setUp
    start = fixture.MaintenanceTests.start

    def test_pending_work_authority_survives_process_restart(self):
        self.peer.triage["disposition"] = "decide"
        service = self.start(auto=False, lifetime="detached")
        work = service.work.submit("Plan the requested feature.", request_id="feature")
        request = eventually(lambda: service.requests() and service.requests()[0], 15)
        os.kill(service.store.get(service.id)["pid"], signal.SIGKILL)
        eventually(lambda: not live(self.control), 8)
        restored = ready_service(self.host.services.start(identity=service.id, lifetime="detached"))
        current = eventually(lambda: restored.requests() and restored.requests()[0], 15)
        self.assertEqual(current.ref, request.ref)
        restored.decide(current, "approve", decision_id="continue")
        eventually(lambda: restored.work.get(work.id).snapshot().state == "review", 20)
        self.assertEqual(len(restored.work.list()["items"]), 1)

    def test_claimed_reasoning_job_recovers_without_reaccepting_message(self):
        import threading

        self.peer.hold = threading.Event()
        service = self.start(lifetime="detached")
        receipt = service.converse(
            "What are the next priorities?", request_id="conversation"
        )
        eventually(lambda: service.job(receipt["job_id"])["state"] == "running", 10)
        os.kill(service.store.get(service.id)["pid"], signal.SIGKILL)
        eventually(lambda: not live(self.control), 8)
        self.peer.hold.set()
        restored = ready_service(self.host.services.start(identity=service.id, lifetime="detached"))
        eventually(lambda: restored.conversation(receipt["id"]).get("result"), 15)
        self.assertEqual(
            restored.converse(
                "What are the next priorities?", request_id="conversation"
            ),
            receipt,
        )
        self.assertEqual(restored.job(receipt["job_id"])["attempts"], 2)

    def test_stale_triage_is_discarded_before_another_model_call(self):
        service = self.start(auto=False)
        service.pause("all")
        work = service.work.submit("Original work.")
        work.revise("Preserve compatibility.")
        service.resume()
        request = eventually(lambda: service.requests() and service.requests()[0], 15)
        jobs = service.records("jobs")["items"]
        self.assertIn("stale", [job["state"] for job in jobs])
        self.assertEqual(len(self.peer.model_requests), 2)
        self.assertEqual(request.arguments["work_id"], work.id)

    def test_finite_model_failure_is_visible_without_creating_a_run(self):
        service = self.start(job_attempts=1)
        with patch.object(
            service.runtime.maintenance.agent,
            "run",
            side_effect=ValueError("Invalid model candidate"),
        ):
            work = service.work.submit("Investigate the request.")
            job = eventually(
                lambda: next(
                    (
                        j
                        for j in service.records("jobs")["items"]
                        if j["state"] == "failed"
                    ),
                    None,
                ),
                10,
            )
        self.assertEqual(job["attempts"], 1)
        self.assertIsNone(service.store.work(work.id)["run_id"])

    def test_report_window_is_bounded_and_does_not_skip_more_activity(self):
        service = self.start()
        service.pause("all")
        store = service.runtime.maintenance.store
        with store.db.write() as conn:
            for n in range(125):
                store.changed(conn, detail={"fact": n})
            report = store.report(conn, force=True)
        record = store.get("reports", report["id"])
        self.assertEqual(len(record["facts"]), 100)
        self.assertTrue(record["more_activity"])
        job = store.claim_job()
        store.finish(job, {"text": "First report."})
        with store.db.write() as conn:
            next_report = store.report(conn, force=True)
        remainder = store.get("reports", next_report["id"])
        self.assertEqual(len(remainder["facts"]), 25)
        self.assertEqual(remainder["from_activity"], 100)

    def test_model_pause_revoke_is_not_reversed_by_old_work_resume(self):
        service = self.start(auto=False)
        work = service.work.submit("Plan first.")
        request = eventually(lambda: service.requests() and service.requests()[0], 15)
        service.pause("execution")
        service.decide(request, "approve", decision_id="grant")
        work.revoke()
        service.resume()
        eventually(lambda: work.snapshot().state == "proposed", 10)
        self.assertIsNone(service.store.work(work.id)["run_id"])
