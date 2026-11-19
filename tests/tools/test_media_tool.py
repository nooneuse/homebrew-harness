"""Tests for the registered render_chart tool: frozen PNG media results."""

import hashlib
import json
import stat
import tempfile
import unittest
from pathlib import Path

from homebrew_harness.media import MediaStore, validate_image
from homebrew_harness.tools.catalog import TOOL_SCHEMAS
from homebrew_harness.tools.media import (
    MAX_BARS,
    MAX_LABEL_CHARS,
    MAX_TITLE_CHARS,
    MAX_VALUE,
    render_bar_chart,
    run_render_chart,
)
from homebrew_harness.tools.registry import TOOL_HANDLERS


class RenderChartToolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def render(self, labels=None, values=None, **kwargs):
        labels = ["alpha", "beta"] if labels is None else labels
        values = [1, 2] if values is None else values
        return json.loads(run_render_chart(labels, values, self.root, **kwargs))

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
            run_render_chart("alpha", [1], self.root)
        with self.assertRaisesRegex(ValueError, "arrays"):
            run_render_chart(["alpha"], "1", self.root)

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
        self.assertIs(TOOL_HANDLERS["render_chart"], run_render_chart)
        schema = TOOL_SCHEMAS["render_chart"]
        self.assertEqual(schema["required"], ["labels", "values"])
        props = schema["properties"]
        self.assertEqual(props["labels"]["maxItems"], MAX_BARS)
        self.assertEqual(props["labels"]["items"]["maxLength"], MAX_LABEL_CHARS)
        self.assertEqual(props["values"]["items"]["maximum"], MAX_VALUE)
        self.assertEqual(props["title"]["maxLength"], MAX_TITLE_CHARS)
        self.assertEqual(props["width"]["minimum"], 160)
        self.assertEqual(props["height"]["maximum"], 2048)


if __name__ == "__main__":
    unittest.main()
