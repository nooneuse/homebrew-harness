import tempfile
import unittest
import asyncio
import threading
from pathlib import Path
from homebrew_harness import Harness, HostConfig
from homebrew_harness.orchestration.workflow_runtime import (
    WorkflowSpec,
    WorkflowRegistry,
)
from homebrew_harness.api import InvalidInput
from homebrew_harness.execution.model_registry import ModelRegistry, ModelSpec
from tests.integration.test_native_model_protocols import ProtocolService
from tests.support.clients import fake_client


class APIWorkflows(unittest.TestCase):
    def test_nonblocking_registered_workflow_and_saved_reader(self):
        async def echo(ctx, args):
            return {"echo": args["value"]}

        schema = {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        }
        registry = WorkflowRegistry([WorkflowSpec("echo", "Echo input", echo, schema)])
        client, _ = fake_client([])
        config = HostConfig.from_components(
            client=client, model="fixture", workflow_registry=registry
        )
        with tempfile.TemporaryDirectory() as root:
            with Harness(workdir=Path(root), config=config) as host:
                job = host.workflows.start("echo", {"value": "hello"}, request_id="one")
                self.assertEqual(job.wait(5).snapshot.state, "completed")
                self.assertEqual(job.result().value, {"echo": "hello"})
                self.assertEqual(
                    host.workflows.start(
                        "echo", {"value": "hello"}, request_id="one"
                    ).id,
                    job.id,
                )
                identity = job.id
            with Harness(workdir=Path(root), config=config) as host:
                observed = host.workflows.get(identity)
                self.assertEqual(observed.result().value, {"echo": "hello"})

    def test_invalid_arguments_do_not_create_a_submission(self):
        async def echo(ctx, args):
            return args

        schema = {
            "type": "object",
            "required": ["value"],
            "properties": {"value": {"type": "string"}},
        }
        client, _ = fake_client([])
        config = HostConfig.from_components(
            client=client,
            model="fixture",
            workflow_registry=WorkflowRegistry(
                [WorkflowSpec("echo", "Echo", echo, schema)]
            ),
        )
        with (
            tempfile.TemporaryDirectory() as root,
            Harness(workdir=Path(root), config=config) as host,
        ):
            with self.assertRaises(InvalidInput):
                host.workflows.start("echo", {}, request_id="one")
            self.assertEqual(host.workflows.list(), ())
            self.assertEqual(
                host.workflows.start("echo", {"value": "ok"}, request_id="one")
                .wait(5)
                .snapshot.state,
                "completed",
            )

    def test_saved_string_result_and_resume_use_the_original_model_profile(self):
        entered = threading.Event()
        count = 0

        async def script(ctx, args):
            nonlocal count
            count += 1
            if count == 1:
                entered.set()
                await asyncio.sleep(10)
            return "finished"

        service = ProtocolService([])
        self.addCleanup(service.close)
        client, _ = fake_client([])
        registry = ModelRegistry(
            [
                ModelSpec("lead", "lead-fixture", "synthetic", service.url),
                ModelSpec("worker", "worker-fixture", "synthetic", service.url),
            ],
            lead_default="lead",
            client_factory=lambda **_kw: client,
        )
        config = HostConfig.from_components(
            model_registry=registry,
            workflow_registry=WorkflowRegistry(
                [WorkflowSpec("script", "Script", script, {"type": "object"})]
            ),
        )
        with tempfile.TemporaryDirectory() as root:
            with Harness(workdir=Path(root), config=config) as host:
                job = host.workflows.start("script", model_profile="worker")
                self.assertTrue(entered.wait(5))
                job.pause()
                self.assertEqual(job.wait(5, until="settled").snapshot.state, "paused")
                resumed = job.resume()
                self.assertEqual(resumed.wait(5).snapshot.state, "completed")
                self.assertEqual(resumed.runtime.runner.binding.profile, "worker")
                identity = job.id
            with Harness(workdir=Path(root), config=config) as host:
                self.assertEqual(host.workflows.get(identity).result().text, "finished")
