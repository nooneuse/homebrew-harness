"""Actual managed workers reconnect and retain MCP effect boundaries."""

import json
import sys
import unittest

from homebrew_harness.runs import RunSpec
from homebrew_harness.runs.supervisor import RunSupervisor
from tests.runs import test_process as fixture
from tests.support.clients import reply, call
from tests.support.paths import PROJECT_ROOT


class RunMCPProcesses(unittest.TestCase):
    setUp = fixture.ProcessRuns.setUp
    service = fixture.ProcessRuns.service
    env = fixture.ProcessRuns.env

    def supervisor(self, command=None):
        env = self.base / "empty.env"
        env.write_text("")
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
                            "tools": {
                                "search": {"action": "allow", "read_only": True},
                                "record_note": {"action": "allow", "read_only": False},
                            },
                        }
                    }
                }
            )
        )
        supervisor = RunSupervisor(
            self.store,
            worker_command=command,
            host_arguments=("--env-file", str(env), "--mcp-config", str(config)),
        )
        self.addCleanup(supervisor.close)
        return supervisor

    def spec(self):
        return RunSpec(
            "Use the selected service",
            mcp={"docs": ["search", "record_note"]},
            episode_model_calls=1,
            heartbeat_seconds=0.05,
            heartbeat_timeout=15,
            shutdown_seconds=3,
            retry_delay=0.1,
        )

    def invoke(self, name, args):
        return reply(
            calls=[
                call("external", name="mcp__docs__" + name, arguments=json.dumps(args))
            ],
            finish_reason="tool_calls",
        )

    def logs(self):
        return "\n".join(
            p.read_text() for p in (self.root / ".harness/runs").rglob("*.log")
        )

    def test_real_workers_reconnect_between_episodes(self):
        model = self.service([self.invoke("search", {"query": "loop"}), reply("done")])
        state = self.store.create(self.spec())
        with self.env(model):
            result = self.supervisor().supervise(state["id"])
        self.assertEqual(result["status"], "completed", self.logs())
        self.assertTrue(result["cleanup_complete"])
        self.assertEqual(result["episode"], 2)
        resources = self.store.runtime.items("mcp_resources")
        self.assertGreaterEqual(len(resources), 1)
        self.assertTrue(all(v["phase"] == "closed" for v in resources.values()))
        self.assertEqual(len(model.requests), 2)

    def crash_command(self):
        path = self.base / "crash.py"
        path.write_text("""import os,sys
from pathlib import Path
from unittest.mock import patch
from homebrew_harness.runs import worker
from homebrew_harness.tools.mcp.runtime import MCPRuntime
root=Path(sys.argv[sys.argv.index('--cwd')+1]); marker=root/'.harness/crashed-once'
original=MCPRuntime.call
def invoke(self,*args,**kwargs):
 result=original(self,*args,**kwargs)
 if not marker.exists():
  marker.write_text('accepted');os._exit(77)
 return result
with patch.object(MCPRuntime,'call',invoke):raise SystemExit(worker.main())
""")
        return (sys.executable, "-I", "-B", str(path))

    def test_read_response_lost_to_worker_crash_is_settled_without_replay(self):
        model = self.service(
            [
                self.invoke("search", {"query": "loop"}),
                reply("A lost read is a failed observation"),
            ]
        )
        state = self.store.create(self.spec())
        with self.env(model):
            result = self.supervisor(self.crash_command()).supervise(state["id"])
        self.assertEqual(result["status"], "completed", self.logs())
        result_message = next(
            v
            for v in model.requests[-1]["messages"]
            if v.get("role") == "tool" and v.get("tool_call_id") == "external"
        )
        self.assertEqual(json.loads(result_message["content"])["outcome"], "failed")
        audit = [
            json.loads(v)
            for v in (self.base / "service/calls.jsonl").read_text().splitlines()
        ]
        self.assertEqual(len([v for v in audit if v.get("name") == "search"]), 1)

    def test_write_response_lost_to_worker_crash_blocks_replay(self):
        model = self.service(
            [self.invoke("record_note", {"text": "once"}), reply("must not be reached")]
        )
        state = self.store.create(self.spec())
        with self.env(model):
            result = self.supervisor(self.crash_command()).supervise(state["id"])
        self.assertEqual(result["status"], "blocked", self.logs())
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(
            len((self.base / "service/notes.jsonl").read_text().splitlines()), 1
        )
