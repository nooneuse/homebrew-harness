"""Service operations preserve native ownership and keep HTTP controls observable."""

import threading
import unittest
from unittest.mock import patch

from homebrew_harness import RemoteService
from homebrew_harness.contracts import SDKError
from homebrew_harness.services.maintenance import Maintenance
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

    def test_stop_command_wins_during_unpark_worker_launch(self):
        self._assert_stop_wins_during_unpark("stop")

    def test_owner_close_wins_during_unpark_worker_launch(self):
        self._assert_stop_wins_during_unpark("close")

    def _assert_stop_wins_during_unpark(self, stop_method):
        host, model = self.host([])
        with (self.base / "model.env").open("a") as env:
            env.write(f"WEBHOOK_URL={model.url}/digest\n")
        service = host.services.start(
            self.spec(
                profile="repository_maintenance",
                notifications={"mode": "local", "url_env": "WEBHOOK_URL"},
            )
        )
        self.addCleanup(lambda: (service.close(), service.wait_closed(8)))
        self.wait_operation(service, service.quiesce())
        runtime = service.runtime
        previous = runtime.maintenance
        parked_revision = service.snapshot().revision
        launched = threading.Event()
        release = threading.Event()
        ran = threading.Event()
        threads = []
        start = runtime.workers.start
        loop = Maintenance.loop

        def delay_start(target, *args, **options):
            thread = start(target, *args, **options)
            if options.get("name") == "service-maintenance":
                threads.append(thread)
                launched.set()
                if not release.wait(10):
                    raise AssertionError("Unpark launch was not released.")
            return thread

        def record_loop(maintenance):
            ran.set()
            loop(maintenance)

        with (
            patch.object(runtime.workers, "start", side_effect=delay_start),
            patch.object(Maintenance, "loop", record_loop),
            patch.object(runtime.notifications, "_attempt") as attempt,
        ):
            row = service.resume_operations()
            try:
                self.assertTrue(launched.wait(8))
                getattr(service, stop_method)()
                stopping = service.store.get(service.id)
                self.assertEqual(stopping["state"], "stopping")
                self.assertEqual(stopping["operations_phase"], "parked")
                self.assertEqual(stopping["revision"], parked_revision + 1)
                with self.assertRaises(SDKError):
                    service.send_notification(force=True, request_id="while-stopping")
                self.assertIsNone(service.receipt("while-stopping"))
            finally:
                release.set()
            self.assertTrue(service.wait_closed(15), service.snapshot().data())
            attempt.assert_not_called()

        stopped = service.store.get(service.id)
        self.assertEqual(stopped["state"], "stopped")
        self.assertEqual(stopped["operations_phase"], "parked")
        self.assertEqual(stopped["revision"], stopping["revision"])
        outcome = runtime.operations.get(row["id"])
        self.assertEqual(outcome["state"], "blocked")
        self.assertIn("stopping", outcome["error_detail"])
        self.assertTrue(runtime.notifications.parked.is_set())
        self.assertIsNot(runtime.maintenance, previous)
        self.assertTrue(runtime.maintenance.agent_closed)
        self.assertFalse(ran.is_set())
        self.assertEqual(len(threads), 1)
        threads[0].join(2)
        self.assertFalse(threads[0].is_alive())

    def test_operation_requires_a_real_park_before_acceptance(self):
        host, _ = self.host([])
        service = host.services.start(self.spec())
        self.addCleanup(lambda: (service.close(), service.wait_closed(8)))
        with self.assertRaises(SDKError):
            service.backup()
        self.assertFalse(service.operations()["items"])
