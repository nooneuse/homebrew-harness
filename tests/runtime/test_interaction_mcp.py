"""Automatic Run selection and closed Session handoff with real MCP transport."""

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from homebrew_harness.runtime.interaction_config import (
    InteractionConfig,
    InteractionAssembler,
)
from homebrew_harness.runtime.interaction_host import InteractionHost
from homebrew_harness.runtime.task_routing import TaskRoute
from tests.integration.test_native_model_protocols import ProtocolService
from tests.runtime.test_interaction import eventually
from tests.support.clients import call, fake_client, reply
from tests.support.paths import PROJECT_ROOT


class InteractionMCPTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.root = self.base / "workspace"
        self.root.mkdir()

    def host(self, model, *, mode="managed", factory=None, detached=False):
        config = self.base / "mcp.json"
        config.write_text(
            json.dumps(
                {
                    "servers": {
                        "docs": {
                            "transport": "stdio",
                            "command": sys.executable,
                            "args": [str(PROJECT_ROOT / "examples/mcp_demo_server.py")],
                            "cwd": str(self.base),
                            "env": {"MCP_DEMO_STATE_DIR": str(self.base / "service")},
                            "tools": {"search": {"action": "allow", "read_only": True}},
                        }
                    }
                }
            )
        )
        env = self.base / "fixture.env"
        env.write_text(
            "OPENAI_API_KEY=synthetic\nOPENAI_BASE_URL="
            + model.url
            + "\nOPENAI_MODEL=fixture\n"
        )
        tests = self.base / "tests.json"
        tests.write_text(json.dumps({"version": 1, "recipes": []}))
        settings = InteractionConfig(
            env_file=env,
            mcp_config=config,
            test_config=tests,
            allowed=("mcp",),
            model_planning=False,
            detached=detached,
            shutdown_seconds=3,
            total_seconds=60,
        )

        class Router:
            def propose(self, *_args, **_kwargs):
                return TaskRoute(
                    execution_mode=mode, capabilities=("mcp",), mcp={"docs": ["search"]}
                )

        # The worker receives the same synthetic environment as the input owner.
        environment = patch.dict(
            "os.environ",
            {
                "OPENAI_API_KEY": "synthetic",
                "OPENAI_BASE_URL": model.url,
                "OPENAI_MODEL": "fixture",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        assembler = InteractionAssembler(self.root, settings, client_factory=factory)
        host = InteractionHost(assembler, trusted=True, router=Router())
        self.addCleanup(host.close)
        return host

    def service(self, values):
        model = ProtocolService([v.model_dump(exclude_none=True) for v in values])
        self.addCleanup(model.close)
        return model

    def search(self):
        return reply(
            calls=[
                call("search", name="mcp__docs__search", arguments='{"query":"loop"}')
            ]
        )

    def test_named_mcp_automatic_run_finishes_with_actual_worker(self):
        model = self.service([self.search(), reply("Documentation read")])
        host = self.host(model)
        task = host.submit("Read Agent Loop documentation")
        eventually(
            lambda: host.state.get(task["task_id"])["state"] in {"finished", "blocked"},
            35,
        )
        current = host.state.get(task["task_id"])
        self.assertEqual(current["state"], "finished", host.status())
        run = host.runs.get(current["target"]["id"])
        self.assertEqual(run["spec"]["mcp"], {"docs": ["search"]})
        self.assertEqual(set(run["mcp_manifest"]), {"mcp__docs__search"})
        self.assertTrue(run["cleanup_complete"])
        self.assertEqual(len(model.requests), 2)

    def test_detached_auto_run_keeps_mcp_configuration(self):
        model = self.service([self.search(), reply("Detached documentation read")])
        host = self.host(model, detached=True)
        task = host.submit("Read documentation in background")
        eventually(
            lambda: host.state.get(task["task_id"])["state"] in {"finished", "blocked"},
            35,
        )
        self.assertEqual(
            host.state.get(task["task_id"])["state"], "finished", host.status()
        )
        self.assertTrue((self.base / "service/calls.jsonl").exists())

    def test_mcp_session_handoff_closes_actor_and_freezes_same_manifest(self):
        model = self.service([reply("Managed continuation complete")])
        entered, release = threading.Event(), threading.Event()
        values = iter(
            [
                reply(
                    calls=[
                        call("connect", name="connect_mcp", arguments='{"name":"docs"}')
                    ]
                ),
                self.search(),
                reply("Source lookup finished"),
            ]
        )
        count = 0

        from contextlib import contextmanager
        from homebrew_harness.execution.providers.adapter import ProtocolAdapter

        @contextmanager
        def response(*_args):
            nonlocal count
            count += 1
            result=next(values)
            if count==3:
                entered.set()
                release.wait(10)
            yield result.model_dump(exclude_none=True)

        def factory(**_kwargs):
            client=ProtocolAdapter("chat_completions",api_key="synthetic",base_url=model.url)
            client.backend.response=response
            return client

        host = self.host(model, mode="interactive", factory=factory)
        self.addCleanup(release.set)
        task = host.submit("Look up documentation and continue as managed")
        self.assertTrue(entered.wait(15), host.status())
        source = host.session
        self.assertTrue(source._input_dispatching.is_set())
        manifest = source.mcp.manifest()
        host.upgrade()
        self.assertEqual(host.state.get(task["task_id"])["target"]["kind"], "session")
        release.set()
        eventually(
            lambda: (
                host.state.get(task["task_id"])["target"]["kind"] == "run"
                or host.state.get(task["task_id"])["state"] == "blocked"
            ),
            20,
        )
        current = host.state.get(task["task_id"])
        self.assertEqual(current["target"]["kind"], "run", host.status())
        self.assertTrue(source.wait_closed(0))
        run = host.runs.get(current["target"]["id"])
        self.assertEqual(run["mcp_manifest"], manifest)
        self.assertEqual(run["expected_mcp"], source.mcp.declaration)
        eventually(
            lambda: host.state.get(task["task_id"])["state"] in {"finished", "blocked"},
            20,
        )
        self.assertEqual(
            host.state.get(task["task_id"])["state"], "finished", host.status()
        )
