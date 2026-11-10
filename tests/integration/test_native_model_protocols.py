"""Real native HTTP and SSE transport through the same Agent Loop."""

import io
import json
import tempfile
import threading
import unittest
from collections import deque
from contextlib import redirect_stdout, redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlunsplit

from homebrew_harness import HarnessSession, ModelRegistry, ModelSpec
from homebrew_harness.execution.model_gateway import ModelGateway
from homebrew_harness.execution.model_protocol import ModelProtocolError
from homebrew_harness.execution.providers.native import NativeAdapter


class ProtocolService:
    def __init__(self, responses):
        self.responses = deque(responses)
        self.requests = []
        self.headers = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                outer.requests.append(
                    json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                )
                outer.headers.append(dict(self.headers))
                value = outer.responses.popleft()
                status = 200
                if isinstance(value, tuple) and isinstance(value[0], int):
                    status, value = value
                if isinstance(value, tuple):
                    raw = "".join(
                        "data: " + json.dumps(event) + "\n\n" for event in value[1]
                    ).encode()
                    kind = "text/event-stream"
                elif isinstance(value, bytes):
                    raw, kind = value, "text/plain"
                else:
                    raw, kind = json.dumps(value).encode(), "application/json"
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = urlunsplit(
            ("http", "127.0.0.1:" + str(self.server.server_port), "/v1", "", "")
        )

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


def responses(text=None, *, tool=None):
    items = [
        {
            "type": "reasoning",
            "id": "reasoning-1",
            "encrypted_content": "opaque-provider-state",
        }
    ]
    if tool:
        items.append(
            {
                "type": "function_call",
                "id": "item-1",
                "call_id": "call-1",
                "name": "write_file",
                "arguments": json.dumps(tool),
            }
        )
    else:
        items.append(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        )
    return {
        "id": "response-1",
        "status": "completed",
        "output": items,
        "usage": {
            "input_tokens": 40,
            "output_tokens": 10,
            "input_tokens_details": {"cached_tokens": 8},
        },
    }


def anthropic(text=None, *, tool=None):
    blocks = [
        {
            "type": "thinking",
            "thinking": "reasoning payload",
            "signature": "unaltered-signature",
        }
    ]
    blocks.append(
        {"type": "tool_use", "id": "call-1", "name": "write_file", "input": tool}
        if tool
        else {"type": "text", "text": text}
    )
    return {
        "id": "message-1",
        "content": blocks,
        "stop_reason": "tool_use" if tool else "end_turn",
        "usage": {
            "input_tokens": 40,
            "output_tokens": 10,
            "cache_read_input_tokens": 8,
            "cache_creation_input_tokens": 4,
        },
    }


def generation(text=None, *, tool=None):
    message = {"role": "assistant", "content": text}
    if tool:
        message["tool_calls"] = [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "write_file", "arguments": json.dumps(tool)},
            }
        ]
    return {
        "request_id": "generation-1",
        "output": {
            "choices": [
                {"message": message, "finish_reason": "tool_calls" if tool else "stop"}
            ]
        },
        "usage": {"input_tokens": 40, "output_tokens": 10},
    }


class NativeProtocolTests(unittest.TestCase):
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

    def run_loop(self, protocol, values, options=None):
        server = self.service(values)
        model = ModelSpec(
            "planner",
            "fixture",
            "synthetic-credential",
            server.url,
            protocol=protocol,
            options_json=json.dumps(options or {}),
        )
        registry = ModelRegistry([model], lead_default="planner")
        session = HarnessSession(
            None, "", self.root, model_registry=registry, shutdown_timeout=0.05
        )
        try:
            self.assertEqual(
                session.run_user("Write the specified output and report"), "done"
            )
            self.assertEqual((self.root / "output.txt").read_text(), "output")
            self.assertEqual(session.gateway.summary()["calls"], 2)
        finally:
            session.close()
            self.assertTrue(session.wait_closed(4))
        return server

    def test_responses_tool_items_and_exact_reasoning_replay(self):
        server = self.run_loop(
            "openai_responses",
            [
                responses(tool={"path": "output.txt", "content": "output"}),
                responses("done"),
            ],
        )
        self.assertFalse(server.requests[0]["store"])
        second = server.requests[1]["input"]
        self.assertTrue(
            any(i.get("encrypted_content") == "opaque-provider-state" for i in second)
        )
        self.assertTrue(
            any(
                i.get("type") == "function_call_output" and i["call_id"] == "call-1"
                for i in second
            )
        )
        self.assertNotIn("_protocol_state", json.dumps(second))

    def test_anthropic_blocks_tool_result_origin_and_thinking_signature(self):
        server = self.run_loop(
            "anthropic_messages",
            [
                anthropic(tool={"path": "output.txt", "content": "output"}),
                anthropic("done"),
            ],
        )
        second = server.requests[1]
        self.assertTrue(second["system"])
        blocks = [b for m in second["messages"] for b in m["content"]]
        self.assertTrue(
            any(b.get("signature") == "unaltered-signature" for b in blocks)
        )
        self.assertTrue(
            any(
                b.get("type") == "tool_result" and b["tool_use_id"] == "call-1"
                for b in blocks
            )
        )

    def test_generation_native_payload_and_tool_results(self):
        server = self.run_loop(
            "dashscope_generation",
            [
                generation(tool={"path": "output.txt", "content": "output"}),
                generation("done"),
            ],
        )
        self.assertIn("input", server.requests[0])
        self.assertIn("parameters", server.requests[0])
        self.assertTrue(
            any(
                m.get("role") == "tool" and m["tool_call_id"] == "call-1"
                for m in server.requests[1]["input"]["messages"]
            )
        )

    def test_responses_stream_requires_terminal_event(self):
        server = self.service(
            [("sse", [{"type": "response.output_text.delta", "delta": "partial"}])]
        )
        adapter = NativeAdapter(
            "openai_responses",
            api_key="synthetic-credential",
            base_url=server.url,
            options_json='{"stream":true}',
        )
        gateway = ModelGateway(adapter, workdir=self.root)
        try:
            with self.assertRaises(ModelProtocolError):
                gateway.request(
                    model="fixture", messages=[{"role": "user", "content": "test"}]
                )
        finally:
            gateway.close()
            self.assertTrue(gateway.wait_idle(3))
            adapter.close()

    def test_responses_stream_final_payload_is_authoritative(self):
        server = self.service(
            [
                (
                    "sse",
                    [
                        {"type": "response.output_text.delta", "delta": "partial"},
                        {"type": "response.completed", "response": responses("done")},
                    ],
                )
            ]
        )
        adapter = NativeAdapter(
            "openai_responses",
            api_key="synthetic-credential",
            base_url=server.url,
            options_json='{"stream":true}',
        )
        gateway = ModelGateway(adapter, workdir=self.root)
        try:
            result = gateway.request(
                model="fixture", messages=[{"role": "user", "content": "test"}]
            )
            self.assertEqual(result.text, "done")
            self.assertEqual(gateway.summary()["known_tokens"], 50)
        finally:
            gateway.close()
            self.assertTrue(gateway.wait_idle(3))
            adapter.close()

    def test_anthropic_usage_cache_parts_are_added_once(self):
        server = self.service([anthropic("done")])
        adapter = NativeAdapter(
            "anthropic_messages", api_key="synthetic-credential", base_url=server.url
        )
        gateway = ModelGateway(adapter, workdir=self.root)
        try:
            result = gateway.request(
                model="fixture", messages=[{"role": "user", "content": "test"}]
            )
            self.assertEqual(result.usage.input_tokens, 52)
            self.assertEqual(result.usage.total_tokens, 62)
        finally:
            gateway.close()
            gateway.wait_idle(3)
            adapter.close()

    def test_generation_cumulative_stream_replaces_previous_text(self):
        server = self.service([("sse", [generation("do"), generation("done")])])
        adapter = NativeAdapter(
            "dashscope_generation",
            api_key="synthetic-credential",
            base_url=server.url,
            options_json='{"stream":true,"incremental_output":false}',
        )
        gateway = ModelGateway(adapter, workdir=self.root)
        try:
            result = gateway.request(
                model="fixture", messages=[{"role": "user", "content": "test"}]
            )
            self.assertEqual(result.text, "done")
            self.assertEqual(result.usage.total_tokens, 50)
        finally:
            gateway.close()
            gateway.wait_idle(3)
            adapter.close()

    def test_anthropic_stream_aggregates_tool_json_and_signature(self):
        first = [
            {
                "type": "message_start",
                "message": {"id": "stream-message", "usage": {"input_tokens": 12}},
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": "", "signature": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "private reasoning"},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {
                    "type": "signature_delta",
                    "signature": "unaltered-signature",
                },
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {
                    "type": "tool_use",
                    "id": "call-1",
                    "name": "write_file",
                    "input": {},
                },
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": '{"path":"output.txt",',
                },
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": '"content":"output"}',
                },
            },
            {"type": "content_block_stop", "index": 1},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 8},
            },
            {"type": "message_stop"},
        ]
        second = [
            {
                "type": "message_start",
                "message": {"id": "stream-final", "usage": {"input_tokens": 12}},
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
                "usage": {"output_tokens": 8},
            },
            {"type": "message_stop"},
        ]
        server = self.run_loop(
            "anthropic_messages", [("sse", first), ("sse", second)], {"stream": True}
        )
        self.assertTrue(
            any(
                b.get("signature") == "unaltered-signature"
                for m in server.requests[1]["messages"]
                for b in m["content"]
            )
        )

    def test_replay_is_private_and_rejects_owner_or_content_changes(self):
        from homebrew_harness.execution.model_protocol import save_replay, load_replay
        from homebrew_harness.tools.files import run_read

        ref = save_replay(
            self.root, "owner", "binding", {"signature": "unaltered-signature"}
        )
        self.assertEqual(
            load_replay(self.root, "owner", "binding", ref)["signature"],
            "unaltered-signature",
        )
        with self.assertRaises(ModelProtocolError):
            load_replay(self.root, "another-owner", "binding", ref)
        with self.assertRaises(ValueError):
            run_read(ref["reference"], self.root)
        (self.root / ref["reference"]).write_text("changed")
        with self.assertRaises(ModelProtocolError):
            load_replay(self.root, "owner", "binding", ref)

    def test_unknown_server_tools_do_not_enter_local_tool_dispatch(self):
        from homebrew_harness.execution.providers.responses import decode

        with self.assertRaises(ModelProtocolError):
            decode({"status": "completed", "output": [{"type": "web_search_call"}]})

    def test_extensions_cannot_override_runtime_model_or_tools(self):
        with self.assertRaises(ValueError):
            ModelSpec(
                "planner",
                "fixture",
                "synthetic-credential",
                self.service([]).url,
                protocol="openai_responses",
                options_json='{"extensions":{"model":"other"}}',
            )
