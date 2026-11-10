"""Workflow entry points retain model limits and exact inference identities."""

from tests.support.clients import cli_model_factory

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from homebrew_harness import Capabilities, HarnessSession
from homebrew_harness.cli import main
from homebrew_harness.execution.model_registry import ModelRegistry, ModelSpec
from homebrew_harness.orchestration.workflow_runtime import (
    ModelWorkflowRunner,
    WorkflowError,
    WorkflowRegistry,
    WorkflowSpec,
)
from tests.support.clients import call, fake_client, reply


async def step(ctx, args):
    return await ctx.agent("Short step")


class WorkflowModelBindingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.root = self.base / "workspace"
        self.root.mkdir()
        for capture in (redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            capture.__enter__()
            self.addCleanup(capture.__exit__, None, None, None)

    def registry(self, client, *, temperature=0.1, options=None, context=50000):
        client.close = lambda: None
        return ModelRegistry(
            [
                ModelSpec(
                    "selected",
                    "fixture",
                    "fixture-credential",
                    "https://model.example.invalid",
                    context_chars=context,
                    max_output_tokens=128,
                    options_json=json.dumps(
                        options if options is not None else {"temperature": temperature}
                    ),
                )
            ],
            lead_default="selected",
            client_factory=lambda **_kwargs: client,
        )

    def session(self, temperature=0.1):
        client, _ = fake_client([reply("step-output")])
        models = self.registry(client, temperature=temperature)
        session = HarnessSession(
            client,
            "fixture",
            self.root,
            model_registry=models,
            capabilities=Capabilities({"workflow"}),
        )
        self.addCleanup(lambda: session.wait_closed(3))
        self.addCleanup(session.close)
        session.workflow.registry.register(
            WorkflowSpec("step", "step", step, {"type": "object"})
        )
        return session, client

    def test_session_workflow_limits_and_binding_cache(self):
        first, client = self.session()
        output = first.workflow.run("step", {})
        identity = output["task"]["run_id"]
        self.assertEqual(
            client.chat.completions.create.call_args.kwargs["max_tokens"], 128
        )
        first.close()
        self.assertTrue(first.wait_closed(3))
        second, client = self.session()
        cached = second.workflow.run("step", resume_from_run_id=identity)
        self.assertEqual(cached["task"]["progress"]["cached_steps"], 1)
        client.chat.completions.create.assert_not_called()
        second.close()
        self.assertTrue(second.wait_closed(3))
        changed, client = self.session(temperature=0.9)
        with self.assertRaisesRegex(WorkflowError, "runner"):
            changed.workflow.run("step", resume_from_run_id=identity)
        client.chat.completions.create.assert_not_called()

    def test_real_protocol_projection_enforces_context_limit(self):
        client, _ = fake_client([])
        models = self.registry(
            client,
            options={"extensions": {"metadata": {"note": "x" * 7000}}},
            context=10000,
        )
        self.addCleanup(models.close)
        runner = ModelWorkflowRunner(models.select("selected"))
        with self.assertRaisesRegex(WorkflowError, "context limit"):
            runner.run("x" * 3500, None, "step", 2048)
        client.chat.completions.create.assert_not_called()

    def test_raw_client_cannot_omit_binding(self):
        client, _ = fake_client([])
        with self.assertRaisesRegex(ValueError, "ModelBinding"):
            ModelWorkflowRunner(client)

    def test_session_cli_dispatch_preserves_limits_in_all_three_calls(self):
        config = self.base / "models.json"
        config.write_text(
            json.dumps(
                {
                    "version": 2,
                    "connections": {
                        "fixture": {
                            "protocol": "chat_completions",
                            "api_key_env": "FIXTURE_KEY",
                            "base_url_env": "FIXTURE_URL",
                        }
                    },
                    "profiles": {
                        "selected": {
                            "connection": "fixture",
                            "model_env": "FIXTURE_MODEL",
                            "context_chars": 50000,
                            "max_output_tokens": 128,
                        }
                    },
                    "defaults": {"lead": "selected"},
                }
            )
        )
        client, _ = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "step",
                            name="workflow",
                            arguments='{"name":"step","args":{}}',
                        )
                    ]
                ),
                reply("step-output"),
                reply("Done"),
            ]
        )
        client.close = lambda: None
        registry = WorkflowRegistry(
            [WorkflowSpec("step", "step", step, {"type": "object"})]
        )
        with (
            patch.dict(
                os.environ,
                {
                    "FIXTURE_KEY": "fixture-credential",
                    "FIXTURE_URL": "https://model.example.invalid",
                    "FIXTURE_MODEL": "fixture",
                },
                clear=True,
            ),
            patch("pathlib.Path.home", return_value=self.base),
            cli_model_factory( return_value=client),
            patch(
                "homebrew_harness.orchestration.workflows.default_registry",
                return_value=registry,
            ),
        ):
            code = main(
                [
                    "session",
                    "--cwd",
                    str(self.root),
                    "--env-file",
                    str(self.base / "missing.env"),
                    "--models-config",
                    str(config),
                    "--demo-workflows",
                    "--shutdown-timeout",
                    "2",
                    "Run the workflow",
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(
            [
                c.kwargs["max_tokens"]
                for c in client.chat.completions.create.call_args_list
            ],
            [128, 128, 128],
        )
