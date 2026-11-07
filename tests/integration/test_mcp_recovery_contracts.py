from tests.support.tools import fixture_invocation
"""MCP calls share active execution and retryable shutdown with other host work."""

import json
import threading
import unittest
from dataclasses import replace
from unittest.mock import patch

from homebrew_harness import HarnessSession
from homebrew_harness.runtime.runtime_control import ClosePending
from homebrew_harness.runtime.runtime_state import RecoveryRequired
from homebrew_harness.storage.durable_runtime import _OPERATION, operation_active
from homebrew_harness.tools.mcp.runtime import ToolPolicy, _RequestActivity
from tests.support.clients import call, fake_client, reply
from tests.tools.mcp import test_container as fixture


class MCPRecoveryContractTests(unittest.TestCase):
    setUp = fixture.MCPContainerTests.setUp
    runtime = fixture.MCPContainerTests.runtime
    binding = fixture.MCPContainerTests.binding

    def test_sent_timeout_blocks_inspection_until_actual_cleanup(self):
        server = self.root / "server.py"
        server.write_text(
            "import asyncio\nfrom mcp.server.fastmcp import FastMCP\ns=FastMCP('fixture',log_level='WARNING')\n@s.tool()\nasync def slow(seconds:float)->str:\n await asyncio.sleep(seconds);return 'done'\ns.run(transport='stdio')\n"
        )
        self.spec = replace(
            self.spec,
            timeout_seconds=0.15,
            execution=replace(self.policy, inputs="none"),
            tools={"slow": ToolPolicy("allow", False)},
        )
        runtime = self.runtime()
        gate = threading.Event()
        fixture.ProcessBackend.cleanup_gate = gate
        self.addCleanup(gate.set)
        client, _ = fake_client(
            [
                reply(
                    calls=[
                        call("connect", name="connect_mcp", arguments='{"name":"docs"}')
                    ]
                ),
                reply(
                    calls=[
                        call("slow", name="mcp__docs__slow", arguments='{"seconds":30}')
                    ]
                ),
            ]
        )
        session = HarnessSession(
            client, "fixture", self.root, mcp_runtime=runtime, shutdown_timeout=0.02
        )
        try:
            with (
                patch.object(fixture, "SERVER", server),
                self.assertRaises(RecoveryRequired),
            ):
                session.run_user("Call slow service")
            row = session.recovery_operations()[0]
            self.assertTrue(row["active"])
            self.assertTrue(next(iter(runtime._actors)).owner._leased)
            with self.assertRaises(RecoveryRequired):
                session.inspect_operation(row["operation_id"], row["revision"])
            gate.set()
            runtime.close(0.02)
            self.assertTrue(runtime.wait_closed(5))
            row = session.recovery_operations()[0]
            self.assertFalse(row["active"])
            session.inspect_operation(row["operation_id"], row["revision"])
            self.assertEqual(session.recovery_operations(), [])
        finally:
            gate.set()
            session.close()
            self.assertTrue(session.wait_closed(5))

    def test_queued_cancellation_cannot_start_or_hold_another_calls_activity(self):
        token = _OPERATION.set((str(self.root), "queued"))
        try:
            ticket = _RequestActivity()
        finally:
            _OPERATION.reset(token)
        self.assertTrue(operation_active(self.root, "queued"))
        ticket.abandon()
        self.assertFalse(ticket.start())
        self.assertFalse(operation_active(self.root, "queued"))
        ticket.finish()

    def test_sent_activity_survives_caller_abandon_until_owner_finishes(self):
        token = _OPERATION.set((str(self.root), "sent"))
        try:
            ticket = _RequestActivity()
        finally:
            _OPERATION.reset(token)
        self.assertTrue(ticket.start())
        ticket.abandon()
        self.assertTrue(operation_active(self.root, "sent"))
        ticket.finish()
        ticket.finish()
        self.assertFalse(operation_active(self.root, "sent"))

    def test_close_thread_launch_failure_is_retryable(self):
        runtime = self.runtime()
        runtime.connect("docs")
        original = threading.Thread.start

        def fail(thread):
            if thread.name == "harness-shutdown":
                raise RuntimeError("fixture before acceptance")
            return original(thread)

        with (
            patch.object(threading.Thread, "start", fail),
            self.assertRaises(ClosePending),
        ):
            runtime.close(0.01)
        self.assertTrue(runtime.shutdown_status()["retryable"])
        self.assertFalse(runtime.wait_closed(0.01))
        runtime.close(0.01)
        self.assertTrue(runtime.wait_closed(5))
        self.assertFalse(runtime.leases.held("session"))

    def test_close_thread_post_acceptance_error_does_not_duplicate_shutdown(self):
        runtime = self.runtime()
        runtime.connect("docs")
        original = threading.Thread.start
        started = []

        def interrupt(thread):
            result = original(thread)
            if thread.name == "harness-shutdown":
                started.append(thread)
                raise RuntimeError("fixture accepted")
            return result

        with patch.object(threading.Thread, "start", interrupt):
            runtime.close(0.01)
        runtime.close(0.01)
        self.assertTrue(runtime.wait_closed(5))
        self.assertEqual(len(started), 1)

    def test_dispatch_scans_workspace_once_and_after_permission(self):
        import homebrew_harness.execution.mcp_runner as runner

        original = runner.bash_snapshot
        client, _ = fake_client(
            [
                reply(
                    calls=[
                        call("connect", name="connect_mcp", arguments='{"name":"docs"}')
                    ]
                ),
                reply(
                    calls=[
                        call("version", name="mcp__docs__get_version", arguments="{}")
                    ]
                ),
                reply("done"),
            ]
        )
        runtime = self.runtime()
        session = HarnessSession(client, "fixture", self.root, mcp_runtime=runtime)
        try:
            with patch.object(runner, "bash_snapshot", wraps=original) as scan:
                session.run_user("Read fixture")
            # ProcessBackend itself snapshots through its own imported helper.
            self.assertEqual(scan.call_count, 1)
        finally:
            session.close()
            self.assertTrue(session.wait_closed(5))

    def test_input_change_during_approval_is_rejected_at_send(self):
        from homebrew_harness.runtime.hooks import create_hooks
        from homebrew_harness.security.permissions import PermissionDecision
        from homebrew_harness.tools.dispatch import execute_tool

        self.spec = replace(
            self.spec, tools={"get_version": ToolPolicy("confirm", True)}
        )
        runtime = self.runtime()
        runtime.connect("docs")
        request = (
            reply(
                calls=[call("version", name="mcp__docs__get_version", arguments="{}")]
            )
            .choices[0]
            .message.tool_calls[0]
        )

        def approve(*_args, **_kwargs):
            (self.root / "late.txt").write_text("new input")
            return PermissionDecision("allow", "fixture")

        with patch("homebrew_harness.security.permissions.ask_user", approve):
            output = json.loads(
                execute_tool(
                    fixture_invocation(request),
                    self.root,
                    hooks=create_hooks(),
                    handlers={},
                    mcp_runtime=runtime,
                )
            )
        self.assertIn("snapshot changed", output["error"])

    def test_host_status_and_connection_close_do_not_call_model(self):
        from homebrew_harness.execution.model_gateway import Deadline
        from homebrew_harness.runtime.host_commands import HostCommand

        runtime = self.runtime()
        client, _ = fake_client([])
        session = HarnessSession(client, "fixture", self.root, mcp_runtime=runtime)
        try:
            session.run_user("/usage")
            runtime.connect("docs")
            session.gateway._state.deadline = Deadline.after(0)
            rows = json.loads(session.run_user("/mcp"))["connections"]
            self.assertEqual(len(rows), 1)
            output = json.loads(session.run_user("/mcp close " + rows[0]["id"]))
            self.assertIn(output["state"], {"closing", "closed"})
            client.chat.completions.create.assert_not_called()
            self.assertTrue(
                HostCommand.parse("/mcp close " + rows[0]["id"]).allowed_during_recovery
            )
            with self.assertRaises(ValueError):
                HostCommand.parse("/mcp close arbitrary")
        finally:
            session.close()
            self.assertTrue(session.wait_closed(5))
