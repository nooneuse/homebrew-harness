"""Run bindings and worker death use real Docker, SDK stdio and resource owners."""

import json
import subprocess
import sys
import unittest

from homebrew_harness import HarnessSession, Capabilities
from homebrew_harness.runs import RunSpec, RunStore, RunController
from homebrew_harness.tools.mcp.runtime import MCPRuntime
from tests.system import test_mcp_docker as fixture
from tests.support.clients import reply, call, fake_client
from tests.runtime.test_interaction import eventually


@unittest.skipUnless(
    fixture.IMAGE and sys.platform == "linux",
    "requires explicit Linux Docker MCP image",
)
class RunMCPDockerTests(unittest.TestCase):
    setUp = fixture.MCPDockerTests.setUp

    def test_managed_idle_scope_eviction_removes_real_containers(self):
        runtime = MCPRuntime(
            {"probe": self.spec},
            workdir=self.cwd,
            selection={"probe": ["read_text"]},
            resource_scope={"run_id": "fixture", "session_id": "fixture"},
            max_container_connections=2,
        )
        self.addCleanup(lambda: self.assertTrue(runtime.wait_closed(12)))
        self.addCleanup(runtime.close)
        connections = []
        for index in range(3):
            cwd = self.cwd / ".worktrees" / str(index)
            cwd.mkdir(parents=True)
            (cwd / "input.txt").write_text("scope " + str(index))
            task = "task_" + str(index) * 8
            runtime.connect("probe", workdir=cwd, task_id=task)
            binding = runtime.snapshot(workdir=cwd, task_id=task)[1][
                "mcp__probe__read_text"
            ]
            result = json.loads(
                runtime.call(binding, {"path": "input.txt"}, workdir=cwd, task_id=task)
            )
            self.assertEqual(result["structuredContent"]["text"], "scope " + str(index))
            connections.append(binding.connection)
        self.assertTrue(connections[0].closed)
        self.assertFalse(connections[0].owner._leased)
        runtime.close()
        self.assertTrue(runtime.wait_closed(12))
        self.assertTrue(
            all(
                json.loads(p.read_text())["phase"] == "removed"
                for p in (self.cwd / ".harness/containers").glob("*.json")
            )
        )

    def test_managed_read_uses_real_container_and_closes_resources(self):
        (self.cwd / "input.txt").write_text("managed container input")
        store = RunStore(self.cwd)
        self.addCleanup(store.close)
        spec = RunSpec("Read container input", mcp={"probe": ["read_text"]})
        state = store.claim(store.create(spec)["id"])
        control = RunController(store, state["id"], state["owner"], state["generation"])
        self.addCleanup(control.close_heartbeat)
        runtime = MCPRuntime(
            {"probe": self.spec},
            workdir=self.cwd,
            selection=spec.mcp,
            resource_scope={**control.header, "session_id": state["session_id"]},
        )
        client, requests = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "read",
                            name="mcp__probe__read_text",
                            arguments='{"path":"input.txt"}',
                        )
                    ]
                ),
                reply("Container input read"),
            ]
        )
        session = HarnessSession(
            client,
            "fixture",
            self.cwd,
            capabilities=Capabilities(spec.enabled),
            run_control=control,
            mcp_runtime=runtime,
        )
        self.addCleanup(lambda: self.assertTrue(session.wait_closed(10)))
        self.addCleanup(session.close)
        result = control.execute()
        self.assertEqual(result.status, "completed", result.data())
        self.assertIn("managed container input", json.dumps(requests[-1]))
        session.close()
        self.assertTrue(session.wait_closed(10), session.shutdown_status())
        self.assertTrue(
            all(
                v["phase"] == "closed"
                for v in store.runtime.items("mcp_resources").values()
            )
        )
        self.assertTrue(
            all(
                json.loads(p.read_text())["phase"] == "removed"
                for p in (self.cwd / ".harness/containers").glob("*.json")
            )
        )

    def test_sigkill_worker_removes_owned_container_and_descendants(self):
        script = self.root / "worker.py"
        script.write_text("""import sys,time,json
from pathlib import Path
from homebrew_harness.execution.mcp_policy import MCPExecutionPolicy
from homebrew_harness.tools.mcp.runtime import MCPRuntime,ServerSpec,ToolPolicy
root=Path(sys.argv[1]);image=sys.argv[2];cli=sys.argv[3]
policy=MCPExecutionPolicy(mode='container',cli=cli,image=image,inputs='workspace')
spec=ServerSpec('probe','stdio',command='/usr/local/bin/python',args=('/opt/mcp_container_server.py',),execution=policy,tools={'spawn_detached':ToolPolicy('allow',False)})
r=MCPRuntime({'probe':spec},workdir=root,selection={'probe':['spawn_detached']},resource_scope={'run_id':'fixture','session_id':'fixture'})
r.connect('probe');binding=r.snapshot()[1]['mcp__probe__spawn_detached'];r.call(binding,{})
print('ready',flush=True);time.sleep(60)
""")
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                str(script),
                str(self.cwd),
                fixture.IMAGE,
                fixture.CLI,
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
        self.assertEqual(process.stdout.readline().strip(), "ready")
        store = RunStore(self.cwd)
        self.addCleanup(store.close)
        records = list((self.cwd / ".harness/containers").glob("*.json"))
        self.assertEqual(len(records), 1)
        process.kill()
        process.wait(5)
        eventually(lambda: json.loads(records[0].read_text())["phase"] == "removed", 12)
        eventually(
            lambda: all(
                v["phase"] == "closed"
                for v in store.runtime.items("mcp_resources").values()
            ),
            12,
        )
        self.assertTrue(
            all(
                v["phase"] == "closed"
                for v in store.runtime.items("mcp_resources").values()
            )
        )
        name = json.loads(records[0].read_text())["name"]
        observed = subprocess.run(
            [
                fixture.CLI,
                "container",
                "ls",
                "-a",
                "--filter",
                "name=" + name,
                "--format",
                "{{.Names}}",
            ],
            capture_output=True,
            text=True,
            timeout=3,
        )
        self.assertEqual(observed.returncode, 0)
        self.assertNotIn(name, observed.stdout.splitlines())
