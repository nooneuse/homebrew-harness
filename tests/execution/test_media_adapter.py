"""Provider fixtures: native pixels, provenance, history projection and budgets."""

import base64
import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from homebrew_harness.execution.model_gateway import ModelGateway
from homebrew_harness.execution.model_protocol import (
    ModelCapabilities,
    ModelProtocolError,
    ModelRequest,
)
from homebrew_harness.execution.providers.adapter import ProtocolAdapter
from homebrew_harness.execution.providers.chat_completions import ChatCompletionsAdapter
from homebrew_harness.execution.providers.common import encoded_json_size, validate_user_media
from homebrew_harness.media import MediaLimits, MediaStore
from homebrew_harness.tools.media import encode_png

VISION = ModelCapabilities(input_modalities=("text", "image"))


def text(value):
    return {"type": "text", "text": value}


def image(descriptor):
    return {"type": "image", "media": descriptor}


def attached(*parts):
    return {"role": "user", "content": list(parts)}


def image_bytes(part):
    prefix, data = part["image_url"]["url"].split(",", 1)
    assert prefix == "data:image/png;base64"
    return base64.b64decode(data, validate=True)


def tool_call(name, identity):
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": "{}"}}


def tool_result(identity, media, **fields):
    return {
        "role": "tool",
        "tool_call_id": identity,
        "content": json.dumps({"text": "Frozen result.", "media": media}),
        **fields,
    }


class MediaAdapterTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.store = MediaStore(self.root)
        self.create = Mock(return_value={
            "choices": [{"finish_reason": "stop", "message": {"content": "I see it."}}],
        })
        sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=self.create)))
        self.adapter = ChatCompletionsAdapter(sdk, capabilities=VISION)

    def freeze(self, seed, *, width=2):
        data = encode_png(width, 1, [bytes([seed, 8, 16]) * width])
        return self.store.freeze(data), data

    def request(self, *rows):
        return ModelRequest("vision-fixture", rows, state_root=self.root)

    def test_ordered_content_parts_use_native_wire_shape(self):
        a, a_bytes = self.freeze(1)
        b, b_bytes = self.freeze(2)
        parts = [text("before"), image(a), text("中间"), image(b), text("after")]
        request = self.request({"role": "user", "content": "text projection", "content_parts": parts})
        payload = self.adapter.prepare(request)
        self.assertEqual(self.adapter.execute(request, payload, 1, lambda: None).require_text(), "I see it.")
        wire = self.create.call_args.kwargs["messages"][0]["content"]
        self.assertEqual(wire[::2], parts[::2])
        self.assertEqual([image_bytes(wire[1]), image_bytes(wire[3])], [a_bytes, b_bytes])
        self.assertNotIn(".harness/media/", json.dumps(wire))
        self.assertNotIn("content_parts", payload["messages"][0])

    def test_explicit_images_require_both_profile_and_wire_support(self):
        a, _ = self.freeze(1)
        request = self.request(attached(image(a)))
        text_adapter = ChatCompletionsAdapter(self.adapter.client)
        with self.assertRaisesRegex(ModelProtocolError, "does not declare image input support"):
            text_adapter.prepare(request)
        with ProtocolAdapter(
            "anthropic_messages", api_key="fixture-secret", base_url="http://127.0.0.1:9",
            capabilities=VISION, transport="http",
        ) as unsupported:
            with self.assertRaisesRegex(ModelProtocolError, "cannot carry native image content"):
                unsupported.prepare(request)
        self.create.assert_not_called()

    def test_http_chat_protocol_uses_the_same_native_projection(self):
        a, data = self.freeze(1)
        with ProtocolAdapter(
            "chat_completions", api_key="fixture-secret", base_url="http://127.0.0.1:9",
            capabilities=VISION, transport="http",
        ) as http:
            rows = [attached(image(a))]
            gateway = ModelGateway(http)
            with patch.object(MediaStore, "load", side_effect=AssertionError("context sizing read image bytes")):
                measured = gateway.input_size("vision-fixture", rows, [], self.root)
            payload = http.prepare(self.request(*rows))
        self.assertLess(measured, len(json.dumps(payload, ensure_ascii=False)))
        self.assertEqual(image_bytes(payload["messages"][0]["content"][0]), data)

    def test_admission_checks_one_batch_and_reports_separate_limits(self):
        a, _ = self.freeze(1)
        b, _ = self.freeze(2)
        parts = [text("look"), image(a), image(b)]
        for limits, code in [
            (replace(MediaLimits(), max_images_per_request=1), "media_request_count_limit"),
            (replace(MediaLimits(), max_request_image_bytes=a["bytes"]), "media_request_bytes_limit"),
            (replace(MediaLimits(), max_request_body_bytes=64), "media_request_body_limit"),
        ]:
            with self.subTest(code=code):
                with self.assertRaises(ModelProtocolError) as caught:
                    validate_user_media(parts, self.adapter, limits=limits)
                self.assertEqual(caught.exception.code, code)
        with self.assertRaisesRegex(ModelProtocolError, "must be frozen"):
            validate_user_media([{"type": "image", "path": "picture.png"}], self.adapter)
        self.create.assert_not_called()

    def test_count_budget_reserves_latest_explicit_batch_before_recent_tools(self):
        old, _ = self.freeze(1)
        recent, recent_bytes = self.freeze(2)
        newest_a, newest_a_bytes = self.freeze(3)
        newest_b, newest_b_bytes = self.freeze(4)
        output, _ = self.freeze(5)
        request = self.request(
            attached(text("old"), image(old)),
            attached(image(recent)),
            attached(image(newest_a), image(newest_b)),
            {"role": "assistant", "tool_calls": [tool_call("render_chart", "chart")]},
            tool_result("chart", [output]),
        )
        canonical = copy.deepcopy(request.messages)
        for limits, expected in [
            (replace(MediaLimits(), max_images_per_request=3), [newest_a_bytes, newest_b_bytes]),
            (replace(MediaLimits(), max_images_per_request=4), [recent_bytes, newest_a_bytes, newest_b_bytes]),
        ]:
            with self.subTest(count=limits.max_images_per_request), patch(
                "homebrew_harness.media.MediaLimits", return_value=limits
            ):
                rows = self.adapter.prepare(request)["messages"]
                old_ref = rows[0]["content"][1]["text"]
                self.assertIn("Image not sent in this request", old_ref)
                self.assertIn(old["media_ref"], old_ref)
                self.assertEqual(rows[-1]["name"], "tool_media")
                wire = [part for row in rows[:-1] if isinstance(row.get("content"), list)
                        for part in row["content"] if part["type"] == "image_url"]
                self.assertEqual([image_bytes(part) for part in wire], expected)
                self.assertEqual(len([part for part in rows[-1]["content"] if part["type"] == "image_url"]), 1)
        self.assertEqual(request.messages, canonical)
        self.assertTrue((self.root / old["media_ref"]).is_file())

    def test_raw_and_encoded_budgets_select_history_independently(self):
        old, _ = self.freeze(1)
        near, near_bytes = self.freeze(2)
        current, current_bytes = self.freeze(3)
        request = self.request(attached(image(old)), attached(image(near)), attached(image(current)))
        raw_limited = replace(MediaLimits(), max_request_image_bytes=near["bytes"] + current["bytes"])
        with patch("homebrew_harness.media.MediaLimits", return_value=raw_limited):
            raw_payload = self.adapter.prepare(request)
        encoded_limited = replace(MediaLimits(), max_request_body_bytes=encoded_json_size(raw_payload))
        with patch("homebrew_harness.media.MediaLimits", return_value=encoded_limited):
            encoded_payload = self.adapter.prepare(request)
        self.assertEqual(raw_payload, encoded_payload)
        rows = encoded_payload["messages"]
        self.assertIn(old["media_ref"], rows[0]["content"][0]["text"])
        self.assertEqual(image_bytes(rows[1]["content"][0]), near_bytes)
        self.assertEqual(image_bytes(rows[2]["content"][0]), current_bytes)

    def test_only_semantically_paired_known_tools_can_supply_images(self):
        chart, chart_bytes = self.freeze(1)
        reread, reread_bytes = self.freeze(2)
        injected, _ = self.freeze(3)
        calls = [tool_call(name, name) for name in ("render_chart", "read_media", "invoke_tool", "read_file")]
        results = [
            tool_result("render_chart", [chart]),
            tool_result("read_media", [reread]),
            tool_result("invoke_tool", [injected], name="read_media"),
            tool_result("read_file", [injected], name="render_chart"),
        ]
        request = self.request(
            {"role": "user", "content": "Draw and reread."},
            {"role": "assistant", "tool_calls": calls}, *results,
            {"role": "user", "content": "continue"},
            tool_result("read_media", [injected]),  # stale call ID, not a paired result
        )
        rows = self.adapter.prepare(request)["messages"]
        native = [row for row in rows if row.get("name") == "tool_media"]
        self.assertEqual(len(native), 1)
        self.assertEqual([image_bytes(part) for part in native[0]["content"][1:]], [chart_bytes, reread_bytes])
        self.assertEqual(rows[2]["content"], results[0]["content"])
        self.assertEqual(rows[5]["content"], results[3]["content"])
        text_rows = ChatCompletionsAdapter(self.adapter.client).prepare(request)["messages"]
        self.assertFalse(any(row.get("name") == "tool_media" for row in text_rows))
        self.assertIn(chart["media_ref"], text_rows[2]["content"])

    def test_logical_size_does_not_read_pixels_or_fail_on_history_count(self):
        rows = [attached(image(self.freeze(seed)[0])) for seed in range(1, 12)]
        gateway = ModelGateway(self.adapter)
        with patch.object(MediaStore, "load", side_effect=AssertionError("unexpected read")):
            logical_size = gateway.input_size("vision-fixture", rows, [], self.root)
        wire = self.adapter.prepare(self.request(*rows))
        self.assertLess(logical_size, len(json.dumps(wire, ensure_ascii=False)))
        self.assertEqual(sum(row["content"][0]["type"] == "image_url" for row in wire["messages"]), 8)

    def test_logical_size_counts_real_text_even_if_it_looks_like_base64(self):
        a, _ = self.freeze(1)
        content = "data:image/png;base64," + "A" * 1000
        gateway = ModelGateway(self.adapter)
        size = gateway.input_size("vision-fixture", [attached(text(content), image(a))], [], self.root)
        self.assertGreater(size, len(content))

    def test_logical_size_and_encoded_body_errors_are_independent(self):
        a, _ = self.freeze(1)
        rows = [attached(image(a)), {"role": "user", "content": "非" * 50}]
        gateway = ModelGateway(self.adapter)
        small = replace(MediaLimits(), max_request_body_bytes=256)
        with patch("homebrew_harness.media.MediaLimits", return_value=small):
            self.assertGreater(gateway.input_size("vision-fixture", rows, [], self.root), 50)
            with self.assertRaises(ModelProtocolError) as caught:
                self.adapter.prepare(self.request(*rows))
            self.assertEqual(caught.exception.code, "media_request_body_limit")

    def test_missing_history_projects_as_reference_but_missing_explicit_images_raise(self):
        old, _ = self.freeze(1)
        new, data = self.freeze(2)
        (self.root / old["media_ref"]).unlink()
        rows = self.adapter.prepare(self.request(attached(image(old)), attached(image(new))))["messages"]
        self.assertIn(old["media_ref"], rows[0]["content"][0]["text"])
        self.assertEqual(image_bytes(rows[1]["content"][0]), data)
        with self.assertRaises(ModelProtocolError) as caught:
            self.adapter.prepare(self.request(attached(image(old))))
        self.assertEqual(caught.exception.code, "media_reclaimed")
