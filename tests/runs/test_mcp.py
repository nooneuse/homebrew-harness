"""Run MCP uses actual SDK transports and persists exact invocation identity."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from homebrew_harness import HarnessSession, Capabilities
from homebrew_harness.runs import RunSpec, RunStore, RunController
from homebrew_harness.runs.requests import HumanRequests
from homebrew_harness.tools.mcp.runtime import MCPRuntime, ServerSpec, ToolPolicy
from tests.support.clients import fake_client, reply, call
from tests.support.paths import PROJECT_ROOT
from tests.runtime.test_interaction import eventually


class RunMCPTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.root = self.base / "workspace"
        self.root.mkdir()
        self.store = RunStore(self.root)
        self.addCleanup(self.store.close)
        self.server = ServerSpec(
            "docs",
            "stdio",
            sys.executable,
            (str(PROJECT_ROOT / "examples/mcp_demo_server.py"),),
            self.base,
            env={"MCP_DEMO_STATE_DIR": str(self.base / "service")},
            tools={
                "search": ToolPolicy("allow", True),
                "record_note": ToolPolicy("confirm", False),
                "get_version": ToolPolicy("allow", True),
            },
            connect_timeout_seconds=5,
        )

    def controller(self, values, *, identity=None, spec=None):
        state = (
            self.store.get(identity)
            if identity
            else self.store.create(
                spec
                or RunSpec(
                    "Use the selected documentation service",
                    mcp={"docs": ["search", "record_note"]},
                )
            )
        )
        state = self.store.claim(state["id"])
        control = RunController(
            self.store, state["id"], state["owner"], state["generation"]
        )
        client, requests = fake_client(values)
        runtime = MCPRuntime(
            {"docs": self.server},
            workdir=self.root,
            selection=control.spec.mcp,
            delegation=control.spec.mcp_delegation,
            resource_scope={**control.header, "session_id": state["session_id"]},
        )
        session = HarnessSession(
            client,
            "fixture",
            self.root,
            capabilities=Capabilities(control.spec.enabled),
            run_control=control,
            mcp_runtime=runtime,
            shutdown_timeout=2,
        )
        self.addCleanup(control.close_heartbeat)
        self.addCleanup(lambda: session.wait_closed(5))
        self.addCleanup(session.close)
        return control, session, requests

    def close(self, control, session):
        session.close()
        self.assertTrue(session.wait_closed(5))
        control.close_heartbeat()
        self.store.ended(
            control.header, cleanup=session.shutdown_status()["persistence_ok"]
        )

    def test_selected_tools_enter_pool_and_actual_stdio_call(self):
        control, session, requests = self.controller(
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
                reply("Read the selected documentation"),
            ]
        )
        outcome = control.execute()
        self.assertEqual(outcome.status, "completed", outcome.data())
        names = {d["function"]["name"] for d in session.tool_pool().tools}
        self.assertIn("mcp__docs__search", names)
        self.assertNotIn("mcp__docs__get_version", names)
        self.assertEqual(
            set(control.state["mcp_manifest"]),
            {"mcp__docs__search", "mcp__docs__record_note"},
        )
        self.close(control, session)
        self.assertTrue(
            all(
                row["phase"] == "closed"
                for row in self.store.runtime.items("mcp_resources").values()
            )
        )

    def test_write_approval_survives_closed_worker_and_consumes_once(self):
        control, session, _ = self.controller(
            [
                reply(
                    calls=[
                        call(
                            "note",
                            name="mcp__docs__record_note",
                            arguments='{"text":"one note"}',
                        )
                    ]
                )
            ]
        )
        outcome = control.execute()
        self.assertEqual(outcome.status, "waiting", outcome.data())
        request = HumanRequests(self.store).records(control.id)[0]
        self.assertIn("mcp", request["context"])
        self.close(control, session)
        HumanRequests(self.store).decide(
            control.id, request["id"], request["revision"], "approve"
        )
        resumed, next_session, _ = self.controller(
            [
                reply(
                    calls=[
                        call(
                            "approved",
                            name="execute_approved",
                            arguments=json.dumps({"request_id": request["id"]}),
                        )
                    ]
                ),
                reply("Note recorded"),
            ],
            identity=control.id,
        )
        result = resumed.execute()
        self.assertEqual(result.status, "completed")
        self.assertEqual(
            HumanRequests(self.store).get(control.id, request["id"])["state"],
            "consumed",
        )
        notes = (self.base / "service/notes.jsonl").read_text().splitlines()
        self.assertEqual(len(notes), 1)
        self.close(resumed, next_session)

    def test_unknown_selection_and_normalization_collision_rejected(self):
        for selected in ({"docs": []}, {"docs": ["x.y", "x_y"]}):
            with self.assertRaises(ValueError):
                RunSpec("invalid selection", mcp=selected)
        with self.assertRaises(ValueError):
            MCPRuntime(
                {"docs": self.server},
                workdir=self.root,
                selection={"docs": ["not_registered"]},
            )

    def test_worker_kill_closes_only_its_owned_stdio_group(self):
        script = self.base / "worker.py"
        script.write_text(
            """import sys,time,json\nfrom pathlib import Path\nfrom homebrew_harness.tools.mcp.runtime import MCPRuntime,ServerSpec,ToolPolicy\nroot=Path(sys.argv[1]);service=Path(sys.argv[2])\nspec=ServerSpec('docs','stdio',sys.executable,(sys.argv[3],),root,env={'MCP_DEMO_STATE_DIR':str(service)},tools={'get_version':ToolPolicy('allow',True)})\nr=MCPRuntime({'docs':spec},workdir=root,selection={'docs':['get_version']},resource_scope={'run_id':'fixture','session_id':'fixture'})\nr.connect('docs');print('ready',flush=True)\ntime.sleep(60)\n"""
        )
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                str(script),
                str(self.root),
                str(self.base / "service"),
                str(PROJECT_ROOT / "examples/mcp_demo_server.py"),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        def stop():
            if process.poll() is None:
                process.kill()
            process.wait(5)
            process.stdout.close()
            process.stderr.close()

        self.addCleanup(stop)
        ready = process.stdout.readline().strip()
        if ready != "ready":
            print(process.stderr.read(), flush=True)
        self.assertEqual(ready, "ready")
        records = self.store.runtime.items("mcp_resources")
        self.assertEqual(len(records), 1)
        row = next(iter(records.values()))
        process.kill()
        process.wait(5)
        eventually(
            lambda: (
                self.store.runtime.get("mcp_resources", row["id"])["phase"] == "closed"
            ),
            8,
        )
        with self.assertRaises(ProcessLookupError):
            os.killpg(row["child_pid"], 0)
