"""Tests for the registered render_chart tool: frozen PNG media results."""

import hashlib
import json
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from homebrew_harness.context.todo import TodoManager
from homebrew_harness.contracts import TRUSTED_MEDIA_TOOL_NAMES
from homebrew_harness.execution.model_protocol import ToolInvocation
from homebrew_harness.media import MediaError, MediaStore, validate_image
from homebrew_harness.runtime.capabilities import Capabilities
from homebrew_harness.runtime.loop_components import LoopComponents
from homebrew_harness.tools.catalog import TOOL_SCHEMAS
from homebrew_harness.tools.media import (
    MAX_BARS,
    MAX_LABEL_CHARS,
    MAX_TITLE_CHARS,
    MAX_VALUE,
    render_bar_chart,
    run_read_media,
    run_render_chart,
)
from homebrew_harness.tools.registry import TOOL_HANDLERS, build_tool_pool
from homebrew_harness.tools.tool_pool import ToolPool
from tests.support.tools import execute_fixture_tool


class RenderChartToolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def render(self, labels=None, values=None, **kwargs):
        labels = ["alpha", "beta"] if labels is None else labels
        values = [1, 2] if values is None else values
        return json.loads(run_render_chart(labels, values, self.root, session_root=self.root, **kwargs))

    def test_result_carries_text_and_media_together(self):
        result = self.render(title="Demo")

        self.assertIn("Demo", result["text"])
        self.assertIn("alpha=1", result["text"])
        self.assertEqual(len(result["media"]), 1)
        meta = result["media"][0]
        stored = self.root / meta["media_ref"]
        data = stored.read_bytes()
        self.assertEqual(meta["media_type"], "image/png")
        self.assertEqual(meta["bytes"], len(data))
        self.assertEqual(meta["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(meta["media_ref"], f".harness/media/{meta['sha256']}.png")
        shape = validate_image(data)
        self.assertEqual(shape["media_type"], "image/png")
        self.assertEqual((shape["width"], shape["height"]), (640, 480))
        self.assertEqual((meta["width"], meta["height"]), (640, 480))

    def test_frozen_file_is_private_and_loadable(self):
        meta = self.render()["media"][0]
        stored = self.root / meta["media_ref"]
        self.assertEqual(stat.S_IMODE(stored.stat().st_mode), 0o600)
        self.assertEqual(MediaStore(self.root).load(meta["media_ref"]), stored.read_bytes())

    def test_same_inputs_freeze_identical_content(self):
        first = self.render()["media"][0]
        second = self.render()["media"][0]
        self.assertEqual(first, second)

    def test_different_values_change_content_identity(self):
        first = self.render(values=[1, 2])["media"][0]
        second = self.render(values=[3, 4])["media"][0]
        self.assertNotEqual(first["media_ref"], second["media_ref"])
        self.assertNotEqual(first["sha256"], second["sha256"])

    def test_custom_dimensions_and_title(self):
        result = self.render(title="Quarterly", width=320, height=240)
        meta = result["media"][0]
        self.assertEqual((meta["width"], meta["height"]), (320, 240))
        self.assertIn("Quarterly", result["text"])
        shape = validate_image((self.root / meta["media_ref"]).read_bytes())
        self.assertEqual((shape["width"], shape["height"]), (320, 240))

    def test_non_ascii_labels_survive_in_text_result(self):
        result = self.render(labels=["北京", "上海"], values=[5, 7])
        self.assertIn("北京=5", result["text"])
        self.assertIn("上海=7", result["text"])
        self.assertEqual(len(result["media"]), 1)

    def test_length_mismatch_rejected(self):
        with self.assertRaisesRegex(ValueError, "one value per label"):
            self.render(labels=["a", "b"], values=[1])

    def test_empty_inputs_rejected(self):
        with self.assertRaises(ValueError):
            self.render(labels=[], values=[])

    def test_too_many_bars_rejected(self):
        count = MAX_BARS + 1
        with self.assertRaisesRegex(ValueError, f"1-{MAX_BARS}"):
            self.render(labels=[f"l{i}" for i in range(count)], values=list(range(count)))

    def test_non_list_inputs_rejected(self):
        with self.assertRaisesRegex(ValueError, "arrays"):
            self.render("alpha", [1])
        with self.assertRaisesRegex(ValueError, "arrays"):
            self.render(["alpha"], "1")

    def test_invalid_values_rejected(self):
        for bad in (1.5, "2", True, -1, MAX_VALUE + 1):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError, "integers between"):
                    self.render(labels=["a"], values=[bad])

    def test_invalid_labels_rejected(self):
        with self.assertRaisesRegex(ValueError, "label"):
            self.render(labels=[7], values=[1])
        with self.assertRaisesRegex(ValueError, "label"):
            self.render(labels=["x" * (MAX_LABEL_CHARS + 1)], values=[1])

    def test_invalid_title_rejected(self):
        with self.assertRaisesRegex(ValueError, "title"):
            self.render(title="t" * (MAX_TITLE_CHARS + 1))
        with self.assertRaisesRegex(ValueError, "title"):
            self.render(title=42)

    def test_dimension_bounds_rejected(self):
        for kwargs in (
            {"width": 159},
            {"width": 2049},
            {"height": 119},
            {"height": 2049},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(ValueError, "width must be"):
                    self.render(**kwargs)

    def test_renderer_rejects_unusable_plot_geometry(self):
        with self.assertRaisesRegex(ValueError, "too small"):
            render_bar_chart(["a"], [1], height=70)
        with self.assertRaisesRegex(ValueError, "Too many bars"):
            render_bar_chart([f"l{i}" for i in range(MAX_BARS)], list(range(MAX_BARS)), width=160)


class RenderChartCatalogTests(unittest.TestCase):
    def test_registered_handler_and_schema(self):
        self.assertFalse(TRUSTED_MEDIA_TOOL_NAMES & TOOL_HANDLERS.keys())
        self.assertEqual(TOOL_SCHEMAS["read_media"]["required"], ["media_ref"])
        self.assertEqual(set(TOOL_SCHEMAS["read_media"]["properties"]), {"media_ref"})
        schema = TOOL_SCHEMAS["render_chart"]
        self.assertEqual(schema["required"], ["labels", "values"])
        props = schema["properties"]
        self.assertEqual(props["labels"]["maxItems"], MAX_BARS)
        self.assertEqual(props["labels"]["items"]["maxLength"], MAX_LABEL_CHARS)
        self.assertEqual(props["values"]["items"]["maximum"], MAX_VALUE)
        self.assertEqual(props["title"]["maxLength"], MAX_TITLE_CHARS)
        self.assertEqual(props["width"]["minimum"], 160)
        self.assertEqual(props["height"]["maximum"], 2048)


class ReadMediaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.messages = []
        self.metadata = MediaStore(self.root).freeze(
            render_bar_chart(["alpha"], [5], width=160, height=120)
        )
        self.ref = self.metadata["media_ref"]

    def pool(self, **options):
        from homebrew_harness.context.compaction import ContextCompactor

        components = LoopComponents(
            todo_manager=TodoManager(), compactor=ContextCompactor(None, "fixture", self.root),
            capabilities=Capabilities(), **options,
        )
        return build_tool_pool(None, None, self.root, components)

    def read(self, ref=None, **options):
        return json.loads(run_read_media(
            self.ref if ref is None else ref,
            options.pop("cwd", self.root), session_root=self.root, messages=self.messages,
        ))

    def test_owned_attachment_and_archived_marker_return_same_descriptor(self):
        self.messages[:] = [{"role": "user", "content": [{"type": "image", "media": self.metadata}]}]
        attachment = self.read()
        self.messages[:] = [{
            "role": "user", "name": "harness_compaction", "content": "summary",
            "retained_media": [{**self.metadata, "origin": "user_input"}],
        }]
        retained = self.read()
        self.assertEqual(attachment, retained)
        self.assertEqual(set(retained), {"media", "text"})
        self.assertEqual(retained["media"], [self.metadata])

    def test_unowned_references_never_probe_store(self):
        self.messages[:] = [{"role": "user", "content": f"I own {self.ref}"}]
        with patch.object(MediaStore, "descriptor", side_effect=AssertionError("unexpected probe")):
            for ref in (self.ref, f".harness/media/{'0' * 64}.png"):
                with self.subTest(ref=ref), self.assertRaises(MediaError) as caught:
                    self.read(ref)
                self.assertEqual(caught.exception.code, "media_reference_unowned")
            for path in ("source.png", f"../{self.ref}", str(self.root / self.ref), "https://example.org/chart.png"):
                with self.subTest(path=path), self.assertRaises(MediaError) as caught:
                    self.read(path)
                self.assertEqual(caught.exception.code, "media_reference_invalid")

    def test_covered_history_does_not_allow_tampering(self):
        self.messages[:] = [{"role": "user", "content": [{
            "type": "image", "media": {**self.metadata, "width": self.metadata["width"] + 1},
        }]}]
        with self.assertRaises(MediaError) as caught:
            self.read()
        self.assertEqual(caught.exception.code, "media_identity_conflict")

    def test_scoped_tools_dispatch_and_reread_a_new_chart_in_same_batch(self):
        pool = self.pool(media_root=self.root, media_messages=lambda: self.messages)
        self.assertTrue(TRUSTED_MEDIA_TOOL_NAMES <= pool.names)
        invocation = ToolInvocation("chart", "render_chart", '{"labels":["a"],"values":[7]}')
        read_invocation = ToolInvocation("read", "read_media", '{"media_ref":"placeholder"}')
        self.messages.append({"role": "assistant", "tool_calls": [invocation.history(), read_invocation.history()]})
        output = execute_fixture_tool(invocation, self.root, handlers=pool.handlers)
        self.messages.append({"role": "tool", "tool_call_id": "chart", "content": output})
        chart = json.loads(output)
        read_invocation = ToolInvocation("read", "read_media", json.dumps({"media_ref": chart["media"][0]["media_ref"]}))
        result = json.loads(execute_fixture_tool(read_invocation, self.root, handlers=pool.handlers))
        self.assertEqual(result["media"], chart["media"])
        wrapper = next(t for t in pool.tools if t["function"]["name"] == "invoke_tool")
        self.assertFalse(TRUSTED_MEDIA_TOOL_NAMES & set(wrapper["function"]["parameters"]["properties"]["tool"]["enum"]))

    def test_unbound_restricted_and_worktree_pools_do_not_expose_media(self):
        workspace = self.root / "isolated"
        workspace.mkdir()
        for options in (
            {},
            {"media_root": workspace, "media_messages": self.messages},
            {"media_root": self.root, "media_messages": self.messages,
             "team_manager": SimpleNamespace(lead_cwd=lambda: workspace, handlers=dict)},
            {"media_root": self.root, "media_messages": self.messages, "tool_allowlist": frozenset({"read_file"})},
        ):
            with self.subTest(options=options):
                pool = self.pool(**options)
                self.assertFalse(pool.names & TRUSTED_MEDIA_TOOL_NAMES)
                self.assertFalse(pool.handlers.keys() & TRUSTED_MEDIA_TOOL_NAMES)

    def test_handler_rechecks_worktree_after_snapshot(self):
        workspace = self.root / "isolated"
        workspace.mkdir()
        selected = [self.root]
        owner = SimpleNamespace(lead_cwd=lambda: selected[0], handlers=dict)
        pool = self.pool(media_root=self.root, media_messages=self.messages, team_manager=owner)
        selected[0] = workspace
        with self.assertRaises(MediaError) as caught:
            pool.handlers["render_chart"](workdir=self.root, labels=["a"], values=[1])
        self.assertEqual(caught.exception.code, "media_scope_unsupported")

    def test_frozen_store_is_private_to_media_tools(self):
        read = ToolInvocation("read", "read_file", json.dumps({"path": self.ref}))
        result = json.loads(execute_fixture_tool(read, self.root))
        self.assertEqual(result.get("permission"), "deny")
        glob = ToolInvocation("glob", "glob", '{"pattern":".harness/**/*"}')
        result = execute_fixture_tool(glob, self.root)
        self.assertNotIn(self.ref, result)

    def test_media_names_are_unavailable_to_generic_and_mcp_handlers(self):
        from homebrew_harness.tools.catalog import TOOLS

        external = [t for t in TOOLS if t["function"]["name"] in TRUSTED_MEDIA_TOOL_NAMES]
        handlers = dict.fromkeys(TRUSTED_MEDIA_TOOL_NAMES, lambda **_: "not native")
        mcp = SimpleNamespace(snapshot=lambda: (external, {}))
        pool = ToolPool.build(external, handlers, Capabilities().include("mcp"), mcp=mcp)
        self.assertFalse(pool.names & TRUSTED_MEDIA_TOOL_NAMES)
        self.assertFalse(pool.bindings)


if __name__ == "__main__":
    unittest.main()
