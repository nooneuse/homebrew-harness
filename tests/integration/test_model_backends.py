"""Real optional SDK clients and HTTP share one protocol and recovery contract."""

import asyncio
import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from homebrew_harness import HarnessSession, ModelRegistry, ModelSpec
from homebrew_harness.execution.model_protocol import (
    ModelRequest,
    ModelProtocolError,
    ProviderError,
)
from homebrew_harness.execution.providers.adapter import ProtocolAdapter
from homebrew_harness.execution.providers.backends import (
    backend_scope,
    sdk_available,
    session_backend_bindings,
)
from tests.integration.test_native_model_protocols import (
    ProtocolService,
    responses,
    anthropic,
    generation,
)

PROTOCOLS = (
    "chat_completions",
    "openai_responses",
    "anthropic_messages",
    "dashscope_generation",
)


def chat(text=None, *, tool=None):
    message = {
        "role": "assistant",
        "content": text,
        "reasoning_content": "keep-reasoning",
    }
    if tool:
        message["tool_calls"] = [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "write_file", "arguments": json.dumps(tool)},
            }
        ]
    return {
        "id": "chat-1",
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if tool else "stop",
            }
        ],
        "usage": {"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50},
    }


FIXTURES = dict(zip(PROTOCOLS, (chat, responses, anthropic, generation)))


def stream(protocol):
    if protocol == "chat_completions":
        return [
            {
                "id": "chat-1",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "content": "done",
                            "reasoning_content": "keep-reasoning",
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 40,
                    "completion_tokens": 10,
                    "total_tokens": 50,
                },
            },
        ]
    if protocol == "openai_responses":
        return [{"type": "response.completed", "response": responses("done")}]
    if protocol == "anthropic_messages":
        return [
            {
                "type": "message_start",
                "message": {
                    "id": "message-1",
                    "usage": {"input_tokens": 40, "output_tokens": 0},
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "done"},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 10},
            },
            {"type": "message_stop"},
        ]
    return [generation("done")]


class BackendTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        for capture in (redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            capture.__enter__()
            self.addCleanup(capture.__exit__, None, None, None)

    def service(self, values):
        server = ProtocolService(values)
        self.addCleanup(server.close)
        return server

    def adapter(self, protocol, server, transport="http", **kwargs):
        result = ProtocolAdapter(
            protocol,
            api_key="synthetic-credential",
            base_url=server.url,
            transport=transport,
            **kwargs,
        )
        self.addCleanup(result.close)
        return result

    def test_http_all_protocols_never_probe_or_import_sdk(self):
        for protocol in PROTOCOLS:
            with (
                self.subTest(protocol=protocol),
                patch(
                    "homebrew_harness.execution.providers.backends.sdk_available",
                    side_effect=AssertionError("SDK probe"),
                ),
            ):
                server = self.service([FIXTURES[protocol]("done")])
                adapter = self.adapter(protocol, server)
                request = ModelRequest(
                    "fixture", ({"role": "user", "content": "hello"},)
                )
                result = adapter.execute(
                    request, adapter.prepare(request), 3, lambda: None
                )
                self.assertEqual(result.require_text(), "done")
                self.assertEqual(adapter.transport_identity["backend"], "http")
                self.assertEqual(len(server.requests), 1)

    def test_auto_missing_sdk_and_explicit_sdk_are_decided_before_send(self):
        server = self.service([])
        with patch(
            "homebrew_harness.execution.providers.backends.sdk_available",
            return_value=(None, "sdk_not_installed"),
        ):
            adapter = self.adapter(PROTOCOLS[0], server, "auto")
            self.assertEqual(adapter.transport_identity["backend"], "http")
            with self.assertRaisesRegex(ValueError, "sdk_not_installed"):
                self.adapter(PROTOCOLS[0], server, "sdk")
        self.assertFalse(server.requests)

    def test_broken_sdk_import_is_not_treated_as_missing(self):
        with (
            patch("importlib.util.find_spec", return_value=object()),
            patch("importlib.metadata.version", return_value="2.54.0"),
            patch(
                "importlib.import_module", side_effect=ImportError("broken dependency")
            ),
        ):
            with self.assertRaisesRegex(ImportError, "broken dependency"):
                sdk_available(PROTOCOLS[0])

    def test_saved_http_stays_http_after_install_and_lazy_thread_selection(self):
        server = self.service([])
        spec = ModelSpec(
            "lead",
            "fixture",
            "synthetic-credential",
            server.url,
            connection_id="shared",
        )
        with patch(
            "homebrew_harness.execution.providers.backends.sdk_available",
            return_value=(None, "sdk_not_installed"),
        ):
            first = ModelRegistry([spec])
            binding = first.select("lead")
            saved = binding.identity()
            first.close()
        with backend_scope(saved):
            second = ModelRegistry([spec])
        self.addCleanup(second.close)
        rows = []
        thread = threading.Thread(
            target=lambda: rows.append(second.select("lead").identity())
        )
        thread.start()
        thread.join(5)
        self.assertEqual(rows, [saved])
        self.assertFalse(server.requests)

    def test_http_session_resume_keeps_original_auto_binding(self):
        server = self.service([chat("first"), chat("second")])
        spec = ModelSpec("lead", "fixture", "synthetic-credential", server.url)
        with patch(
            "homebrew_harness.execution.providers.backends.sdk_available",
            return_value=(None, "sdk_not_installed"),
        ):
            session = HarnessSession(
                None,
                "",
                self.root,
                model_registry=ModelRegistry([spec], lead_default="lead"),
            )
            self.assertEqual(session.run_user("First task"), "first")
            sid = session.session_id
            saved = session.lead_binding.identity()
            session.close()
            self.assertTrue(session.wait_closed(5))
        registry = ModelRegistry([spec], lead_default="lead")
        resumed = HarnessSession(
            None, "", self.root, model_registry=registry, resume_session=sid
        )
        try:
            self.assertEqual(resumed.run_user("Next task"), "second")
            self.assertEqual(resumed.lead_binding.identity(), saved)
        finally:
            resumed.close()
            self.assertTrue(resumed.wait_closed(5))
        self.assertEqual(len(server.requests), 2)

    def test_real_sdk_and_http_wire_tool_replay_and_usage_are_equivalent(self):
        for protocol in PROTOCOLS:
            if sdk_available(protocol)[0] is None:
                continue
            wires = []
            for transport in ("http", "sdk"):
                with self.subTest(protocol=protocol, transport=transport):
                    root = self.root / (protocol + "-" + transport)
                    root.mkdir()
                    factory = FIXTURES[protocol]
                    server = self.service(
                        [
                            factory(tool={"path": "output.txt", "content": "output"}),
                            factory("done"),
                        ]
                    )
                    spec = ModelSpec(
                        "lead",
                        "fixture",
                        "synthetic-credential",
                        server.url,
                        protocol=protocol,
                        transport=transport,
                    )
                    session = HarnessSession(
                        None,
                        "",
                        root,
                        model_registry=ModelRegistry([spec], lead_default="lead"),
                    )
                    try:
                        self.assertEqual(
                            session.run_user("Write output and report"), "done"
                        )
                        self.assertEqual((root / "output.txt").read_text(), "output")
                        self.assertEqual(session.gateway.summary()["calls"], 2)
                        self.assertEqual(
                            session.lead_binding.transport["backend"], transport
                        )
                    finally:
                        session.close()
                        self.assertTrue(session.wait_closed(5))
                    wires.append(
                        json.loads(
                            json.dumps(server.requests).replace(
                                str(root), "<workspace>"
                            )
                        )
                    )
            self.assertEqual(wires[0], wires[1], protocol)

    def test_real_sdk_and_http_stream_completion_and_usage_are_equivalent(self):
        for protocol in PROTOCOLS:
            values = []
            for transport in ("http", "sdk"):
                if transport == "sdk" and sdk_available(protocol)[0] is None:
                    continue
                with self.subTest(protocol=protocol, transport=transport):
                    server = self.service([("stream", stream(protocol))])
                    adapter = self.adapter(
                        protocol,
                        server,
                        transport,
                        options_json=json.dumps({"stream": True}),
                    )
                    request = ModelRequest(
                        "fixture", ({"role": "user", "content": "hello"},)
                    )
                    response = adapter.execute(
                        request, adapter.prepare(request), 3, lambda: None
                    )
                    self.assertEqual(response.require_text(), "done")
                    values.append(response)
            if len(values) == 2:
                self.assertEqual(values[0], values[1])

    def test_saved_sdk_requires_package_and_exact_version(self):
        protocol = PROTOCOLS[0]
        if sdk_available(protocol)[0] is None:
            self.skipTest("Optional OpenAI SDK unavailable")
        server = self.service([])
        adapter = self.adapter(protocol, server, "sdk")
        saved = adapter.transport_identity
        with patch(
            "homebrew_harness.execution.providers.backends.sdk_available",
            return_value=(None, "sdk_not_installed"),
        ):
            with self.assertRaisesRegex(ValueError, "sdk_not_installed"):
                self.adapter(protocol, server, "auto", expected_transport=saved)
        with self.assertRaisesRegex(ValueError, "saved model backend"):
            self.adapter(
                protocol,
                server,
                "auto",
                expected_transport={**saved, "sdk_version": "different"},
            )
        self.assertFalse(server.requests)

    def test_received_error_or_truncated_stream_never_switches_backend(self):
        for protocol in PROTOCOLS:
            for transport in ("http", "sdk"):
                if transport == "sdk" and sdk_available(protocol)[0] is None:
                    continue
                with self.subTest(protocol=protocol, transport=transport):
                    server = self.service([{"error": {"type": "rate_limit_error"}}])
                    adapter = self.adapter(protocol, server, transport)
                    request = ModelRequest(
                        "fixture", ({"role": "user", "content": "hello"},)
                    )
                    with self.assertRaises(ProviderError):
                        adapter.execute(
                            request, adapter.prepare(request), 3, lambda: None
                        )
                    self.assertEqual(len(server.requests), 1)
                    server = self.service([("stream", [])])
                    adapter = self.adapter(
                        protocol,
                        server,
                        transport,
                        options_json=json.dumps({"stream": True}),
                    )
                    with self.assertRaises(ModelProtocolError):
                        adapter.execute(
                            request, adapter.prepare(request), 3, lambda: None
                        )
                    self.assertEqual(len(server.requests), 1)

    def test_environment_headers_do_not_enter_an_auto_sdk_connection(self):
        server = self.service([])
        for protocol, name in (
            (PROTOCOLS[0], "OPENAI_CUSTOM_HEADERS"),
            (PROTOCOLS[2], "ANTHROPIC_CUSTOM_HEADERS"),
        ):
            with patch.dict("os.environ", {name: "Authorization: other-account"}):
                adapter = self.adapter(protocol, server, "auto")
                self.assertEqual(adapter.transport_identity["backend"], "http")
                with self.assertRaisesRegex(
                    ValueError, "headers_not_isolated|sdk_not_installed"
                ):
                    self.adapter(protocol, server, "sdk")
        self.assertFalse(server.requests)

    def test_http_status_classification_and_bounds_match_real_sdk(self):
        for protocol in PROTOCOLS:
            for transport in ("http", "sdk"):
                if transport == "sdk" and sdk_available(protocol)[0] is None:
                    continue
                with self.subTest(protocol=protocol, transport=transport):
                    request = ModelRequest(
                        "fixture", ({"role": "user", "content": "hello"},)
                    )
                    server = self.service(
                        [(429, {"error": {"type": "rate_limit_error"}})]
                    )
                    adapter = self.adapter(protocol, server, transport)
                    with self.assertRaises(ProviderError) as failure:
                        adapter.execute(
                            request, adapter.prepare(request), 3, lambda: None
                        )
                    self.assertEqual(failure.exception.kind, "rate_limit")
                    self.assertEqual(failure.exception.status_code, 429)
                    self.assertEqual(len(server.requests), 1)
                    server = self.service([(500, {"error": {"message": "x" * 1000}})])
                    adapter = self.adapter(protocol, server, transport)
                    with patch(
                        "homebrew_harness.execution.providers.backends.MAX_RESPONSE_BYTES",
                        128,
                    ):
                        with self.assertRaisesRegex(ModelProtocolError, "bounded size"):
                            adapter.execute(
                                request, adapter.prepare(request), 3, lambda: None
                            )
                    self.assertEqual(len(server.requests), 1)

    def test_request_stream_override_uses_the_prepared_wire_mode(self):
        server = self.service([chat("done")])
        adapter = self.adapter(
            PROTOCOLS[0], server, "http", options_json=json.dumps({"stream": True})
        )
        request = ModelRequest(
            "fixture",
            ({"role": "user", "content": "hello"},),
            options={"stream": False},
        )
        payload = adapter.prepare(request)
        self.assertFalse(payload.get("stream"))
        self.assertEqual(
            adapter.execute(request, payload, 3, lambda: None).require_text(), "done"
        )

    def test_direct_workflow_api_resume_preserves_http_auto_binding(self):
        from homebrew_harness import Harness, HostConfig
        from homebrew_harness.orchestration.workflow_runtime import (
            WorkflowSpec,
            WorkflowRegistry,
        )

        entered = threading.Event()
        count = 0

        async def script(ctx, args):
            nonlocal count
            count += 1
            if count == 1:
                entered.set()
                await asyncio.sleep(30)
            return await ctx.agent("Return a JSON object")

        server = self.service([chat('{"result":"done"}')])
        root = self.root / "workflow"
        root.mkdir()
        env = self.root / "model.env"
        env.write_text(
            "OPENAI_API_KEY=synthetic-credential\nOPENAI_BASE_URL="
            + server.url
            + "\nOPENAI_MODEL=fixture\n"
        )
        env.chmod(0o600)
        registry = WorkflowRegistry(
            [WorkflowSpec("test", "test", script, {"type": "object"})]
        )
        config = HostConfig.from_components(workflow_registry=registry, env_file=env)
        with patch.dict("os.environ", {}, clear=True):
            with patch(
                "homebrew_harness.execution.providers.backends.sdk_available",
                return_value=(None, "sdk_not_installed"),
            ):
                with Harness(workdir=root, config=config) as host:
                    job = host.workflows.start("test")
                    identity = job.id
                    self.assertTrue(entered.wait(5))
                    job.pause()
                    self.assertEqual(
                        job.wait(5, until="settled").snapshot.state, "paused"
                    )
                    saved = host.workflows.record(identity)["model_binding"]
                    self.assertEqual(saved["transport"]["backend"], "http")
            with Harness(workdir=root, config=config) as host:
                job = host.workflows.resume(identity)
                self.assertEqual(job.wait(5).snapshot.state, "completed")
                self.assertEqual(job.runtime.runner.binding.identity(), saved)
        self.assertEqual(len(server.requests), 1)

    def test_service_restart_retains_its_prepared_http_binding(self):
        from homebrew_harness import Harness, HostConfig, ServiceSpec

        server = self.service([])
        control, workspace = self.root / "control", self.root / "project"
        control.mkdir(mode=0o700)
        workspace.mkdir()
        env = self.root / "service-model.env"
        env.write_text(
            "OPENAI_API_KEY=synthetic-credential\nOPENAI_BASE_URL="
            + server.url
            + "\nOPENAI_MODEL=fixture\n"
        )
        env.chmod(0o600)
        config = HostConfig.from_files(env_file=env, shutdown_seconds=3)
        with patch.dict("os.environ", {}, clear=True):
            with patch(
                "homebrew_harness.execution.providers.backends.sdk_available",
                return_value=(None, "sdk_not_installed"),
            ):
                with Harness(workdir=control, config=config) as host:
                    service = host.services.start(
                        ServiceSpec(str(workspace), shutdown_seconds=3)
                    )
                    identity = service.id
                    saved = service.store.get(identity)["binding"]
                    self.assertEqual(
                        saved["execution"]["model"]["transport"]["backend"], "http"
                    )
                    service.stop()
                    self.assertTrue(service.wait_closed(5))
            with Harness(workdir=control, config=config) as host:
                service = host.services.start(identity=identity)
                self.assertEqual(service.store.get(identity)["binding"], saved)
                service.stop()
                self.assertTrue(service.wait_closed(5))
        self.assertFalse(server.requests)

    def test_new_connection_does_not_inherit_an_alias_matching_another_connection(self):
        server = self.service([])
        first_spec = ModelSpec(
            "first",
            "fixture",
            "synthetic-credential",
            server.url,
            connection_id="second",
        )
        new_spec = ModelSpec(
            "second",
            "fixture",
            "synthetic-credential",
            server.url,
            connection_id="new_connection",
        )
        with patch(
            "homebrew_harness.execution.providers.backends.sdk_available",
            return_value=(None, "sdk_not_installed"),
        ):
            first = ModelRegistry([first_spec])
            saved = first.select("first").identity()
            first.close()
        with backend_scope(saved):
            registry = ModelRegistry([first_spec, new_spec])
        self.addCleanup(registry.close)
        with patch(
            "homebrew_harness.execution.providers.backends.sdk_available",
            return_value=(None, "sdk_not_installed"),
        ) as probe:
            registry.select("first")
            registry.select("second")
            probe.assert_called_once()
        self.assertFalse(server.requests)

    def test_anthropic_sdk_raw_stream_timeout_matches_http(self):
        import time
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.parse import urlunsplit

        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                requests.append(self.rfile.read(int(self.headers["Content-Length"])))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                event = {
                    "type": "message_start",
                    "message": {
                        "id": "message-1",
                        "usage": {"input_tokens": 3, "output_tokens": 0},
                    },
                }
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()
                time.sleep(0.4)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = urlunsplit(
            ("http", "127.0.0.1:" + str(server.server_port), "/v1", "", "")
        )
        for transport in ("http", "sdk"):
            if transport == "sdk" and sdk_available("anthropic_messages")[0] is None:
                continue
            with self.subTest(transport=transport):
                adapter = ProtocolAdapter(
                    "anthropic_messages",
                    api_key="synthetic-credential",
                    base_url=url,
                    transport=transport,
                    options_json=json.dumps({"stream": True}),
                )
                self.addCleanup(adapter.close)
                _ = adapter.default_headers
                request = ModelRequest(
                    "fixture", ({"role": "user", "content": "hello"},)
                )
                with self.assertRaises(ProviderError) as failure:
                    adapter.execute(
                        request, adapter.prepare(request), 0.15, lambda: None
                    )
                self.assertEqual(failure.exception.kind, "timeout")
                self.assertEqual(failure.exception.model_usage.input_tokens, 3)
        self.assertEqual(
            len(requests), 2 if sdk_available("anthropic_messages")[0] else 1
        )

    def test_non_json_upstream_error_keeps_http_status_on_all_backends(self):
        for protocol in PROTOCOLS:
            for transport in ("http", "sdk"):
                if transport == "sdk" and sdk_available(protocol)[0] is None:
                    continue
                with self.subTest(protocol=protocol, transport=transport):
                    server = self.service([(503, b"Upstream temporarily unavailable")])
                    adapter = self.adapter(protocol, server, transport)
                    request = ModelRequest(
                        "fixture", ({"role": "user", "content": "hello"},)
                    )
                    with self.assertRaises(ProviderError) as failure:
                        adapter.execute(
                            request, adapter.prepare(request), 3, lambda: None
                        )
                    self.assertEqual(failure.exception.kind, "server_error")
                    self.assertEqual(failure.exception.status_code, 503)
                    self.assertEqual(len(server.requests), 1)
