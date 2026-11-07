"""Real SDK stdio on a fake process backend; OS isolation uses separate Docker probes."""

import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from homebrew_harness import Capabilities, HarnessSession
from homebrew_harness.execution.bash_policy import bash_snapshot
from homebrew_harness.execution.execution_control import check
from homebrew_harness.execution.mcp_policy import MCPExecutionPolicy
from homebrew_harness.execution.mcp_runner import MCPWorkspace, snapshot_policy
from homebrew_harness.tools.mcp.runtime import (
    MCPError,
    MCPRuntime,
    ServerSpec,
    ToolPolicy,
    load_specs,
)
from tests.support.clients import call, fake_client, reply
from tests.support.paths import PROJECT_ROOT

SERVER = PROJECT_ROOT / "examples/mcp_workspace_server.py"


class ProcessBackend:
    cleanup_gate = None
    launch_gate = None
    instances = []

    def __init__(self, spec):
        self.spec = spec
        self.policy = type(
            "Policy",
            (),
            {
                "argv": (spec.command, *spec.args),
                "native_argv": (spec.command, *spec.args),
            },
        )()
        self.inputs = {}
        self.active = False
        self.instances.append(self)

    def launch(self, request):
        self.request = request
        if self.launch_gate:
            while not self.launch_gate.wait(0.01):
                check(request.deadline, request.cancel_events)
        self.inputs = (
            bash_snapshot(request.cwd, snapshot_policy(self.spec.execution))[0]
            if self.spec.execution.inputs == "workspace"
            else {}
        )
        self.active = True
        return subprocess.Popen(
            [sys.executable, "-B", str(SERVER)],
            cwd=request.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            start_new_session=True,
        )

    def finish(self, process):
        self.active = False
        return self.status()

    def status(self):
        return {
            "cleanup_complete": not self.active
            and (self.cleanup_gate is None or self.cleanup_gate.is_set())
        }

    def wait_closed(self, timeout=0):
        if self.active:
            return False
        return self.cleanup_gate is None or self.cleanup_gate.wait(timeout)


class MCPContainerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.policy = MCPExecutionPolicy(
            mode="container",
            cli="/missing/docker",
            image="sha256:" + "a" * 64,
            inputs="workspace",
        )
        self.spec = ServerSpec(
            "docs",
            "stdio",
            command="/image/server",
            execution=self.policy,
            tools={
                "get_version": ToolPolicy("allow", True),
                "read_text": ToolPolicy("allow", True),
            },
        )
        ProcessBackend.instances = []
        ProcessBackend.cleanup_gate = None
        ProcessBackend.launch_gate = None
        self.patcher = patch(
            "homebrew_harness.execution.mcp_runner.MCPContainerBackend", ProcessBackend
        )
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        for capture in (redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            capture.__enter__()
            self.addCleanup(capture.__exit__, None, None, None)

    def runtime(self, **kwargs):
        runtime = MCPRuntime({"docs": self.spec}, workdir=self.root, **kwargs)
        self.addCleanup(lambda: runtime.wait_closed(5))
        self.addCleanup(runtime.close)
        return runtime

    def binding(self, runtime, cwd=None, task=None, tool="get_version"):
        return runtime.snapshot(workdir=cwd or self.root, task_id=task)[1][
            "mcp__docs__" + tool
        ]

    def test_policy_and_loader_reject_network_credentials_host_paths_and_mutable_images(
        self,
    ):
        config = {
            "transport": "stdio",
            "command": "/image/server",
            "execution": {
                "mode": "container",
                "cli": "/missing/docker",
                "image": "sha256:" + "a" * 64,
            },
        }
        path = self.root / "config.json"
        for extra in (
            {"env": {"TOKEN": "fixture"}},
            {"cwd": "."},
            {"execution": {**config["execution"], "network": "host"}},
            {"execution": {**config["execution"], "image": "image:latest"}},
            {"execution": {**config["execution"], "uid": 0}},
        ):
            path.write_text(json.dumps({"servers": {"docs": {**config, **extra}}}))
            with self.subTest(extra=extra), self.assertRaises(MCPError):
                load_specs(path)
        path.write_text(json.dumps({"servers": {"docs": config}}))
        self.assertEqual(load_specs(path)["docs"].execution.inputs, "none")

    def test_explicit_host_mode_and_http_execution_rejection(self):
        path = self.root / "config.json"
        path.write_text(
            json.dumps(
                {
                    "servers": {
                        "host": {
                            "transport": "stdio",
                            "command": sys.executable,
                            "execution": {"mode": "host"},
                        }
                    }
                }
            )
        )
        self.assertEqual(load_specs(path)["host"].execution.mode, "host")
        path.write_text(
            json.dumps(
                {
                    "servers": {
                        "net": {
                            "transport": "streamable_http",
                            "url": "",
                            "execution": {"mode": "host"},
                        }
                    }
                }
            )
        )
        with self.assertRaises(MCPError):
            load_specs(path)

    def test_connection_reuses_one_process_and_holds_session_lease(self):
        runtime = self.runtime()
        runtime.connect("docs")
        binding = self.binding(runtime)
        first = json.loads(runtime.call(binding, {}))
        second = json.loads(runtime.call(binding, {}))
        self.assertEqual(
            first["structuredContent"]["pid"], second["structuredContent"]["pid"]
        )
        self.assertEqual(len(ProcessBackend.instances), 1)
        self.assertTrue(runtime.leases.held("session"))
        self.assertTrue(first["harness_execution"]["configuration_verified"])
        runtime.disconnect("docs")
        self.assertFalse(runtime.leases.held("session"))

    def test_task_workspaces_get_separate_connections_and_filtered_tool_snapshots(self):
        a = self.root / ".worktrees/a"
        b = self.root / ".worktrees/b"
        a.mkdir(parents=True)
        b.mkdir()
        (a / "input.txt").write_text("alpha")
        (b / "input.txt").write_text("beta")
        runtime = self.runtime()
        runtime.connect("docs", workdir=a, task_id="task_aaaaaaaa")
        runtime.connect("docs", workdir=b, task_id="task_bbbbbbbb")
        self.assertEqual(runtime.snapshot()[0], [])
        one = self.binding(runtime, a, "task_aaaaaaaa", tool="read_text")
        two = self.binding(runtime, b, "task_bbbbbbbb", tool="read_text")
        self.assertIsNot(one.connection, two.connection)
        self.assertEqual(
            json.loads(
                runtime.call(
                    one, {"path": "input.txt"}, workdir=a, task_id="task_aaaaaaaa"
                )
            )["structuredContent"]["text"],
            "alpha",
        )
        with self.assertRaises(MCPError):
            runtime.call(one, {"path": "input.txt"}, workdir=b, task_id="task_bbbbbbbb")
        runtime.disconnect("docs", workdir=a, task_id="task_aaaaaaaa")
        self.assertTrue(runtime.snapshot(workdir=b, task_id="task_bbbbbbbb")[0])

    def test_changed_inputs_reject_old_call_and_explicit_reconnect_replaces_process(
        self,
    ):
        (self.root / "input.txt").write_text("before")
        runtime = self.runtime()
        runtime.connect("docs")
        old = self.binding(runtime)
        (self.root / "input.txt").write_text("after")
        with self.assertRaises(MCPError):
            runtime.call(old, {})
        runtime.connect("docs")
        new = self.binding(runtime)
        self.assertIsNot(old.connection, new.connection)
        self.assertEqual(len(ProcessBackend.instances), 2)
        self.assertFalse(old.connection.owner._leased)

    def test_workspace_identity_and_internal_symlink_are_rejected(self):
        (self.root / "real").mkdir()
        (self.root / "alias").symlink_to(self.root / "real")
        with self.assertRaises((ValueError, OSError)):
            MCPWorkspace.select(self.root, self.root / "alias")
        nested = self.root / "real/a"
        nested.mkdir()
        (self.root / "alias-parent").symlink_to(self.root / "real")
        with self.assertRaises(OSError):
            MCPWorkspace.select(self.root, self.root / "alias-parent/a")

    def test_missing_docker_never_launches_host_command(self):
        self.patcher.stop()
        spec = replace(self.spec, command="/image/server", args=("host-effect",))
        runtime = MCPRuntime({"docs": spec}, workdir=self.root)
        self.addCleanup(runtime.close)
        with self.assertRaises(MCPError):
            runtime.connect("docs")
        self.assertFalse((self.root / "host-effect").exists())
        self.assertFalse(runtime.leases.held("session"))

    def test_cleanup_failure_keeps_close_pending_and_lease_until_repaired(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        ProcessBackend.cleanup_gate = gate
        runtime = self.runtime()
        runtime.connect("docs")
        start = time.monotonic()
        runtime.close(0.03)
        self.assertLess(time.monotonic() - start, 0.3)
        self.assertFalse(runtime.wait_closed(0.03))
        self.assertTrue(runtime.leases.held("session"))
        gate.set()
        self.assertTrue(runtime.wait_closed(5))
        self.assertFalse(runtime.leases.held("session"))

    def test_cancelled_preparation_retains_ownership_until_launch_thread_stops(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        ProcessBackend.launch_gate = gate
        self.spec = replace(self.spec, connect_timeout_seconds=0.1)
        runtime = self.runtime()
        with self.assertRaises(MCPError):
            runtime.connect("docs")
        runtime.close(0.03)
        self.assertTrue(runtime.wait_closed(3))
        self.assertFalse(runtime.leases.held("session"))

    def test_connection_limit_refuses_extra_container_without_affecting_first(self):
        runtime = self.runtime(max_container_connections=1)
        runtime.connect("docs")
        a = self.root / "a"
        a.mkdir()
        with self.assertRaises(MCPError):
            runtime.connect("docs", workdir=a)
        self.assertTrue(runtime.snapshot()[0])
        self.assertEqual(len(ProcessBackend.instances), 1)

    def test_none_inputs_service_is_shared_and_never_snapshots_host_files(self):
        self.spec = replace(self.spec, execution=replace(self.policy, inputs="none"))
        runtime = self.runtime()
        runtime.connect("docs")
        binding = self.binding(runtime)
        (self.root / "new.txt").write_text("changed")
        runtime.call(binding, {})
        self.assertEqual(binding.connection.owner.backend.inputs, {})

    def test_real_loop_connects_calls_and_reconnects_after_file_tool_updates(self):
        runtime = self.runtime()
        (self.root / "input.txt").write_text("original")
        client, _ = fake_client(
            [
                reply(
                    calls=[
                        call("connect", name="connect_mcp", arguments='{"name":"docs"}')
                    ]
                ),
                reply(
                    calls=[
                        call(
                            "read",
                            name="mcp__docs__read_text",
                            arguments='{"path":"input.txt"}',
                        )
                    ]
                ),
                reply(
                    calls=[
                        call(
                            "write",
                            name="write_file",
                            arguments='{"path":"input.txt","content":"updated"}',
                        )
                    ]
                ),
                reply(
                    calls=[
                        call("refresh", name="connect_mcp", arguments='{"name":"docs"}')
                    ]
                ),
                reply(
                    calls=[
                        call(
                            "read-new",
                            name="mcp__docs__read_text",
                            arguments='{"path":"input.txt"}',
                        )
                    ]
                ),
                reply("done"),
            ]
        )
        session = HarnessSession(
            client,
            "fixture",
            self.root,
            mcp_runtime=runtime,
            capabilities=Capabilities({"mcp"}),
            shutdown_timeout=0.03,
        )
        self.addCleanup(lambda: session.wait_closed(5))
        self.addCleanup(session.close)
        self.assertEqual(
            session.run_user("Read the input with the registered service"), "done"
        )
        self.assertIs(runtime.leases, session.workspace.leases)
        self.assertEqual(len(ProcessBackend.instances), 2)
        self.assertEqual((self.root / "input.txt").read_text(), "updated")

    def test_global_tools_must_fit_each_existing_workspace_pool(self):
        shared = replace(
            self.spec, name="shared", execution=replace(self.policy, inputs="none")
        )
        runtime = MCPRuntime(
            {"docs": self.spec, "shared": shared},
            workdir=self.root,
            reserved_names={"reserved_" + str(i) for i in range(125)},
        )
        self.addCleanup(lambda: runtime.wait_closed(5))
        self.addCleanup(runtime.close)
        runtime.connect("docs")
        with self.assertRaisesRegex(MCPError, "128"):
            runtime.connect("shared")
        self.assertEqual(len(runtime.snapshot()[0]), 2)
        self.assertTrue(self.binding(runtime).connection.owner._leased)

    def test_unassigned_member_snapshot_hides_workspace_bindings(self):
        runtime = self.runtime()
        runtime.connect("docs")
        self.assertEqual(runtime.snapshot(allow_workspace=False), ([], {}))
