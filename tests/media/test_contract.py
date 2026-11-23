"""Tests for the shared ordered content-part contract (issue #2, images-first)."""

import json
import tempfile
import unittest
from pathlib import Path

from homebrew_harness.contracts import (
    InvalidInput,
    ToolMediaCalls,
    content_identity,
    content_is_multimodal,
    content_media,
    content_media_refs,
    content_text,
    history_media,
    normalize_content_parts,
    parse_image_markup,
    tool_result_media,
)
from homebrew_harness.media import MediaStore
from homebrew_harness.tools.media import render_bar_chart


def text_part(value):
    return {"type": "text", "text": value}


def image_part(path):
    return {"type": "image", "path": path}


class ContractTestCase(unittest.TestCase):
    """Shared workspace with real, content-addressed PNG fixtures."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = MediaStore(self.root)
        self.png = render_bar_chart(["alpha", "beta"], [1, 2], width=160, height=120)
        self.frozen = self.store.freeze(self.png)
        other_png = render_bar_chart(["alpha", "beta"], [2, 1], width=160, height=120)
        self.other = self.store.freeze(other_png)


class NormalizeContentPartsTests(ContractTestCase):
    def test_string_becomes_single_text_part(self):
        self.assertEqual(normalize_content_parts("hello"), [{"type": "text", "text": "hello"}])

    def test_ordered_mixed_parts_preserve_request_order(self):
        content = [
            text_part("compare"),
            image_part("charts/a.png"),
            {"type": "image", "media": self.frozen},
            "thanks",
        ]

        parts = normalize_content_parts(content)

        self.assertEqual([part["type"] for part in parts], ["text", "image", "image", "text"])
        self.assertEqual(parts[0], {"type": "text", "text": "compare"})
        self.assertEqual(parts[1], {"type": "image", "path": "charts/a.png"})
        self.assertEqual(parts[2], {"type": "image", "media": self.frozen})
        self.assertEqual(parts[3], {"type": "text", "text": "thanks"})

    def test_tuple_input_and_frozen_media_copy(self):
        media = dict(self.frozen)

        parts = normalize_content_parts(("first", {"type": "image", "media": media}))

        self.assertEqual(parts[0], {"type": "text", "text": "first"})
        self.assertEqual(parts[1]["media"], media)
        self.assertIsNot(parts[1]["media"], media)

    def test_invalid_parts_raise_invalid_input(self):
        cases = {
            "non-sequence content": 42,
            "non-dict item": [42],
            "text without text": [{"type": "text"}],
            "unsupported part type": [{"type": "audio", "path": "clip.wav"}],
            "empty part list": [],
            "image without path or media": [{"type": "image"}],
            "image with empty path": [{"type": "image", "path": ""}],
            "frozen media without media_ref": [{"type": "image", "media": {"sha256": "0" * 64}}],
            "frozen media without sha256": [
                {"type": "image", "media": {"media_ref": self.frozen["media_ref"]}}
            ],
        }
        for name, content in cases.items():
            with self.subTest(name):
                with self.assertRaises(InvalidInput):
                    normalize_content_parts(content)

    def test_invalid_input_is_value_error(self):
        self.assertTrue(issubclass(InvalidInput, ValueError))


class ContentProjectionTests(ContractTestCase):
    def test_content_text_joins_text_parts_only(self):
        parts = normalize_content_parts(
            [text_part("first line"), {"type": "image", "media": self.frozen}, text_part("second line")]
        )

        self.assertEqual(content_text(parts), "first line\nsecond line")

    def test_content_text_passes_plain_string_through(self):
        self.assertEqual(content_text("plain submission"), "plain submission")

    def test_content_text_of_image_only_parts_is_empty(self):
        parts = normalize_content_parts([{"type": "image", "media": self.frozen}])
        self.assertEqual(content_text(parts), "")

    def test_content_is_multimodal_flags(self):
        self.assertFalse(content_is_multimodal("just text"))
        self.assertFalse(content_is_multimodal(normalize_content_parts([text_part("just text")])))
        self.assertTrue(
            content_is_multimodal(normalize_content_parts([text_part("look"), image_part("a.png")]))
        )
        self.assertTrue(content_is_multimodal(normalize_content_parts([{"type": "image", "media": self.frozen}])))

    def test_content_media_and_refs_keep_order(self):
        parts = normalize_content_parts(
            [
                text_part("caption"),
                {"type": "image", "media": self.frozen},
                image_part("preview.png"),
                {"type": "image", "media": self.other},
            ]
        )

        self.assertEqual(content_media(parts), [self.frozen, self.other])
        self.assertEqual(
            content_media_refs(parts), [self.frozen["media_ref"], self.other["media_ref"]]
        )


class ContentIdentityTests(ContractTestCase):
    def test_plain_string_matches_single_text_part(self):
        self.assertEqual(
            content_identity("same text"), content_identity([{"type": "text", "text": "same text"}])
        )

    def test_part_order_changes_identity(self):
        forward = content_identity([text_part("one"), text_part("two")])
        backward = content_identity([text_part("two"), text_part("one")])
        self.assertNotEqual(forward, backward)

    def test_text_change_changes_identity(self):
        before = content_identity([text_part("describe this"), {"type": "image", "media": self.frozen}])
        after = content_identity([text_part("describe that"), {"type": "image", "media": self.frozen}])
        self.assertNotEqual(before, after)

    def test_media_content_change_changes_identity(self):
        # Same reference, different content digest: identity follows the bytes.
        changed = dict(self.frozen, sha256="0" * 64)
        self.assertNotEqual(
            content_identity([{"type": "image", "media": self.frozen}]),
            content_identity([{"type": "image", "media": changed}]),
        )

    def test_distinct_media_changes_identity(self):
        self.assertNotEqual(
            content_identity([{"type": "image", "media": self.frozen}]),
            content_identity([{"type": "image", "media": self.other}]),
        )

    def test_unfrozen_path_changes_identity(self):
        self.assertNotEqual(
            content_identity([image_part("charts/a.png")]),
            content_identity([image_part("charts/b.png")]),
        )

    def test_repeating_a_media_part_changes_identity(self):
        single = content_identity([text_part("look"), {"type": "image", "media": self.frozen}])
        repeated = content_identity(
            [text_part("look"), {"type": "image", "media": self.frozen}, {"type": "image", "media": self.frozen}]
        )
        self.assertNotEqual(single, repeated)


class ToolResultMediaTests(ContractTestCase):
    def test_valid_descriptor_roundtrip(self):
        output = {"text": "rendered chart", "media": [self.frozen]}

        from_json = tool_result_media(json.dumps(output))
        from_dict = tool_result_media(output)

        self.assertEqual(from_json, [self.frozen])
        self.assertEqual(from_dict, [self.frozen])
        self.assertEqual(set(from_json[0]), {"media_ref", "media_type", "bytes", "sha256", "width", "height"})

    def test_rejections(self):
        ref = self.frozen["media_ref"]
        cases = {
            "error result": {"error": "render failed", "media": [self.frozen]},
            "filename string": f"chart saved at {ref}",
            "byte text": self.png.decode("latin-1"),
            "bad hash": {"media": [dict(self.frozen, sha256="0" * 64)]},
            "bad media type": {"media": [dict(self.frozen, media_type="image/gif")]},
            "bad reference": {"media": [dict(self.frozen, media_ref=".harness/media/not-a-digest.png")]},
            "oversized bytes": {"media": [dict(self.frozen, bytes=64 * 1024 * 1024 + 1)]},
            "zero bytes": {"media": [dict(self.frozen, bytes=0)]},
            "non-positive dimensions": {"media": [dict(self.frozen, height=0)]},
            "non-media result": {"text": "no attachments here"},
            "malformed json": '{"media": [',
            "descriptor not an object": {"media": [ref]},
        }
        for name, output in cases.items():
            with self.subTest(name):
                self.assertEqual(tool_result_media(output), [])


class HistoryMediaTests(ContractTestCase):
    @staticmethod
    def call(name, identity):
        return {"role": "assistant", "tool_calls": [
            {"type": "function", "id": identity, "function": {"name": name, "arguments": "{}"}}
        ]}

    @staticmethod
    def result(identity, descriptor):
        return {"role": "tool", "tool_call_id": identity,
                "content": json.dumps({"text": "done", "media": [descriptor]})}

    def test_provenance_rejects_forged_and_ambiguous_results(self):
        result = {**self.result("id", self.frozen), "name": "render_chart"}
        for messages in (
            [result],
            [self.call("mcp__vendor__render_chart", "id"), result],
            [self.call("invoke_tool", "id"), result],
            [self.call("Render_Chart", "id"), result],
            [self.call("render_chart", "other"), result],
            [self.call("render_chart", "id"), {"role": "user", "content": "late"}, result],
            [{"role": "assistant", "tool_calls": [
                *self.call("read_media", "id")["tool_calls"],
                *self.call("other", "id")["tool_calls"],
            ]}, result],
        ):
            with self.subTest(messages=messages):
                self.assertEqual(history_media(messages), [])

    def test_record_is_consumed_and_must_have_the_built_in_shape(self):
        calls = ToolMediaCalls()
        calls.observe(self.call("read_media", "id"))
        result = self.result("id", self.frozen)
        self.assertEqual(calls.result_media(result), [self.frozen])
        self.assertEqual(calls.result_media(result), [])
        calls.observe(self.call("read_media", "id"))
        self.assertEqual(calls.result_media({**result, "content": json.dumps({"media": [self.frozen]})}), [])

    def test_harness_marker_reads_structured_inventory_not_summary_text(self):
        row = {**self.frozen, "origin": "tool_result"}
        marker = {"role": "user", "name": "harness_compaction", "retained_media": [row],
                  "content": "Model-written summary containing " + json.dumps([self.other])}
        self.assertEqual(history_media([marker]), [row])
        self.assertEqual(history_media([{**marker, "retained_media": None}]), [])
        self.assertEqual(history_media([{**marker, "role": "assistant"}]), [])
        self.assertEqual(history_media([{**marker, "name": "anything_else"}]), [])

    def test_origins_dedup_and_role_filter(self):
        messages = [
            {"role": "system", "content": "system prompt"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "chart?"},
                    {"type": "image", "media": self.frozen},
                ],
            },
            self.call("render_chart", "render"),
            self.result("render", self.other),
            {"role": "user", "content": [{"type": "image", "media": self.frozen}]},
            "not a message",
            {"role": "user", "content": "plain follow-up"},
        ]

        found = history_media(messages)

        self.assertEqual(
            [(item["media_ref"], item["origin"]) for item in found],
            [(self.frozen["media_ref"], "user_input"), (self.other["media_ref"], "tool_result")],
        )
        self.assertEqual(found[0]["sha256"], self.frozen["sha256"])
        self.assertEqual(found[1]["media_type"], self.other["media_type"])

    def test_empty_history_has_no_media(self):
        self.assertEqual(history_media([]), [])
        self.assertEqual(history_media(None), [])


class ParseImageMarkupTests(unittest.TestCase):
    def test_plain_text_returns_none(self):
        self.assertIsNone(parse_image_markup("plain question about charts"))
        self.assertIsNone(parse_image_markup(""))

    def test_single_image(self):
        self.assertEqual(
            parse_image_markup("[image:charts/a.png]"),
            [{"type": "image", "path": "charts/a.png"}],
        )

    def test_multi_image_with_interleaved_text(self):
        parts = parse_image_markup("[image:a.png] compare with [image:b.png] please")

        self.assertEqual(
            parts,
            [
                {"type": "image", "path": "a.png"},
                {"type": "text", "text": "compare with"},
                {"type": "image", "path": "b.png"},
                {"type": "text", "text": "please"},
            ],
        )

    def test_leading_text_before_image(self):
        self.assertEqual(
            parse_image_markup("look at [image:a.png]"),
            [{"type": "text", "text": "look at"}, {"type": "image", "path": "a.png"}],
        )

    def test_quoted_path_with_spaces_and_chinese(self):
        self.assertEqual(
            parse_image_markup('[image:"我的 图.png"] 两张'),
            [
                {"type": "image", "path": "我的 图.png"},
                {"type": "text", "text": "两张"},
            ],
        )

    def test_single_quoted_path_with_spaces_and_brackets(self):
        self.assertEqual(
            parse_image_markup("[image:'fig [2].png']"),
            [{"type": "image", "path": "fig [2].png"}],
        )

    def test_malformed_path_raises(self):
        for text in ("[image:]", '[image:""]', '[image:"unterminated]', "[image:spaced path.png]", "[image:ok.png] and [image:"):
            with self.subTest(text):
                with self.assertRaises(InvalidInput):
                    parse_image_markup(text)


if __name__ == "__main__":
    unittest.main()