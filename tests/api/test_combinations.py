"""Real MCP transports, automatic routing and native worker combinations."""

import json
import sys
import unittest

from homebrew_harness import Harness, HostConfig
from homebrew_harness.api import SessionOptions
from homebrew_harness.runtime.task_routing import TaskRoute
from homebrew_harness.runs import RunSpec
from tests.api import test_runs as run_fixture
from tests.support.clients import fake_client, reply, call
from tests.support.paths import PROJECT_ROOT


class APICombinations(unittest.TestCase):
    setUp = run_fixture.APIRuns.setUp
    service = run_fixture.APIRuns.service

    def mcp_config(self):
        path = self.base / "mcp.json"
        path.write_text(
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
        return path

    def test_session_mcp_uses_actual_transport_and_closes(self):
        client, messages = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "search",
                            name="mcp__docs__search",
                            arguments='{"query":"loop"}',
                        )
                    ]
                ),
                reply("Documentation read"),
            ]
        )
        config = HostConfig.from_components(
            client=client, model="fixture", mcp_config=self.mcp_config()
        )
        with Harness(workdir=self.root, config=config) as host:
            session = host.sessions.open(SessionOptions(mcp={"docs": ["search"]}))
            turn = session.submit("Read Agent Loop documentation")
            self.assertEqual(turn.wait(10).snapshot.state, "completed")
            self.assertEqual(turn.result().text, "Documentation read")
            self.assertTrue(any(m.get("role") == "tool" for m in messages[-1]))
        self.assertTrue(session.runtime.wait_closed(0))

    def test_auto_routing_to_a_real_run_retains_the_task_handle(self):
        from dataclasses import replace

        service, config = self.service([reply("Automatic work finished")])
        config = replace(
            config, settings=replace(config.settings, model_planning=False)
        )

        class Router:
            def propose(self, *_args, **_kwargs):
                return TaskRoute(execution_mode="managed")

        with Harness(workdir=self.root, config=config) as host:
            entrance = host.interactions.open(router=Router())
            job = entrance.submit(
                "/approve is ordinary task material", request_id="one"
            )
            report = job.wait(25)
            self.assertEqual(report.snapshot.state, "completed", report.data())
            self.assertEqual(job.result().text, "Automatic work finished")
            self.assertEqual(job.ref.kind, "interaction")
            self.assertEqual(job.snapshot().details["target"]["kind"], "run")
            self.assertEqual(
                entrance.submit(
                    "/approve is ordinary task material", request_id="one"
                ).id,
                job.id,
            )

    def test_run_mcp_reconnects_across_episodes(self):
        from dataclasses import replace

        service, config = self.service(
            [
                reply(
                    calls=[
                        call(
                            "search",
                            name="mcp__docs__search",
                            arguments='{"query":"loop"}',
                        )
                    ]
                ),
                reply("done"),
            ]
        )
        config = replace(
            config,
            settings=replace(
                config.settings, mcp_config=self.mcp_config(), allowed=("mcp",)
            ),
        )
        spec = RunSpec(
            "Read documentation",
            mcp={"docs": ["search"]},
            episode_model_calls=1,
            heartbeat_seconds=0.1,
            heartbeat_timeout=10,
            total_seconds=40,
        )
        with Harness(workdir=self.root, config=config) as host:
            job = host.runs.start(spec)
            report = job.wait(30, until="settled")
            self.assertEqual(report.snapshot.state, "completed", report.data())
            self.assertTrue(
                all(
                    r["phase"] == "closed"
                    for r in host.store.items("mcp_resources").values()
                )
            )
            self.assertEqual(len(service.requests), 2)
