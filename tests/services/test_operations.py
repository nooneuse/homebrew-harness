"""Service operations preserve native ownership and keep HTTP controls observable."""

import unittest

from homebrew_harness import RemoteService
from homebrew_harness.contracts import SDKError
from tests.services import test_runtime as fixture
from tests.runtime.test_interaction import eventually
from tests.support.clients import reply


class OperationTests(unittest.TestCase):
    setUp = fixture.ServiceRuntimeTests.setUp
    host = fixture.ServiceRuntimeTests.host
    spec = fixture.ServiceRuntimeTests.spec

    def wait_operation(self, service, row):
        def done():
            current = service.operation(row["id"])
            if current["state"] == "blocked":
                raise AssertionError(current)
            return current if current["state"] == "completed" else None

        return eventually(done, 15)

    def test_idle_health_makes_no_model_calls_and_park_keeps_http(self):
        host, model = self.host([])
        service = host.services.start(self.spec())
        self.addCleanup(lambda: (service.close(), service.wait_closed(8)))
        self.assertTrue(service.health()["ready"])
        first = service.quiesce(request_id="park")
        self.wait_operation(service, first)
        self.assertEqual(service.quiesce(request_id="park")["id"], first["id"])
        with RemoteService.from_root(self.control, control=True) as remote:
            self.assertEqual(remote.health()["operations_phase"], "parked")
            self.assertTrue(remote.health()["live"])
            self.assertFalse(remote.health()["ready"])
            with self.assertRaises(SDKError):
                remote.work.submit("Do not run while parked")
            self.wait_operation(remote, remote.resume_operations())
            self.assertTrue(remote.health()["ready"])
        self.assertEqual(model.requests, [])

    def test_park_waits_for_native_pause_and_resumes_same_run(self):
        host, _ = self.host([reply("Done")])
        service = host.services.start(self.spec())
        self.addCleanup(lambda: (service.close(), service.wait_closed(8)))
        work = service.work.submit("Complete a bounded task")
        eventually(lambda: work.snapshot().details["run_id"], 10)
        identity = work.snapshot().details["run_id"]
        self.wait_operation(service, service.quiesce())
        self.assertFalse(work.snapshot().execution_pending)
        self.wait_operation(service, service.resume_operations())
        eventually(lambda: work.snapshot().state == "review", 15)
        self.assertEqual(work.snapshot().details["run_id"], identity)

    def test_operation_requires_a_real_park_before_acceptance(self):
        host, _ = self.host([])
        service = host.services.start(self.spec())
        self.addCleanup(lambda: (service.close(), service.wait_closed(8)))
        with self.assertRaises(SDKError):
            service.backup()
        self.assertFalse(service.operations()["items"])
