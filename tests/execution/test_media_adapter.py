"""Local wire fixture for the documented image-capable Chat Completions path.

Proves that ordered content parts reach the provider as native image content
(real frozen bytes, preserved order), that model/adapter capability gates
reject unsupported media before any provider submission, and that the media
limit classes (count, raw bytes, encoded body) are reported distinctly.
"""

import base64
import dataclasses
import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from homebrew_harness.execution.model_protocol import (
    ModelCapabilities,
    ModelProtocolError,
    ModelRequest,
)
from homebrew_harness.execution.providers.chat_completions import (
    ChatCompletionsAdapter,
)
from homebrew_harness.execution.providers.common import logical_payload
from homebrew_harness.media import MediaLimits, MediaStore
from homebrew_harness.tools.media import render_bar_chart

IMAGE_CAPS = ModelCapabilities(input_modalities=("text", "image"))
TEXT_CAPS = ModelCapabilities()


def png_bytes(seed, width=160, height=120):
    return render_bar_chart(
        [f"label {seed}"],
        [seed + 1],
        title=f"chart {seed}",
        width=width,
        height=height,
    )


def text_part(value):
    return {"type": "text", "text": value}


def frozen_part(meta):
    return {"type": "image", "media": dict(meta)}


class _Capture:
    def __init__(self):
        self.bodies = []
        self.reply = {
            "id": "chatcmpl-fixture",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "I see the chart.",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7},
        }


def _serve(capture):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(
                self.rfile.read(int(self.headers["Content-Length"]))
            )
            capture.bodies.append(body)
            payload = json.dumps(capture.reply).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class _Completions:
    def __init__(self, url):
        self._url = url

    def create(self, **body):
        request = urllib.request.Request(
            self._url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))


class _Chat:
    def __init__(self, url):
        self.completions = _Completions(url)


class WireClient:
    """Injected client speaking real HTTP to the local fixture server."""

    def __init__(self, url):
        self.chat = _Chat(url)
        self.api_key = "fixture-key"
        self.base_url = url
        self.timeout = 20
        self.default_headers = {}

    def close(self):
        pass


class TextOnlyWireAdapter(ChatCompletionsAdapter):
    supported_input_modalities = ("text",)


class MediaAdapterWireTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = MediaStore(self.root)
        self.capture = _Capture()
        self.server, self.thread = _serve(self.capture)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/chat/completions"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self._tmp.cleanup()

    def adapter(self, capabilities=IMAGE_CAPS):
        return ChatCompletionsAdapter(
            WireClient(self.url), capabilities=capabilities
        )

    def request(self, messages, **kwargs):
        return ModelRequest(
            model="fixture-vision",
            messages=tuple(messages),
            state_root=self.root,
            **kwargs,
        )

    def freeze(self, seed, **size):
        return self.store.freeze(png_bytes(seed, **size))

    def user_message(self, parts):
        return {
            "role": "user",
            "content": "\n".join(
                part["text"] for part in parts if part["type"] == "text"
            ),
            "content_parts": parts,
        }

    def data_url_bytes(self, part):
        url = part["image_url"]["url"]
        prefix = "data:image/png;base64,"
        self.assertTrue(url.startswith(prefix))
        return base64.b64decode(url.split(",", 1)[1])

    def test_ordered_parts_transmit_real_bytes_over_http(self):
        first = png_bytes(1)
        second = png_bytes(2, width=200, height=140)
        meta_a = self.store.freeze(first)
        meta_b = self.store.freeze(second)
        parts = [
            text_part("before"),
            frozen_part(meta_a),
            text_part("中间 middle"),
            frozen_part(meta_b),
            text_part("after"),
        ]
        adapter = self.adapter()
        request = self.request([self.user_message(parts)])
        payload = adapter.prepare(request)

        wire_parts = payload["messages"][0]["content"]
        self.assertEqual(
            [part["type"] for part in wire_parts],
            ["text", "image_url", "text", "image_url", "text"],
        )
        self.assertEqual(wire_parts[0]["text"], "before")
        self.assertEqual(wire_parts[2]["text"], "中间 middle")
        self.assertEqual(wire_parts[4]["text"], "after")
        self.assertEqual(self.data_url_bytes(wire_parts[1]), first)
        self.assertEqual(self.data_url_bytes(wire_parts[3]), second)

        serialized = json.dumps(payload)
        # Neither filenames nor frozen references travel as visual input.
        self.assertNotIn(".harness/media/", serialized)
        self.assertNotIn(meta_a["sha256"], serialized)

        response = adapter.execute(request, payload, 20, lambda: None)
        self.assertEqual(response.finish, "completed")
        self.assertEqual(response.parts[0]["text"], "I see the chart.")
        self.assertEqual(response.usage.input_tokens, 11)
        self.assertEqual(response.usage.output_tokens, 7)

        self.assertEqual(len(self.capture.bodies), 1)
        captured = self.capture.bodies[0]
        self.assertEqual(captured["model"], "fixture-vision")
        self.assertEqual(captured["messages"], payload["messages"])

        logical = logical_payload(payload)
        self.assertEqual(
            logical["messages"][0]["content"][1]["image_url"]["url"],
            "media:inline",
        )
        self.assertEqual(
            logical["messages"][0]["content"][3]["image_url"]["url"],
            "media:inline",
        )
        self.assertLess(len(json.dumps(logical)), len(serialized) // 2)

    def test_plain_text_request_shape_unchanged(self):
        adapter = self.adapter(TEXT_CAPS)
        request = self.request([{"role": "user", "content": "hello"}])
        payload = adapter.prepare(request)
        self.assertEqual(
            payload["messages"], [{"role": "user", "content": "hello"}]
        )
        self.assertEqual(payload["model"], "fixture-vision")
        self.assertIn("max_tokens", payload)
        response = adapter.execute(request, payload, 20, lambda: None)
        self.assertEqual(response.finish, "completed")
        self.assertEqual(self.capture.bodies[0]["messages"][0]["content"], "hello")

    def test_list_content_is_normalized_and_transmitted(self):
        data = png_bytes(4)
        meta = self.store.freeze(data)
        request = self.request(
            [
                {
                    "role": "user",
                    "content": [text_part("look"), frozen_part(meta)],
                }
            ]
        )
        payload = self.adapter().prepare(request)
        wire_parts = payload["messages"][0]["content"]
        self.assertEqual(wire_parts[0], {"type": "text", "text": "look"})
        self.assertEqual(self.data_url_bytes(wire_parts[1]), data)

    def test_text_only_model_rejected_before_submission(self):
        meta = self.freeze(5)
        request = self.request(
            [self.user_message([text_part("look"), frozen_part(meta)])]
        )
        with self.assertRaises(ModelProtocolError) as caught:
            self.adapter(TEXT_CAPS).prepare(request)
        self.assertIn("does not declare image input support", str(caught.exception))
        self.assertIn("never silently dropped", str(caught.exception))
        self.assertEqual(self.capture.bodies, [])

    def test_text_only_wire_adapter_rejected(self):
        meta = self.freeze(6)
        request = self.request(
            [self.user_message([text_part("look"), frozen_part(meta)])]
        )
        adapter = TextOnlyWireAdapter(
            WireClient(self.url), capabilities=IMAGE_CAPS
        )
        with self.assertRaises(ModelProtocolError) as caught:
            adapter.prepare(request)
        self.assertIn("cannot carry native image content", str(caught.exception))
        self.assertEqual(self.capture.bodies, [])

    def test_unfrozen_image_rejected_before_submission(self):
        request = self.request(
            [
                self.user_message(
                    [text_part("look"), {"type": "image", "path": "picture.png"}]
                )
            ]
        )
        with self.assertRaises(ModelProtocolError) as caught:
            self.adapter().prepare(request)
        self.assertIn("must be frozen", str(caught.exception))
        self.assertEqual(self.capture.bodies, [])

    def test_missing_state_root_rejected(self):
        meta = self.freeze(7)
        request = ModelRequest(
            model="fixture-vision",
            messages=(self.user_message([text_part("look"), frozen_part(meta)]),),
        )
        with self.assertRaises(ModelProtocolError) as caught:
            self.adapter().prepare(request)
        self.assertIn("owning state root", str(caught.exception))
        self.assertEqual(self.capture.bodies, [])

    def test_reclaimed_media_reports_distinct_code(self):
        meta = self.freeze(8)
        (self.root / meta["media_ref"]).unlink()
        request = self.request(
            [self.user_message([text_part("look"), frozen_part(meta)])]
        )
        with self.assertRaises(ModelProtocolError) as caught:
            self.adapter().prepare(request)
        self.assertEqual(getattr(caught.exception, "code", None), "media_reclaimed")
        self.assertEqual(self.capture.bodies, [])

    def test_request_count_limit_is_distinct(self):
        parts = [text_part("many")]
        for seed in range(9):
            parts.append(frozen_part(self.freeze(seed + 20)))
        request = self.request([self.user_message(parts)])
        with self.assertRaises(ModelProtocolError) as caught:
            self.adapter().prepare(request)
        self.assertEqual(
            getattr(caught.exception, "code", None), "media_request_count_limit"
        )
        details = caught.exception.details
        self.assertEqual(details["limit_value"], 8)
        self.assertEqual(details["observed_value"], 9)
        self.assertEqual(self.capture.bodies, [])

    def test_request_image_bytes_limit_is_distinct(self):
        parts = [
            text_part("two"),
            frozen_part(self.freeze(40)),
            frozen_part(self.freeze(41)),
        ]
        request = self.request([self.user_message(parts)])
        small = dataclasses.replace(MediaLimits(), max_request_image_bytes=100)
        with mock.patch("homebrew_harness.media.MediaLimits", return_value=small):
            with self.assertRaises(ModelProtocolError) as caught:
                self.adapter().prepare(request)
        self.assertEqual(
            getattr(caught.exception, "code", None), "media_request_bytes_limit"
        )
        self.assertEqual(caught.exception.details["limit_value"], 100)
        self.assertEqual(self.capture.bodies, [])

    def test_request_body_limit_is_distinct(self):
        meta = self.freeze(42)
        request = self.request(
            [self.user_message([text_part("look"), frozen_part(meta)])]
        )
        small = dataclasses.replace(MediaLimits(), max_request_body_bytes=64)
        with mock.patch("homebrew_harness.media.MediaLimits", return_value=small):
            with self.assertRaises(ModelProtocolError) as caught:
                self.adapter().prepare(request)
        self.assertEqual(
            getattr(caught.exception, "code", None), "media_request_body_limit"
        )
        self.assertEqual(caught.exception.details["limit_value"], 64)
        self.assertEqual(self.capture.bodies, [])

    def tool_turn_messages(self, metas):
        calls = [
            {
                "id": f"call-{index}",
                "type": "function",
                "function": {"name": "render_chart", "arguments": "{}"},
            }
            for index in range(len(metas))
        ]
        rows = [
            {"role": "user", "content": "draw the chart"},
            {"role": "assistant", "content": "", "tool_calls": calls},
        ]
        for index, meta in enumerate(metas):
            rows.append(
                {
                    "role": "tool",
                    "tool_call_id": f"call-{index}",
                    "content": json.dumps(
                        {"text": "Chart rendered.", "media": [meta]}
                    ),
                }
            )
        rows.append({"role": "user", "content": "what do you see?"})
        return rows

    def test_tool_media_reinspected_natively_on_capable_path(self):
        data = png_bytes(50)
        meta = self.store.freeze(data)
        request = self.request(self.tool_turn_messages([meta]))
        payload = self.adapter().prepare(request)
        rows = payload["messages"]
        media_rows = [row for row in rows if row.get("name") == "tool_media"]
        self.assertEqual(len(media_rows), 1)
        parts = media_rows[0]["content"]
        self.assertEqual(parts[0]["type"], "text")
        self.assertEqual(parts[1]["type"], "image_url")
        self.assertEqual(self.data_url_bytes(parts[1]), data)
        # The reinspection turn sits after the tool result, before the next
        # user turn; the canonical tool result keeps its text projection.
        roles = [row["role"] for row in rows]
        self.assertEqual(roles, ["user", "assistant", "tool", "user", "user"])
        tool_row = rows[2]
        self.assertIn(meta["media_ref"], tool_row["content"])

    def test_tool_media_deduplicated_by_reference(self):
        meta = self.freeze(51)
        request = self.request(self.tool_turn_messages([meta, meta]))
        payload = self.adapter().prepare(request)
        media_rows = [
            row for row in payload["messages"] if row.get("name") == "tool_media"
        ]
        self.assertEqual(len(media_rows), 1)
        images = [
            part for part in media_rows[0]["content"] if part["type"] == "image_url"
        ]
        self.assertEqual(len(images), 1)

    def test_tool_media_stays_text_on_text_only_model(self):
        meta = self.freeze(52)
        request = self.request(self.tool_turn_messages([meta]))
        payload = self.adapter(TEXT_CAPS).prepare(request)
        rows = payload["messages"]
        self.assertFalse(any(row.get("name") == "tool_media" for row in rows))
        self.assertNotIn("image_url", json.dumps(payload))
        tool_row = next(row for row in rows if row["role"] == "tool")
        self.assertIn(meta["media_ref"], tool_row["content"])
        self.assertIn(meta["sha256"], tool_row["content"])


if __name__ == "__main__":
    unittest.main()
