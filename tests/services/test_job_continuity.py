"""Job input and collection continue while a reasoning request waits."""

import json
import threading
import unittest
from unittest.mock import patch
from tests.services import test_maintenance as fixture
from tests.runtime.test_interaction import eventually
from homebrew_harness.services.agent import ServiceAgent


class JobContinuity(unittest.TestCase):
    setUp = fixture.MaintenanceTests.setUp
    start = fixture.MaintenanceTests.start

    def test_long_job_does_not_stop_inbox_and_exposes_steering(self):
        entered = threading.Event()
        release = threading.Event()
        original = ServiceAgent.run

        def run(agent, job):
            if job["kind"] == "conversation":
                entered.set()
                release.wait(10)
            return original(agent, job)

        self.addCleanup(release.set)
        with patch.object(ServiceAgent, "run", run):
            service = self.start(sources=True)
            accepted = service.converse("Review the plan.")
            self.assertTrue(entered.wait(10))
            before = len(self.peer.source_requests)
            eventually(lambda: len(self.peer.source_requests) > before + 2, 5)
            job = service.job(accepted["job_id"])
            self.assertIsNotNone(job["execution_id"])
            service.control_job(
                job["id"],
                "input",
                text="Preserve the new operator constraint.",
                request_id="input-one",
            )
            release.set()
            eventually(lambda: service.job(job["id"])["state"] == "completed", 15)
            self.assertTrue(
                any(
                    "Preserve the new operator constraint" in json.dumps(r)
                    for r in self.peer.model_requests
                )
            )

    def test_read_record_pages_have_version_and_reject_replaced_snapshot(self):
        service = self.start(auto=False)
        service.pause("all")
        work = service.work.submit("Current version.")
        first = service.record("work", work.id, offset=0, limit=40)
        self.assertIn("version", first)
        work.context("A later operator instruction.")
        with self.assertRaises(Exception):
            service.record(
                "work",
                work.id,
                offset=first["next_offset"],
                expected_version=first["version"],
            )
