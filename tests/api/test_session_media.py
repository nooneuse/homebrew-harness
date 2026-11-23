"""Session API image input/output (issue #2 images-first).

Covers ordered text+image parts through ``SessionHandle.submit(parts=...)``,
freeze-on-accept identity (later source edits never change an accepted turn),
request-ID deduplication including media content/order, typed media metadata
and export, capability rejection before provider submission, tool-produced
PNG results with native model reinspection, and text-only non-regression.
"""

import base64
import hashlib
import json
import tempfile
import time
from unittest.mock import patch
import unittest
from pathlib import Path

from homebrew_harness import Harness, HostConfig
from homebrew_harness.api.types import InvalidInput
from homebrew_harness.execution.model_protocol import ModelCapabilities
from homebrew_harness.execution.model_registry import ModelRegistry, ModelSpec
from homebrew_harness.media import MediaLimits, MediaStore
from homebrew_harness.runtime.host_commands import HostCommand
from homebrew_harness.runtime.session import HarnessSession
from homebrew_harness.storage.artifact_inventory import inspect_artifacts
from homebrew_harness.storage.durable_runtime import RecoveryRequired
from tests.media.test_store import jpeg_bytes
from homebrew_harness.tools.media import render_bar_chart
from tests.support.clients import call, conversation, fake_client, reply


def png(seed, width=160, height=120):
    return render_bar_chart(
        [f"label {seed}"], [seed + 1], title=f"chart {seed}", width=width, height=height
    )


def text_part(value):
    return {"type": "text", "text": value}


def image_part(path):
    return {"type": "image", "path": str(path)}


def wire_images(messages):
    """Native image_url parts actually sent to the provider, in order."""
    found = []
    for message in conversation(messages):
        content = message.get("content")
        if isinstance(content, list):
            for part in content:
                if part.get("type") == "image_url":
                    url = part["image_url"]["url"]
                    assert url.startswith(("data:image/png;base64,", "data:image/jpeg;base64,"))
                    found.append(base64.b64decode(url.split(",", 1)[1]))
    return found


class SessionMedia(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.workdir = self.root / "work"
        self.workdir.mkdir()

    def host(self, responses, *, image=True, include_text_profile=False):
        client, requests = fake_client(responses)
        client.close = lambda: None
        caps = (
            ModelCapabilities(input_modalities=("text", "image"))
            if image
            else ModelCapabilities()
        )
        fixture = dict(api_key="fixture-key", base_url="http://127.0.0.1:9/v1")
        specs = [ModelSpec(name="fixture", model="fixture-model", capabilities=caps, **fixture)]
        if include_text_profile:
            specs.append(ModelSpec(name="text", model="text-model", **fixture))
        registry = ModelRegistry(specs, lead_default="fixture", client_factory=lambda **_kw: client)
        config = HostConfig.from_components(
            model_registry=registry, shutdown_seconds=3
        )
        host = Harness(workdir=self.workdir, config=config)
        self.addCleanup(host.close)
        return host, client, requests

    def write_png(self, name, seed):
        path = self.workdir / name
        data = png(seed)
        path.write_bytes(data)
        return path, data

    def test_ordered_mixed_parts_transmit_native_images(self):
        first, first_bytes = self.write_png("first.png", 1)
        second = self.workdir / "图表 second.jpg"
        second_bytes = jpeg_bytes()
        second.write_bytes(second_bytes)
        host, client, requests = self.host([reply("I see two charts.")])
        session = host.sessions.open()
        parts = [
            text_part("before"),
            image_part(first),
            text_part("middle"),
            image_part(second),
            text_part("after"),
        ]
        turn = session.submit("describe both", parts=parts, request_id="mixed")
        report = turn.wait(5)
        self.assertTrue(report.condition_met)
        self.assertEqual(report.snapshot.state, "completed")
        self.assertEqual(turn.result().text, "I see two charts.")
        self.assertEqual(client.chat.completions.create.call_count, 1)
        user_rows = [
            m
            for m in conversation(requests[0])
            if m.get("role") == "user" and isinstance(m.get("content"), list)
        ]
        self.assertEqual(len(user_rows), 1)
        content = user_rows[0]["content"]
        self.assertEqual(
            [p["type"] for p in content],
            ["text", "image_url", "text", "image_url", "text"],
        )
        self.assertEqual(
            [p["text"] for p in content if p["type"] == "text"],
            ["before", "middle", "after"],
        )
        self.assertEqual(wire_images(requests[0]), [first_bytes, second_bytes])
        media = session.media()
        self.assertEqual(len(media), 2)
        self.assertEqual([m["origin"] for m in media], ["user_input", "user_input"])
        self.assertEqual([m["status"] for m in media], ["available", "available"])
        self.assertEqual(
            [m["sha256"] for m in media],
            [hashlib.sha256(first_bytes).hexdigest(), hashlib.sha256(second_bytes).hexdigest()],
        )
        self.assertEqual([m["bytes"] for m in media], [len(first_bytes), len(second_bytes)])
        self.assertEqual([m["media_type"] for m in media], ["image/png", "image/jpeg"])

    def test_image_only_submission_is_accepted(self):
        path, data = self.write_png("only.png", 3)
        host, client, requests = self.host([reply("One chart.")])
        session = host.sessions.open()
        turn = session.submit("", parts=[image_part(path)])
        self.assertEqual(turn.wait(5).snapshot.state, "completed")
        self.assertEqual(wire_images(requests[0]), [data])
        self.assertEqual(len(session.media()), 1)

    def test_accepted_media_is_frozen_against_source_edits(self):
        path, data = self.write_png("source.png", 4)
        host, client, requests = self.host([reply("noted"), reply("still noted")])
        session = host.sessions.open()
        turn = session.submit("look", parts=[text_part("look"), image_part(path)])
        self.assertEqual(turn.wait(5).snapshot.state, "completed")
        original_sha = hashlib.sha256(data).hexdigest()
        # Editing and then deleting the original file must not change the
        # accepted turn or its frozen content.
        path.write_bytes(png(99))
        follow = session.submit("what did I show you?")
        self.assertEqual(follow.wait(5).snapshot.state, "completed")
        self.assertEqual(wire_images(requests[-1]), [data])
        path.unlink()
        entry = session.media()[0]
        self.assertEqual(entry["status"], "available")
        self.assertEqual(entry["sha256"], original_sha)
        exported = session.export_media(entry["media_ref"], "exports/kept.png")
        self.assertEqual(exported["sha256"], original_sha)
        self.assertEqual((self.workdir / "exports/kept.png").read_bytes(), data)

    def test_freeze_happens_before_dispatch_and_model_switch_cannot_strand_queue(self):
        path, original = self.write_png("before dispatch.png", 14)
        host, client, requests = self.host([reply("seen")], include_text_profile=True)
        session = host.sessions.open()
        runtime = session.runtime
        runtime._user_waiting.set()
        try:
            turn = session.submit("look", parts=[image_part(path)], request_id="same-path")
            path.write_bytes(png(15))
            with self.assertRaises(InvalidInput):
                session.submit("look", parts=[image_part(path)], request_id="same-path")
            from homebrew_harness.runtime.session_commands import select

            with self.assertRaisesRegex(ValueError, "does not declare image input support"):
                select(runtime, {"profile": "text"})
            self.assertEqual(client.chat.completions.create.call_count, 0)
            path.unlink()
        finally:
            runtime._user_waiting.clear()
        self.assertEqual(turn.wait(5).snapshot.state, "completed")
        self.assertEqual(wire_images(requests[0]), [original])
        with self.assertRaisesRegex(ValueError, "does not declare image input support"):
            runtime.run_user("/model text")

    def test_request_dedup_includes_media_content_and_order(self):
        path_a, _ = self.write_png("a.png", 5)
        path_b, _ = self.write_png("b.png", 6)
        host, client, _ = self.host([reply("once")])
        session = host.sessions.open()
        parts = [text_part("compare"), image_part(path_a)]
        first = session.submit("compare", parts=parts, request_id="dup")
        self.assertEqual(first.wait(5).snapshot.state, "completed")
        replay = session.submit("compare", parts=parts, request_id="dup")
        self.assertEqual(replay.id, first.id)
        self.assertEqual(replay.wait(5).snapshot.state, "completed")
        self.assertEqual(client.chat.completions.create.call_count, 1)
        # Same request ID with different media bytes is a conflict, not a replay.
        with self.assertRaises(ValueError):
            session.submit(
                "compare",
                parts=[text_part("compare"), image_part(path_b)],
                request_id="dup",
            )
        # Same media in a different order is also a distinct payload.
        with self.assertRaises(ValueError):
            session.submit(
                "compare",
                parts=[image_part(path_a), text_part("compare")],
                request_id="dup",
            )
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_text_only_model_rejects_images_before_provider_call(self):
        path, _ = self.write_png("reject.png", 7)
        host, client, _ = self.host([reply("unused")], image=False)
        session = host.sessions.open()
        with self.assertRaisesRegex(InvalidInput, "does not declare image input support"):
            session.submit("look", parts=[text_part("look"), image_part(path)])
        self.assertEqual(client.chat.completions.create.call_count, 0)
        self.assertFalse((self.workdir / ".harness/media").exists())
        self.assertEqual(session.media(), [])

    def test_attachment_errors_are_distinct_and_pre_submission(self):
        host, client, _ = self.host([reply("unused")])
        session = host.sessions.open()
        cases = []
        cases.append(("missing.png", "media_missing"))
        bad = self.workdir / "bad.png"
        bad.write_bytes(b"this is not an image")
        cases.append(("bad.png", "media_format_invalid"))
        outside = self.root / "outside.png"
        outside.write_bytes(png(8))
        cases.append(("../outside.png", "media_path_unauthorized"))
        oversized = self.workdir / "big.png"
        oversized.write_bytes(
            b"\x89PNG\r\n\x1a\n" + b"\x00" * (MediaLimits().max_image_bytes + 1)
        )
        cases.append(("big.png", "media_bytes_limit"))
        for name, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(InvalidInput) as caught:
                    session.submit("look", parts=[image_part(name)])
                self.assertEqual(caught.exception.details["code"], code)
        self.assertEqual(client.chat.completions.create.call_count, 0)
        self.assertEqual(session.media(), [])

    def test_media_metadata_and_export_boundaries(self):
        path, data = self.write_png("meta.png", 9)
        host, _, _ = self.host([reply("ok")])
        session = host.sessions.open()
        turn = session.submit("look", parts=[image_part(path)])
        self.assertEqual(turn.wait(5).snapshot.state, "completed")
        entry = session.media()[0]
        ref = entry["media_ref"]
        meta = session.media_metadata(ref)
        self.assertEqual(meta["reference"], ref)
        self.assertEqual(meta["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(meta["bytes"], len(data))
        self.assertEqual(meta["media_type"], "image/png")
        self.assertEqual((meta["width"], meta["height"]), (160, 120))
        exported = session.export_media(ref, "exports/chart.png")
        self.assertEqual(exported["path"], "exports/chart.png")
        self.assertEqual((self.workdir / "exports/chart.png").read_bytes(), data)
        with self.assertRaises(InvalidInput) as caught:
            session.export_media(ref, "exports/chart.png")
        self.assertEqual(caught.exception.details["code"], "media_export_exists")
        with self.assertRaises(InvalidInput) as caught:
            session.export_media(ref, "../escape.png")
        self.assertEqual(caught.exception.details["code"], "media_export_unauthorized")
        with self.assertRaises(InvalidInput) as caught:
            session.export_media(ref, ".harness/evil.png")
        self.assertEqual(caught.exception.details["code"], "media_export_unauthorized")
        with self.assertRaises(InvalidInput) as caught:
            session.media_metadata("not-a-reference")
        self.assertEqual(caught.exception.details["code"], "media_reference_invalid")

    def test_tool_png_result_metadata_and_native_reinspection(self):
        host, client, requests = self.host(
            [
                reply(
                    calls=[
                        call(
                            "c1",
                            name="render_chart",
                            arguments=json.dumps(
                                {
                                    "labels": ["alpha", "beta"],
                                    "values": [3, 7],
                                    "title": "demo",
                                }
                            ),
                        )
                    ]
                ),
                reply("Two bars."),
            ]
        )
        session = host.sessions.open()
        turn = session.submit("Render a demo chart")
        self.assertEqual(turn.wait(5).snapshot.state, "completed")
        self.assertEqual(turn.result().text, "Two bars.")
        media = session.media()
        tool_media = [m for m in media if m["origin"] == "tool_result"]
        self.assertEqual(len(tool_media), 1)
        entry = tool_media[0]
        self.assertEqual(entry["status"], "available")
        self.assertEqual(entry["media_type"], "image/png")
        frozen = (self.workdir / entry["media_ref"]).read_bytes()
        self.assertEqual(entry["sha256"], hashlib.sha256(frozen).hexdigest())
        self.assertEqual(entry["bytes"], len(frozen))
        # The follow-up model request reinspects the artifact as native image
        # content, never as a filename or byte text.
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertEqual(wire_images(requests[1]), [frozen])
        tool_media_rows = [
            m
            for m in conversation(requests[1])
            if m.get("role") == "user" and m.get("name") == "tool_media"
        ]
        self.assertEqual(len(tool_media_rows), 1)
        exported = session.export_media(entry["media_ref"], "exports/tool.png")
        self.assertEqual((self.workdir / exported["path"]).read_bytes(), frozen)

    def test_terminal_markup_and_typed_media_commands(self):
        _, data = self.write_png("quoted [图] image.png", 10)
        host, _, requests = self.host([reply("seen")])
        runtime = host.sessions.open().runtime
        self.assertEqual(runtime.run_user('Before [image:"quoted [图] image.png"] after'), "seen")
        ref = json.loads(runtime.run_user("/media list"))["media"][0]["media_ref"]
        self.assertEqual(json.loads(runtime.run_user(f"/media show {ref}"))["bytes"], len(data))
        runtime.run_user(f'/media export {ref} "exports/terminal image.png"')
        self.assertEqual((self.workdir / "exports/terminal image.png").read_bytes(), data)
        self.assertEqual(wire_images(requests[0]), [data])
        with self.assertRaisesRegex(ValueError, "standalone Session"):
            HostCommand.parse("/media list")
        with self.assertRaisesRegex(ValueError, "host command"):
            runtime.run_user("/goal inspect [image:unreachable.png]")

    def test_boundaries_reject_exact_foreign_refs_and_worktree_paths(self):
        path, data = self.write_png("ownership.png", 11)
        host, client, _ = self.host([reply("unused")])
        session = host.sessions.open()
        frozen = MediaStore(self.workdir).freeze(data)
        ref = frozen["media_ref"]
        with self.assertRaises(InvalidInput) as caught:
            session.media_metadata(ref)
        self.assertEqual(caught.exception.details["code"], "media_reference_unowned")
        with self.assertRaises(InvalidInput) as caught:
            session.submit("", parts=[{"type": "image", "media": frozen}])
        self.assertEqual(caught.exception.details["code"], "media_reference_invalid")
        with self.assertRaises(InvalidInput) as caught:
            session.submit("", parts=[image_part(ref)])
        self.assertEqual(caught.exception.details["code"], "media_path_unauthorized")
        branch = self.root / "assigned-worktree"
        branch.mkdir()
        with patch.object(session.runtime.workspace, "lead_cwd", return_value=branch):
            with self.assertRaises(InvalidInput) as caught:
                session.submit("", parts=[image_part(path)])
        self.assertEqual(caught.exception.details["code"], "media_mode_unsupported")
        self.assertEqual(client.chat.completions.create.call_count, 0)

    def test_planning_rejects_image_admission_and_sdk_text_remains_literal(self):
        path, _ = self.write_png("plan.png", 12)
        host, client, requests = self.host([reply("literal")])
        session = host.sessions.open()
        session.runtime.run_user("/plan on")
        with self.assertRaises(InvalidInput) as caught:
            session.submit("", parts=[image_part(path)])
        self.assertEqual(caught.exception.details["code"], "media_mode_unsupported")
        self.assertEqual(client.chat.completions.create.call_count, 0)
        session.runtime.run_user("/plan off")
        self.assertEqual(session.submit("Explain [image:markup]").wait(5).snapshot.state, "completed")
        self.assertFalse(wire_images(requests[0]))
        self.assertFalse((self.workdir / ".harness/media").exists())

    def test_gc_and_resumed_session_keep_the_frozen_source(self):
        path, data = self.write_png("retention.png", 13)
        host, _, _ = self.host([reply("ack")])
        session = host.sessions.open()
        self.assertEqual(session.submit("", parts=[image_part(path)]).wait(5).snapshot.state, "completed")
        sid, ref = session.id, session.media()[0]["media_ref"]
        orphan = MediaStore(self.workdir).freeze(png(77))["media_ref"]
        path.unlink()
        host.close()
        report = inspect_artifacts(self.workdir, retention_days=1, now=time.time() + 3 * 86400)
        self.assertIn(ref, report["references"])
        self.assertEqual({m["path"] for m in report["candidates"] if m["path"].startswith(".harness/media/")}, {orphan})
        without_media, _ = fake_client([])
        with HarnessSession(without_media, "unused", self.workdir, resume_session=sid) as runtime:
            with self.assertRaisesRegex(RecoveryRequired, "accepted images"):
                runtime._ensure_runtime()
        other, _, requests = self.host([reply("still here")])
        resumed = other.sessions.resume(sid)
        self.assertEqual(resumed.media()[0]["media_ref"], ref)
        self.assertEqual(resumed.submit("what's in the image?").wait(5).snapshot.state, "completed")
        self.assertEqual(wire_images(requests[0]), [data])

    def test_count_limit_rejects_before_reading_any_attachments(self):
        host, client, _ = self.host([reply("unused")])
        session = host.sessions.open()
        parts = [image_part("missing.png")] * (MediaLimits().max_images_per_request + 1)
        with self.assertRaises(InvalidInput) as caught:
            session.submit("", parts=parts)
        self.assertEqual(caught.exception.details["code"], "media_request_count_limit")
        self.assertFalse((self.workdir / ".harness/media").exists())
        self.assertEqual(client.chat.completions.create.call_count, 0)

    def test_plain_text_submission_is_unchanged(self):
        host, client, requests = self.host([reply("plain ok")])
        session = host.sessions.open()
        turn = session.submit("hello")
        self.assertEqual(turn.wait(5).snapshot.state, "completed")
        self.assertEqual(turn.result().text, "plain ok")
        user_rows = [
            m for m in conversation(requests[0]) if m.get("role") == "user"
        ]
        self.assertTrue(all(isinstance(m.get("content"), str) for m in user_rows))
        self.assertTrue(any(m.get("content") == "hello" for m in user_rows))
        self.assertEqual(session.media(), [])

    def test_text_only_parts_must_use_plain_text(self):
        host, client, _ = self.host([reply("unused")])
        session = host.sessions.open()
        with self.assertRaises(ValueError):
            session.submit("hi", parts=[text_part("hi")])
        self.assertEqual(client.chat.completions.create.call_count, 0)


if __name__ == "__main__":
    unittest.main()
