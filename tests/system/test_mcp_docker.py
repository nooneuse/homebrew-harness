"""Opt-in actual Docker plus MCP SDK tests; no paid model calls."""

import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from homebrew_harness.execution.mcp_policy import MCPExecutionPolicy
from homebrew_harness.execution.mcp_runner import MCPContainerBackend
from homebrew_harness.tools.mcp.runtime import (
    MCPError,
    MCPRuntime,
    ServerSpec,
    ToolPolicy,
)

IMAGE = os.getenv("HARNESS_MCP_DOCKER_IMAGE", "")
CLI = os.getenv("HARNESS_DOCKER_CLI", "/usr/bin/docker")


@unittest.skipUnless(
    IMAGE and __import__("sys").platform == "linux",
    "requires explicit MCP image and Linux Docker controller",
)
class MCPDockerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="harness-mcp-case-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.cwd = self.root / "workspace"
        self.cwd.mkdir()
        self.policy = MCPExecutionPolicy(
            mode="container", cli=CLI, image=IMAGE, inputs="workspace", memory_mb=192
        )
        self.spec = ServerSpec(
            "probe",
            "stdio",
            command="/usr/local/bin/python",
            args=("/opt/mcp_container_server.py",),
            execution=self.policy,
            tools={
                name: ToolPolicy("allow", True)
                for name in (
                    "identity",
                    "probe",
                    "read_text",
                    "write_workspace",
                    "delay",
                    "huge_output",
                    "spawn_detached",
                )
            },
        )
        self.runtimes = []
        for capture in (redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            capture.__enter__()
            self.addCleanup(capture.__exit__, None, None, None)

    def tearDown(self):
        for runtime in self.runtimes:
            runtime.close(0.5)
            self.assertTrue(runtime.wait_closed(8))
            self.assertFalse(runtime.leases.held("session"))

    def runtime(self, spec=None):
        runtime = MCPRuntime({"probe": spec or self.spec}, workdir=self.cwd)
        self.runtimes.append(runtime)
        return runtime

    def invoke(self, runtime, name, args=None, **scope):
        binding = runtime.snapshot(**scope)[1]["mcp__probe__" + name]
        return json.loads(runtime.call(binding, args or {}, **scope))

    def test_multiple_rpc_calls_reuse_verified_container_then_close(self):
        runtime = self.runtime()
        connected = runtime.connect("probe")
        self.assertEqual(connected["execution"]["mode"], "container")
        self.assertTrue(connected["execution"]["configuration_verified"])
        first = self.invoke(runtime, "identity")
        second = self.invoke(runtime, "identity")
        self.assertEqual(
            first["structuredContent"]["pid"], second["structuredContent"]["pid"]
        )
        connection = next(iter(runtime._connections.values()))
        owner = connection.owner
        names = [r[0] for r in owner.backend.active.values()]
        runtime.close(0.5)
        self.assertTrue(runtime.wait_closed(8))
        self.assertTrue(owner.backend.wait_closed(0))
        self.assertTrue(names)

    def test_host_files_credentials_network_and_privileges_are_isolated(self):
        (self.root / "outside").write_text("host-only")
        (self.cwd / "input.txt").write_text("current input")
        (self.cwd / ".env").write_text("private configuration")
        (self.cwd / ".harness").mkdir()
        (self.cwd / ".harness/private").write_text("runtime data")
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        self.addCleanup(listener.close)
        os.environ["HARNESS_ENV_CANARY"] = "fixture"
        self.addCleanup(os.environ.pop, "HARNESS_ENV_CANARY", None)
        runtime = self.runtime()
        runtime.connect("probe")
        response = self.invoke(
            runtime,
            "probe",
            {
                "paths": [
                    str(self.root / "outside"),
                    "/workspace/.env",
                    "/workspace/.harness/private",
                    "/var/run/docker.sock",
                ],
                "port": listener.getsockname()[1],
            },
        )
        self.assertTrue(all(response["structuredContent"]["blocked"]), response)
        self.assertTrue(
            all(v for k, v in response["structuredContent"].items() if k != "blocked"),
            response,
        )
        self.assertEqual(
            self.invoke(runtime, "read_text", {"path": "input.txt"})[
                "structuredContent"
            ]["text"],
            "current input",
        )

    def test_workspace_writes_are_refused_and_host_files_unchanged(self):
        (self.cwd / "input.txt").write_text("original")
        runtime = self.runtime()
        runtime.connect("probe")
        self.assertTrue(
            self.invoke(runtime, "write_workspace", {"path": "input.txt"})["isError"]
        )
        self.assertEqual((self.cwd / "input.txt").read_text(), "original")

    def test_empty_input_service_has_no_workspace_files(self):
        (self.cwd / "input.txt").write_text("host-only")
        runtime = self.runtime(
            replace(self.spec, execution=replace(self.policy, inputs="none"))
        )
        runtime.connect("probe")
        self.assertTrue(
            self.invoke(runtime, "read_text", {"path": "input.txt"})["isError"]
        )

    def test_changed_input_rejects_call_then_explicit_reconnect_reads_new_copy(self):
        (self.cwd / "input.txt").write_text("before")
        runtime = self.runtime()
        runtime.connect("probe")
        old = runtime.snapshot()[1]["mcp__probe__read_text"]
        (self.cwd / "input.txt").write_text("after")
        with self.assertRaisesRegex(MCPError, "snapshot changed"):
            runtime.call(old, {"path": "input.txt"})
        runtime.connect("probe")
        self.assertEqual(
            self.invoke(runtime, "read_text", {"path": "input.txt"})[
                "structuredContent"
            ]["text"],
            "after",
        )
        self.assertTrue(old.connection.owner.backend.wait_closed(0))

    def test_task_workspaces_have_independent_containers(self):
        a = self.cwd / ".worktrees/a"
        b = self.cwd / ".worktrees/b"
        a.mkdir(parents=True)
        b.mkdir()
        (a / "input.txt").write_text("alpha")
        (b / "input.txt").write_text("beta")
        runtime = self.runtime()
        runtime.connect("probe", workdir=a, task_id="task_aaaaaaaa")
        runtime.connect("probe", workdir=b, task_id="task_bbbbbbbb")
        first = self.invoke(runtime, "identity", workdir=a, task_id="task_aaaaaaaa")
        second = self.invoke(runtime, "identity", workdir=b, task_id="task_bbbbbbbb")
        self.assertEqual(runtime.snapshot()[0], [])
        one = runtime.snapshot(workdir=a, task_id="task_aaaaaaaa")[1][
            "mcp__probe__read_text"
        ]
        with self.assertRaises(MCPError):
            runtime.call(one, {"path": "input.txt"}, workdir=b, task_id="task_bbbbbbbb")
        self.assertEqual(
            self.invoke(
                runtime,
                "read_text",
                {"path": "input.txt"},
                workdir=b,
                task_id="task_bbbbbbbb",
            )["structuredContent"]["text"],
            "beta",
        )

    def test_request_timeout_marks_unknown_and_removes_container(self):
        runtime = self.runtime(replace(self.spec, timeout_seconds=0.3))
        runtime.connect("probe")
        with self.assertRaises(MCPError) as error:
            self.invoke(runtime, "delay", {"seconds": 30})
        self.assertEqual(error.exception.outcome, "unknown")
        runtime.close(0.1)
        self.assertTrue(runtime.wait_closed(8))

    def test_large_response_closes_transport_and_container(self):
        runtime = self.runtime()
        runtime.connect("probe")
        with self.assertRaises(MCPError):
            self.invoke(runtime, "huge_output")
        runtime.close(0.1)
        self.assertTrue(runtime.wait_closed(8))

    def test_detached_descendant_is_removed_with_owned_container(self):
        runtime = self.runtime()
        runtime.connect("probe")
        self.assertGreater(
            self.invoke(runtime, "spawn_detached")["structuredContent"]["child_pid"], 1
        )
        owner = next(iter(runtime._connections.values())).owner
        runtime.close(0.1)
        self.assertTrue(runtime.wait_closed(8))
        self.assertTrue(owner.backend.wait_closed(0))

    def test_failed_removal_retains_pending_and_lease_until_repaired(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        original = MCPContainerBackend._remove

        def remove(backend, *args, **kwargs):
            return original(backend, *args, **kwargs) if gate.is_set() else False

        with patch.object(MCPContainerBackend, "_remove", remove):
            runtime = self.runtime()
            runtime.connect("probe")
            runtime.close(0.03)
            self.assertFalse(runtime.wait_closed(0.03))
            self.assertTrue(runtime.leases.held("session"))
            gate.set()
            self.assertTrue(runtime.wait_closed(8))
            self.assertFalse(runtime.leases.held("session"))

    def test_cancel_during_create_is_owned_until_container_removed(self):
        created = threading.Event()
        original = MCPContainerBackend._control

        def control(backend, argv, timeout, cancel_events=()):
            result = original(backend, argv, timeout, cancel_events)
            if "create" in argv:
                created.set()
                while not any(event.is_set() for event in cancel_events):
                    if not created.wait(0.01):
                        break
                    time.sleep(0.01)
            return result

        runtime = self.runtime()
        errors = []
        with patch.object(MCPContainerBackend, "_control", control):

            def connect():
                try:
                    runtime.connect("probe")
                except MCPError as error:
                    errors.append(error)

            thread = threading.Thread(target=connect)
            thread.start()
            self.assertTrue(created.wait(8))
            runtime.close(0.03)
            thread.join(8)
            self.assertFalse(thread.is_alive())
            self.assertTrue(runtime.wait_closed(8))
            self.assertTrue(errors)

    def test_readonly_executable_inputs_and_mode_change_invalidate_binding(self):
        script = self.cwd / "run.sh"
        script.write_text("#!/bin/sh\nprintf mcp-script")
        script.chmod(0o6755)
        runtime = self.runtime()
        runtime.connect("probe")
        result = self.invoke(runtime, "run_script")["structuredContent"]
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["stdout"], "mcp-script")
        self.assertEqual(result["mode"], 0o555)
        binding = runtime.snapshot()[1]["mcp__probe__identity"]
        script.chmod(0o644)
        with self.assertRaisesRegex(MCPError, "snapshot changed"):
            runtime.call(binding, {})

    def test_timed_out_call_remains_active_until_container_cleanup_finishes(self):
        from homebrew_harness.storage.durable_runtime import (
            _OPERATION,
            operation_active,
        )

        gate = threading.Event()
        self.addCleanup(gate.set)
        original = MCPContainerBackend._remove

        def remove(backend, *args, **kwargs):
            return original(backend, *args, **kwargs) if gate.is_set() else False

        runtime = self.runtime(replace(self.spec, timeout_seconds=0.2))
        runtime.connect("probe")
        with patch.object(MCPContainerBackend, "_remove", remove):
            token = _OPERATION.set((str(self.cwd), "fixture-call"))
            try:
                with self.assertRaises(MCPError):
                    self.invoke(runtime, "delay", {"seconds": 30})
            finally:
                _OPERATION.reset(token)
            self.assertTrue(operation_active(self.cwd, "fixture-call"))
            self.assertTrue(runtime.leases.held("session"))
            gate.set()
            runtime.close(0.03)
            self.assertTrue(runtime.wait_closed(8))
        self.assertFalse(operation_active(self.cwd, "fixture-call"))
