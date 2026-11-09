"""Independent workers and SDK decisions use the same control and resource chain."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from homebrew_harness import Harness, HostConfig
from homebrew_harness.runs import RunSpec
from tests.integration.test_native_model_protocols import ProtocolService
from tests.support.clients import reply, call
from tests.runtime.test_interaction import eventually
from homebrew_harness.api.runs import RunHandle


class APIRuns(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.root = self.base / "workspace"
        self.root.mkdir()

    def service(self, replies):
        service = ProtocolService([r.model_dump(exclude_none=True) for r in replies])
        self.addCleanup(service.close)
        environment = patch.dict("os.environ", {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        env = self.base / "model.env"
        env.write_text(
            "OPENAI_API_KEY=synthetic\nOPENAI_BASE_URL="
            + service.url
            + "\nOPENAI_MODEL=fixture\n"
        )
        config = HostConfig.from_files(
            env_file=env, shutdown_seconds=5, total_seconds=40, request_seconds=5
        )
        return service, config

    def test_start_actual_worker_preserves_identity_and_submission_replay(self):
        service, config = self.service(
            [
                reply(
                    calls=[
                        call(
                            "write",
                            name="write_file",
                            arguments='{"path":"out.txt","content":"done"}',
                        )
                    ]
                ),
                reply("done"),
            ]
        )
        with Harness(workdir=self.root, config=config) as host:
            spec = RunSpec(
                "Write out.txt",
                episode_model_calls=1,
                heartbeat_seconds=0.1,
                heartbeat_timeout=10,
                shutdown_seconds=3,
                total_seconds=30,
            )
            job = host.runs.start(spec, request_id="one")
            self.assertEqual(job.wait(25, until="settled").snapshot.state, "completed")
            again = host.runs.start(spec, request_id="one")
            self.assertEqual(job.id, again.id)
            self.assertEqual(len(service.requests), 2)
            self.assertEqual((self.root / "out.txt").read_text(), "done")
        self.assertEqual(job.snapshot().state, "completed")
        self.assertIsNone(host.run_store._anchor)

    def test_durable_attention_and_exact_approval_continue_the_same_run(self):
        service, config = self.service(
            [
                reply(
                    calls=[
                        call(
                            "attention",
                            name="request_attention",
                            arguments='{"question":"Which behavior?","reason":"Need a requirement","impact":"Output"}',
                        )
                    ]
                ),
                reply(
                    calls=[
                        call(
                            "shell",
                            name="bash",
                            arguments='{"command":"printf approved"}',
                        )
                    ]
                ),
                reply("done"),
            ]
        )
        with Harness(workdir=self.root, config=config) as host:
            from dataclasses import replace

            host.config = replace(
                config, settings=replace(config.settings, managed_bash=True)
            )
            spec = RunSpec(
                "Ask for behavior then run command",
                tools=("bash",),
                heartbeat_seconds=0.1,
                heartbeat_timeout=10,
                total_seconds=40,
                shutdown_seconds=3,
            )
            job = host.runs.start(spec)
            question = eventually(lambda: job.requests() and job.requests()[0], 15)
            self.assertEqual(question.kind, "attention")
            host.requests.answer(
                question.ref, text="Keep current behavior", decision_id="answer-one"
            )
            tool = eventually(
                lambda: next((r for r in job.requests() if r.kind == "tool"), None), 15
            )
            # An approved tool is executed through execute_approved by the model.
            service.responses.appendleft(
                reply(
                    calls=[
                        call(
                            "exec",
                            name="execute_approved",
                            arguments=json.dumps({"request_id": tool.ref.id}),
                        )
                    ]
                ).model_dump(exclude_none=True)
            )
            host.requests.approve(tool.ref, decision_id="approve-one")
            self.assertEqual(job.wait(20, until="settled").snapshot.state, "completed")

    def test_accepted_launch_failure_replays_the_same_queued_run(self):
        service, config = self.service([reply("done")])
        with Harness(workdir=self.root, config=config) as host:
            spec = RunSpec(
                "Work", total_seconds=30, heartbeat_seconds=0.1, heartbeat_timeout=10
            )
            with patch.object(
                RunHandle, "_ensure_started", side_effect=RuntimeError("launch failed")
            ):
                accepted = host.runs.start(spec, request_id="one")
            self.assertTrue(accepted.submission().accepted)
            self.assertEqual(accepted.submission().launch_state, "startup_pending")
            resumed = host.runs.start(spec, request_id="one")
            self.assertEqual(resumed.id, accepted.id)
            self.assertEqual(
                resumed.wait(20, until="settled").snapshot.state, "completed"
            )
            self.assertEqual(len(host.runs.list()), 1)

    def test_detached_run_outlives_the_observing_host(self):
        service, config = self.service(
            [
                reply(
                    calls=[
                        call(
                            "question",
                            name="request_attention",
                            arguments='{"question":"What next?","reason":"Wait for the host","impact":"Completion"}',
                        )
                    ]
                )
            ]
        )
        host = Harness(workdir=self.root, config=config)
        spec = RunSpec(
            "Ask then wait",
            heartbeat_seconds=0.1,
            heartbeat_timeout=10,
            total_seconds=40,
            shutdown_seconds=3,
        )
        job = host.runs.start(spec, lifetime="detached", request_id="detached")
        question = eventually(lambda: job.requests() and job.requests()[0], 20)
        self.assertEqual(question.kind, "attention")
        eventually(lambda: job.snapshot().state == "waiting", 10)
        self.assertEqual(host.close().state, "closed")
        with Harness(workdir=self.root, config=config) as other:
            observed = other.runs.get(job.id)
            self.assertEqual(observed.snapshot().state, "waiting")
            owner = other.runs.get(job.id, control=True)
            owner.cancel()
            self.assertEqual(
                owner.wait(15, until="settled").snapshot.state, "cancelled"
            )

    def test_control_config_is_validated_before_run_acceptance(self):
        from dataclasses import replace
        from homebrew_harness.api import InvalidInput

        _, config = self.service([])
        path = self.base / "control.json"
        path.write_text('{"version":true}')
        config = replace(config, settings=replace(config.settings, control_config=path))
        with Harness(workdir=self.root, config=config) as host:
            with self.assertRaises(InvalidInput):
                host.runs.start(RunSpec("Work"))
            self.assertEqual(host.runs.list(), ())

    def test_control_binding_is_saved_in_the_acceptance_transaction(self):
        from dataclasses import replace
        from homebrew_harness.runs.services import RunServices
        from homebrew_harness.execution.model_connections import read_environment

        service, config = self.service([])
        env = config.settings.env_file
        env.write_text(env.read_text() + "CONTROL_URL=" + service.url + "\n")
        path = self.base / "control.json"
        path.write_text('{"version":1,"notification":{"url_env":"CONTROL_URL"}}')
        config = replace(config, settings=replace(config.settings, control_config=path))
        with Harness(workdir=self.root, config=config) as host:
            with patch.object(RunHandle, "_ensure_started"):
                job = host.runs.start(RunSpec("Work"), request_id="control")
            state = host.run_store.get(job.id)
            self.assertTrue(state["notify_enabled"])
            services = RunServices.load(
                host.run_store, path, environment=read_environment(env, self.root)
            )
            try:
                self.assertEqual(state["control_identity"], services.identity)
            finally:
                services.close()
