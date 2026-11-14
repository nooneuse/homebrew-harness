"""Observable limits survive model, execution and persistence boundaries."""

import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from types import SimpleNamespace

from homebrew_harness.execution.model_gateway import ModelGateway, GatewayLimits, ModelDeadlineExceeded
from homebrew_harness.execution.model_protocol import ModelProtocolError
from homebrew_harness.execution.providers import chat_completions, responses, anthropic, dashscope
from homebrew_harness.orchestration.workflow_runtime import ModelWorkflowRunner, WorkflowLimits, WorkflowRegistry, WorkflowRuntime, WorkflowSpec
from homebrew_harness.runtime.errors import AgentError, boundary_error, error_info
from homebrew_harness.runtime.loop import agent_loop
from homebrew_harness.security.data_boundary import register_private_values
from homebrew_harness.security.safe_files import SafeFile
from homebrew_harness.tools.dispatch import tool_error
from tests.support.clients import call, fake_client, reply, model_binding
from tests.services import test_maintenance as service_fixture


class BoundaryFeedback(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_native_provider_limit_reasons_are_kept_for_all_protocols(self):
        choice = {"finish_reason": "length", "message": {"content": "partial"}}
        fixtures = (
            (chat_completions.decode, {"choices": [choice]}, "length"),
            (dashscope.decode, {"output": {"choices": [choice]}}, "length"),
            (responses.decode, {"status": "incomplete", "output": [], "incomplete_details": {"reason": "max_output_tokens"}}, "incomplete"),
            (anthropic.decode, {"stop_reason": "max_tokens", "content": [{"type": "text", "text": "partial"}]}, "max_tokens"),
        )
        for decode, payload, reason in fixtures:
            with self.subTest(reason=reason):
                info = error_info(decode(payload).failure())
                self.assertEqual(info["code"], "model_output_limit")
                self.assertEqual(info["details"]["provider_finish_reason"], reason)

    def test_unfinished_stream_is_not_reported_as_an_output_limit(self):
        with self.assertRaises(ModelProtocolError) as raised:
            chat_completions.streamed([{"choices": [{"delta": {"content": "partial"}}]}])
        self.assertEqual(error_info(raised.exception)["code"], "model_stream_incomplete")

    def test_partial_model_tool_output_is_not_executed_and_reports_allowance(self):
        response = reply(calls=[call("partial", name="write_file", arguments='{"path":"unexpected","content":"no"}')], finish_reason="length")
        response.usage = SimpleNamespace(prompt_tokens=1, completion_tokens=8192, total_tokens=8193,
                                        completion_tokens_details=SimpleNamespace(reasoning_tokens=7000))
        client, _ = fake_client([response])
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), self.assertRaises(AgentError) as raised:
            agent_loop(client, "fixture", [{"role": "user", "content": "Do the task"}], self.root,
                           max_turns=1, worker_output_tokens=8192)
        info = error_info(raised.exception)
        self.assertEqual(info["code"], "model_output_limit")
        self.assertEqual(info["details"]["max_output_tokens"], 8192)
        self.assertEqual(info["details"]["output_tokens"], 8192)
        self.assertEqual(info["details"]["reasoning_tokens"], 7000)
        self.assertFalse((self.root / "unexpected").exists())

    def test_queue_timeout_reports_the_effective_limit_without_sending(self):
        entered, release = threading.Event(), threading.Event()
        client, _ = fake_client([])
        def hold(**_kwargs):
            entered.set()
            release.wait(2)
            return reply("ready")
        client.chat.completions.create.side_effect = hold
        gateway = ModelGateway(client, GatewayLimits(concurrency=1, request_timeout=1), workdir=self.root)
        thread = threading.Thread(target=lambda: gateway.request(model="fixture", messages=[]))
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            with self.assertRaises(ModelDeadlineExceeded) as raised:
                gateway.request(model="fixture", messages=[], timeout=0.05)
            info = error_info(raised.exception)
            self.assertEqual(info["code"], "request_timeout")
            self.assertEqual(info["details"]["phase"], "queue")
            self.assertEqual(info["details"]["request_timeout_seconds"], 0.05)
            self.assertLessEqual(info["details"]["effective_timeout_seconds"], 0.05 + 0.001)
            self.assertFalse(info["details"]["sent"])
            self.assertTrue(info["details"]["settled"])
            self.assertEqual(client.chat.completions.create.call_count, 1)
        finally:
            release.set()
            thread.join(2)
            gateway.close()
            gateway.wait_idle(2)

    def test_file_limit_reaches_tool_feedback_with_measured_bytes(self):
        (self.root / "large").write_bytes(b"12345")
        with self.assertRaises(ValueError) as raised:
            with SafeFile(self.root, "large") as file:
                file.snapshot(max_bytes=4)
        output = json.loads(tool_error(raised.exception))
        self.assertEqual(output["error_info"]["code"], "file_snapshot_bytes_limit")
        self.assertEqual(output["error_info"]["details"]["limit_value"], 4)
        self.assertEqual(output["error_info"]["details"]["observed_value"], 5)

    def test_workflow_preserves_output_limit_after_its_one_correction(self):
        client, _ = fake_client([reply("partial", finish_reason="length")] * 2)
        async def script(ctx, _args):
            return await ctx.agent("Return the answer")
        runtime = WorkflowRuntime(self.root, ModelWorkflowRunner(model_binding(client, "fixture")),
                                  WorkflowRegistry([WorkflowSpec("probe", "probe", script, {"type": "object"})]),
                                  limits=WorkflowLimits(max_output_tokens=8192))
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                result = runtime.run("probe", {})
            self.assertEqual(result["task"]["status"], "failed")
            self.assertEqual(result["result"]["error_info"]["code"], "model_output_limit")
            self.assertEqual(result["result"]["error_info"]["details"]["max_output_tokens"], 8192)
            self.assertEqual(client.chat.completions.create.call_count, 2)
        finally:
            runtime.close()

    def test_mcp_operation_timeout_reports_its_window(self):
        import asyncio
        from homebrew_harness.tools.mcp.runtime import MCPRuntime, MCPError

        runtime = MCPRuntime({}, workdir=self.root)
        runtime._ensure_loop()
        try:
            with self.assertRaises(MCPError) as raised:
                runtime._submit(asyncio.sleep(1), 0.03)
            info = error_info(raised.exception)
            self.assertEqual(info["code"], "mcp_operation_timeout")
            self.assertEqual(info["details"]["limit_value"], 0.03)
            self.assertLessEqual(info["details"]["effective_timeout_seconds"], 0.03)
            self.assertIn("unknown", info["message"])
        finally:
            runtime.close()
            self.assertTrue(runtime.wait_closed(5))

    def test_mcp_cancellation_is_not_a_timeout(self):
        import asyncio
        from unittest.mock import patch
        from homebrew_harness.execution.model_gateway import ModelCancelled
        from homebrew_harness.tools.mcp.runtime import MCPRuntime, MCPError

        runtime = MCPRuntime({}, workdir=self.root)
        runtime._ensure_loop()
        try:
            with patch("homebrew_harness.tools.mcp.runtime.wait_future", side_effect=ModelCancelled("Cancelled")):
                with self.assertRaises(MCPError) as raised:
                    runtime._submit(asyncio.sleep(1), 1)
            self.assertEqual(error_info(raised.exception)["code"], "mcp_cancelled")
        finally:
            runtime.close()
            self.assertTrue(runtime.wait_closed(5))

    def test_diagnostics_redact_secrets_and_omit_reasoning_payloads(self):
        secret = "diagnostic-private-fixture"
        register_private_values(secret)
        error = boundary_error(AgentError, "model_output_limit", "Provider failed: " + secret,
                               max_output_tokens=8192, reasoning_content="PRIVATE_THOUGHTS", body={"secret": secret})
        serialized = json.dumps(error_info(error))
        self.assertNotIn(secret, serialized)
        self.assertNotIn("PRIVATE_THOUGHTS", serialized)
        self.assertNotIn('"body"', serialized)
        self.assertIn("8192", serialized)


class ServiceFeedback(unittest.TestCase):
    setUp = service_fixture.MaintenanceTests.setUp
    start = service_fixture.MaintenanceTests.start

    def test_failed_job_and_target_expose_the_same_limit(self):
        service = self.start(job_attempts=1)
        service.pause("all")
        receipt = service.converse("Review the task", request_id="limit-feedback")
        store = service.runtime.maintenance.store
        job = store.claim_job()
        self.assertEqual(job["id"], receipt["job_id"])
        error = boundary_error(AgentError, "model_output_limit", "Provider reports an output limit.",
                               max_output_tokens=8192, provider_finish_reason="length", output_tokens=8192)
        store.finish(job, error=error)
        result = service.job(job["id"])
        self.assertEqual(result["error_info"]["code"], "model_output_limit")
        self.assertIn("8192", result["error_detail"])
        self.assertEqual(service.conversation(receipt["id"])["error_info"]["code"], "model_output_limit")
        listed = next(item for item in service.records("jobs")["items"] if item["id"] == job["id"])
        self.assertEqual(listed["error_code"], "model_output_limit")
