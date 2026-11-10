import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from homebrew_harness import Harness, HostConfig, ServiceSpec
from homebrew_harness.api.types import SDKError
from homebrew_harness.services.control import ControlClient
from tests.integration.test_native_model_protocols import ProtocolService
from tests.runtime.test_interaction import eventually
from tests.support.clients import reply, call


class ServiceRuntimeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.control = self.base / "control"
        self.control.mkdir(mode=0o700)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        environment = patch.dict("os.environ", {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def host(self, replies, **settings):
        model = ProtocolService([r.model_dump(exclude_none=True) for r in replies])
        self.addCleanup(model.close)
        env = self.base / "model.env"
        env.write_text(
            "OPENAI_API_KEY=synthetic\nOPENAI_BASE_URL="
            + model.url
            + "\nOPENAI_MODEL=fixture\n"
        )
        host = Harness(
            workdir=self.control,
            config=HostConfig.from_files(env_file=env, shutdown_seconds=3, **settings),
        )
        self.addCleanup(host.close)
        return host, model

    def spec(self, **options):
        overrides = options.pop("run_defaults", {})
        return ServiceSpec(
            str(self.workspace),
            run_defaults={
                "total_seconds": 30,
                "request_seconds": 5,
                "episode_model_calls": 2,
                "heartbeat_seconds": 0.1,
                "heartbeat_timeout": 10,
                "shutdown_seconds": 2,
                **overrides,
            },
            shutdown_seconds=3,
            **options,
        )

    def test_two_serial_native_runs_keep_distinct_work_refs_and_replay(self):
        host, model = self.host([reply("First"), reply("Second")])
        service = host.services.start(self.spec())
        first = service.work.submit("First", request_id="one")
        second = service.work.submit("Second", request_id="two")
        self.assertEqual(first.wait(15).snapshot.state, "review")
        self.assertEqual(second.wait(15).snapshot.state, "review")
        self.assertEqual(first.result().text, "First")
        self.assertEqual(second.result().text, "Second")
        self.assertEqual(first.id, service.work.submit("First", request_id="one").id)
        self.assertEqual(len(model.requests), 2)
        service.stop()
        self.assertTrue(service.wait_closed(8))
        self.assertEqual(host.close().state, "closed")

    def test_owner_only_control_reconnect_and_duplicate_submit(self):
        host, model = self.host([reply("Done")])
        service = host.services.start(self.spec())
        client = ControlClient(self.control)
        payload = {"text": "Work", "goal": None, "priority": 0}
        first = client.call(service.id, "submit", payload, "one")
        again = ControlClient(self.control).call(service.id, "submit", payload, "one")
        self.assertEqual(first, again)
        self.assertEqual(
            service.work.get(first["id"]).wait(15).snapshot.state, "review"
        )
        with self.assertRaises(SDKError):
            client.call(service.id, "submit", {**payload, "actor": "owner"}, "forged")
        with self.assertRaises(SDKError):
            client.call("svc_" + "f" * 32, "snapshot")
        self.assertEqual(len(model.requests), 1)
        service.stop()
        self.assertTrue(service.wait_closed(8))

    def test_native_attention_is_visible_and_answered_through_root_requests(self):
        host, model = self.host(
            [
                reply(
                    calls=[
                        call(
                            "ask",
                            name="request_attention",
                            arguments='{"question":"What behavior?","reason":"Clarify","impact":"Result"}',
                        )
                    ]
                ),
                reply("Done"),
            ]
        )
        service = host.services.start(self.spec())
        work = service.work.submit("Ask then work")
        request = eventually(
            lambda: (
                host.requests.list(target=service.ref)
                and host.requests.list(target=service.ref)[0]
            ),
            15,
        )
        self.assertEqual(request.kind, "attention")
        self.assertEqual(request.ref.target.owner_id, service.id)
        with self.assertRaises(SDKError):
            host.requests.approve(request.ref)
        host.requests.answer(
            request.ref, text="Keep current behavior", decision_id="answer"
        )
        eventually(lambda: work.snapshot().state == "review", 15)
        self.assertTrue(work.wait(0).condition_met)
        service.stop()
        self.assertTrue(service.wait_closed(8))

    def test_pause_blocks_new_work_and_queued_cancel_does_not_execute(self):
        host, model = self.host([reply("Done")])
        service = host.services.start(self.spec())
        service.pause()
        queued = service.work.submit("Do not run")
        queued.cancel()
        second = service.work.submit("Run after resume")
        self.assertEqual(queued.snapshot().state, "cancelled")
        self.assertEqual(len(model.requests), 0)
        service.resume()
        self.assertEqual(second.wait(15).snapshot.state, "review")
        self.assertEqual(len(model.requests), 1)
        service.stop()
        self.assertTrue(service.wait_closed(8))

    def test_observer_is_read_only_and_closing_it_keeps_owner(self):
        host, _ = self.host([])
        service = host.services.start(self.spec())
        with Harness(workdir=self.control) as observer:
            view = observer.services.get(service.id)
            with self.assertRaises(SDKError):
                view.work.submit("Not authorized through observation")
            self.assertEqual(view.snapshot().state, "running")
        self.assertEqual(service.snapshot().state, "running")
        service.stop()
        self.assertTrue(service.wait_closed(8))

    def test_exact_bash_approval_runs_once_through_the_native_dispatcher(self):
        host, model = self.host(
            [
                reply(
                    calls=[
                        call(
                            "shell",
                            name="bash",
                            arguments='{"command":"printf approved"}',
                        )
                    ]
                ),
                reply("Done"),
            ],
            managed_bash=True,
        )
        service = host.services.start(self.spec(run_defaults={"tools": ["bash"]}))
        work = service.work.submit("Run the exact approved command")
        request = eventually(lambda: service.requests() and service.requests()[0], 15)
        self.assertEqual(request.kind, "tool")
        model.responses.appendleft(
            reply(
                calls=[
                    call(
                        "execute",
                        name="execute_approved",
                        arguments='{"request_id":"' + request.ref.id + '"}',
                    )
                ]
            ).model_dump(exclude_none=True)
        )
        host.requests.approve(request.ref, decision_id="approve")
        eventually(lambda: work.snapshot().state == "review", 20)
        self.assertEqual(
            host.requests.approve(request.ref, decision_id="approve")["state"],
            "approved",
        )
        service.stop()
        self.assertTrue(service.wait_closed(8))

    def test_queued_work_pause_skips_it_without_losing_the_original_input(self):
        host, model = self.host([reply("Second"), reply("First")])
        service = host.services.start(self.spec())
        service.pause()
        first = service.work.submit("First", request_id="first")
        first.pause()
        second = service.work.submit("Second", request_id="second")
        service.resume()
        self.assertEqual(second.wait(15).snapshot.state, "review")
        self.assertEqual(first.input()["text"], "First")
        first.resume()
        self.assertEqual(first.wait(15).snapshot.state, "review")
        self.assertEqual(first.result().text, "First")
        service.stop()
        self.assertTrue(service.wait_closed(8))

    def test_failed_service_thread_launch_is_owned_and_can_close(self):
        from homebrew_harness.runtime.runtime_control import Workers

        host, _ = self.host([])
        original = Workers.start

        def start(workers, *args, **kwargs):
            if kwargs.get("name") == "service-control":
                raise RuntimeError("launch rejected")
            return original(workers, *args, **kwargs)

        with patch.object(Workers, "start", start):
            service = host.services.start(self.spec())
        self.assertTrue(service.wait_closed(8))
        self.assertEqual(host.close().state, "closed")

    def test_root_close_is_bounded_and_waits_for_native_process_cleanup(self):
        host, model = self.host(
            [
                reply(
                    calls=[
                        call(
                            "question",
                            name="request_attention",
                            arguments='{"question":"Wait?","reason":"Wait","impact":"Result"}',
                        )
                    ]
                )
            ]
        )
        service = host.services.start(self.spec())
        service.work.submit("Ask")
        eventually(lambda: service.requests(), 15)
        report = host.close(0.01)
        self.assertEqual(report.state, "cleanup_pending")
        self.assertEqual(host.wait_closed(10).state, "closed")
        self.assertTrue(service.snapshot().cleanup["resources_closed"])
