"""Upstream diagnostics use real local transports and existing error classification."""

import json
import unittest

from homebrew_harness.execution.model_protocol import ModelRequest, ProviderError
from homebrew_harness.execution.model_errors import quota_exhausted
from homebrew_harness.execution.providers.adapter import ProtocolAdapter
from homebrew_harness.execution.providers.common import classify_error
from homebrew_harness.security.data_boundary import register_private_values
from tests.integration.test_model_backends import FIXTURES
from tests.integration.test_native_model_protocols import ProtocolService


class FeedbackTests(unittest.TestCase):
    def adapter(self, protocol, replies, **kwargs):
        server = ProtocolService(replies)
        self.addCleanup(server.close)
        adapter = ProtocolAdapter(protocol, api_key="feedback-credential", base_url=server.url, transport="http", **kwargs)
        self.addCleanup(adapter.close)
        return adapter, server

    def execute(self, adapter, request=None):
        request = request or ModelRequest("fixture", ({"role": "user", "content": "hello"},))
        payload = adapter.prepare(request)
        return adapter.execute(request, payload, 5, lambda: None)

    def test_four_protocols_preserve_actual_server_errors(self):
        for protocol in FIXTURES:
            with self.subTest(protocol=protocol):
                adapter, server = self.adapter(protocol, [(422, {"error": {"code": "bad_option", "type": "validation_error", "message": "Unsupported option: future_flag"}})])
                with self.assertRaises(ProviderError) as caught:
                    self.execute(adapter)
                error = caught.exception
                self.assertEqual(error.status_code, 422)
                self.assertEqual(error.code, "bad_option")
                self.assertEqual(error.message, "Unsupported option: future_flag")
                self.assertIn("future_flag", str(error))
                self.assertEqual(len(server.requests), 1)

    def test_plain_text_error_is_visible_and_existing_redaction_applies(self):
        secret = "registered-feedback-credential"
        register_private_values(secret)
        adapter, _ = self.adapter("openai_responses", [(400, ("Invalid option; " + secret).encode())])
        with self.assertRaises(ProviderError) as caught:
            self.execute(adapter)
        self.assertIn("Invalid option", str(caught.exception))
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn(secret, json.dumps(caught.exception.body))



    def test_sse_failure_retains_service_message(self):
        for protocol in FIXTURES:
            with self.subTest(protocol=protocol):
                event = {"type": "error", "error": {"type": "future_option_error", "message": "Unknown option in streamed request"}}
                if protocol == "openai_responses":
                    event = {"type": "response.failed", "response": {"status": "failed", "error": event["error"]}}
                adapter, _ = self.adapter(protocol, [("sse", [event])], options_json='{"stream":true}')
                with self.assertRaises(ProviderError) as caught:
                    self.execute(adapter)
                self.assertIn("Unknown option", str(caught.exception))

    def test_sdk_status_error_without_json_keeps_service_message(self):
        from homebrew_harness.execution.providers.backends import sdk_available
        if not sdk_available("chat_completions"):
            self.skipTest("Optional OpenAI SDK is unavailable.")
        server = ProtocolService([(400, b"Unsupported option: sdk_future_flag")])
        self.addCleanup(server.close)
        adapter = ProtocolAdapter("chat_completions", api_key="feedback-credential", base_url=server.url, transport="sdk")
        self.addCleanup(adapter.close)
        with self.assertRaises(ProviderError) as caught:
            self.execute(adapter)
        self.assertIn("sdk_future_flag", str(caught.exception))
        self.assertEqual(caught.exception.status_code, 400)

    def test_upstream_codes_keep_quota_recovery_classification(self):
        error = classify_error(400, {"code": "Arrearage", "message": "Account quota exhausted"})
        self.assertEqual(error.code, "Arrearage")
        self.assertTrue(quota_exhausted(error))
        self.assertFalse(error.transient)
