import os
import signal
import unittest

from tests.services import test_runtime as fixture
from tests.runtime.test_interaction import eventually
from tests.support.clients import reply, call
from homebrew_harness import Harness
from homebrew_harness.services.launcher import live


class ServiceProcesses(unittest.TestCase):
    setUp = fixture.ServiceRuntimeTests.setUp
    host = fixture.ServiceRuntimeTests.host
    spec = fixture.ServiceRuntimeTests.spec

    def stop(self, host, identity):
        try:
            handle = host.services.get(identity, control=True)
            if live(host.workdir):
                handle.stop()
            eventually(lambda: not live(host.workdir), 10)
        except Exception:
            pass

    def test_detached_service_survives_client_exit_and_accepts_reconnected_work(self):
        host, model = self.host([reply("Done")])
        service = host.services.start(self.spec(), lifetime="detached")
        self.addCleanup(self.stop, host, service.id)
        self.assertTrue(live(self.control))
        host.close()
        with Harness(workdir=self.control) as client:
            handle = client.services.get(service.id, control=True)
            work = handle.work.submit("Work", request_id="one")
            self.assertEqual(work.wait(20).snapshot.state, "review")
            self.assertEqual(work.result().text, "Done")
            self.assertEqual(len(model.requests), 1)
            handle.stop()
            eventually(lambda: not live(self.control), 10)
            self.assertEqual(handle.snapshot().state, "stopped")

    def test_supervisor_crash_restores_same_pending_run_and_attention(self):
        host, model = self.host(
            [
                reply(
                    calls=[
                        call(
                            "ask",
                            name="request_attention",
                            arguments='{"question":"Resume?","reason":"Wait","impact":"Result"}',
                        )
                    ]
                ),
                reply("Recovered"),
            ]
        )
        service = host.services.start(self.spec(), lifetime="detached")
        self.addCleanup(self.stop, host, service.id)
        work = service.work.submit("Ask then complete", request_id="one")
        request = eventually(lambda: service.requests() and service.requests()[0], 20)
        before = service.store.work(work.id)
        pid = service.store.get(service.id)["pid"]
        os.kill(pid, signal.SIGKILL)
        eventually(lambda: not live(self.control), 8)
        recovered = host.services.start(identity=service.id, lifetime="detached")
        self.assertEqual(recovered.id, service.id)
        restored = eventually(
            lambda: recovered.requests() and recovered.requests()[0], 20
        )
        self.assertEqual(restored.ref.id, request.ref.id)
        host.requests.answer(restored.ref, text="Continue", decision_id="one")
        eventually(lambda: recovered.work.get(work.id).snapshot().state == "review", 20)
        self.assertEqual(recovered.store.work(work.id)["run_id"], before["run_id"])
        self.assertEqual(len(model.requests), 2)
        recovered.stop()
        eventually(lambda: not live(self.control), 10)

    def test_sigterm_performs_native_resource_shutdown(self):
        host, _ = self.host([])
        service = host.services.start(self.spec(), lifetime="detached")
        self.addCleanup(self.stop, host, service.id)
        pid = service.store.get(service.id)["pid"]
        os.kill(pid, signal.SIGTERM)
        eventually(lambda: not live(self.control), 10)
        self.assertTrue(service.snapshot().cleanup["resources_closed"])

    def test_graceful_stop_and_restart_continue_the_same_service_paused_run(self):
        host, model = self.host(
            [
                reply(
                    calls=[
                        call(
                            "ask",
                            name="request_attention",
                            arguments='{"question":"Continue?","reason":"Wait","impact":"Result"}',
                        )
                    ]
                ),
                reply(
                    calls=[
                        call(
                            "ask-again",
                            name="request_attention",
                            arguments='{"question":"Continue after pause?","reason":"Refresh the request","impact":"Result"}',
                        )
                    ]
                ),
                reply("Done"),
            ]
        )
        service = host.services.start(self.spec(), lifetime="detached")
        self.addCleanup(self.stop, host, service.id)
        work = service.work.submit("Ask then continue")
        eventually(lambda: service.requests(), 20)
        before = service.store.work(work.id)["run_id"]
        service.stop()
        eventually(lambda: not live(self.control), 15)
        restarted = host.services.start(identity=service.id, lifetime="detached")
        request = eventually(
            lambda: restarted.requests() and restarted.requests()[0], 20
        )
        host.requests.answer(request.ref, text="Continue", decision_id="answer")
        eventually(lambda: restarted.work.get(work.id).snapshot().state == "review", 20)
        self.assertEqual(restarted.store.work(work.id)["run_id"], before)
        self.assertEqual(len(model.requests), 3)
        restarted.stop()
        eventually(lambda: not live(self.control), 10)
