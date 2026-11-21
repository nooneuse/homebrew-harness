"""Saved file-output views preserve content, ownership and source coordinates."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from homebrew_harness.context.compaction import ArchiveStore, ContextCompactor
from homebrew_harness.execution.model_protocol import ToolInvocation
from homebrew_harness.security.file_visibility import file_access_scope
from homebrew_harness.tools.files import run_read, OUTPUT_CHAR_LIMIT
from tests.support.clients import call
from tests.support.tools import execute_fixture_tool


class SavedOutputTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.store = ArchiveStore(self.root, namespace="lead-test")
        self.text = ('quote " slash \\ 你好\r\n' * 700)
        (self.root / "source.py").write_text(self.text, encoding="utf-8", newline="")
        self.source = json.loads(run_read("source.py", self.root, limit=15))
        self.reference = self.store.save("tool-results", json.dumps(self.source, ensure_ascii=False))

    def read(self, reference=None, **kw):
        with file_access_scope(self.store):
            result = run_read(reference or self.reference, self.root, **kw)
        self.assertLessEqual(len(result), OUTPUT_CHAR_LIMIT)
        return json.loads(result)

    def test_owned_view_contains_exact_code_and_separate_recorded_source(self):
        raw = (self.root / self.reference).read_bytes()
        result = self.read()
        self.assertEqual(result["view"], "recorded_file_content")
        self.assertEqual(result["content"], self.source["content"])
        self.assertEqual(result["source"]["path"], "source.py")
        self.assertEqual(result["source"]["version"], self.source["version"])
        self.assertEqual(result["source"]["offset"], self.source["offset"])
        self.assertEqual(result["source"]["next_offset"], self.source["next_offset"])
        self.assertEqual((self.root / self.reference).read_bytes(), raw)
        literal = self.read(raw=True)
        self.assertNotIn("view", literal)
        self.assertEqual(json.loads(literal["content"]), self.source)

    def test_legacy_nested_reads_follow_owned_references_without_more_encoding(self):
        reference = self.reference
        original_hash = hashlib.sha256((self.root / reference).read_bytes()).hexdigest()
        for _ in range(5):
            wrapped = self.read(reference, raw=True)
            reference = self.store.save("tool-results", json.dumps(wrapped, ensure_ascii=False))
        result = self.read(reference)
        self.assertEqual(result["content"], self.source["content"])
        self.assertEqual(result["source"]["path"], "source.py")
        self.assertEqual(hashlib.sha256((self.root / self.reference).read_bytes()).hexdigest(), original_hash)

    def test_rearchived_views_remain_plain_content_under_context_pressure(self):
        compactor = ContextCompactor(None, "fixture", self.root, archive_namespace="lead-test")
        compactor.store = self.store
        reference = self.reference
        for _ in range(6):
            result = self.read(reference)
            self.assertEqual(result["content"], self.source["content"])
            marker = compactor.persisted_preview(json.dumps(result, ensure_ascii=False), preview_chars=20)
            reference = marker.split("Full output: ", 1)[1].split("\n", 1)[0]
        self.assertEqual(self.read(reference)["source"]["path"], "source.py")

    def test_paged_view_preserves_unicode_and_actual_source_offsets(self):
        content = self.text
        source = {**self.source, "offset": 900, "content": content, "next_offset": 900 + len(content), "truncated": True}
        reference = self.store.save("tool-results", json.dumps(source, ensure_ascii=False))
        parts = [];offset = 0;version = None
        while offset is not None:
            result = self.read(reference, offset=offset, expected_version=version)
            version = result["version"]
            self.assertEqual(result["source"]["offset"], 900 + offset)
            self.assertEqual(result["source"]["next_offset"], 900 + offset + len(result["content"]))
            parts.append(result["content"])
            next_offset = result["next_offset"]
            if next_offset is not None:
                self.assertGreater(next_offset, offset)
            offset = next_offset
        self.assertEqual("".join(parts), content)

    def test_ordinary_json_and_unowned_archives_remain_literal(self):
        encoded = json.dumps(self.source, ensure_ascii=False)
        (self.root / "ordinary.json").write_text(encoded)
        self.assertEqual(json.loads(run_read("ordinary.json", self.root))["content"], encoded)
        foreign = ArchiveStore(self.root, namespace="foreign").save("tool-results", encoded)
        self.assertNotIn("view", self.read(foreign))
        with file_access_scope(self.store, restrict_archives=True):
            with self.assertRaises(ValueError):
                run_read(foreign, self.root)

    def test_inherited_archive_uses_the_same_dispatch_boundary(self):
        child = ArchiveStore(self.root, namespace="child")
        child.inherit_checkpoint(self.store.checkpoint())
        result = execute_fixture_tool(ToolInvocation.from_history(call("read", name="read_file", arguments=json.dumps({"path": self.reference}))),
                                      self.root, archive_store=child, restrict_archives=True)
        self.assertEqual(json.loads(result)["content"], self.source["content"])
        literal = execute_fixture_tool(ToolInvocation.from_history(call("raw", name="read_file", arguments=json.dumps({"path": self.reference, "raw": True}))),
                                       self.root, archive_store=child, restrict_archives=True)
        self.assertNotIn("view", json.loads(literal))

    def test_changed_linked_archive_returns_version_error_before_content(self):
        wrapper = self.read(raw=True)
        reference = self.store.save("tool-results", json.dumps(wrapper, ensure_ascii=False))
        (self.root / self.reference).write_text("changed")
        with self.assertRaises(ValueError) as error:
            self.read(reference)
        self.assertIn("changed", str(error.exception))

    def test_cycles_and_excessive_link_depth_return_errors_with_raw_fallback(self):
        cycle = self.store.save("tool-results", "placeholder")
        value = {**self.source, "path": cycle}
        (self.root / cycle).write_text(json.dumps(value, ensure_ascii=False))
        with self.assertRaisesRegex(ValueError, "cycle"):
            self.read(cycle)
        self.assertNotIn("view", self.read(cycle, raw=True))
        reference = self.reference
        for _ in range(9):
            wrapper = self.read(reference, raw=True)
            reference = self.store.save("tool-results", json.dumps(wrapper, ensure_ascii=False))
        with self.assertRaisesRegex(ValueError, "nesting"):
            self.read(reference)

    def test_source_json_with_file_response_keys_is_not_recursively_decoded(self):
        encoded = json.dumps(self.source, ensure_ascii=False)
        (self.root / "source.json").write_text(encoded)
        source_read = json.loads(run_read("source.json", self.root))
        reference = self.store.save("tool-results", json.dumps(source_read, ensure_ascii=False))
        self.assertEqual(self.read(reference)["content"], encoded)

    def test_nested_reads_of_plain_tool_output_do_not_wrap_its_json(self):
        output = "command output\n" * 1000
        reference = self.store.save("tool-results", output)
        wrapper = self.read(reference, raw=True)
        reference = self.store.save("tool-results", json.dumps(wrapper, ensure_ascii=False))
        result = self.read(reference, limit=3)
        self.assertEqual(result["view"], "recorded_tool_output")
        self.assertEqual(result["content"], "command output\n" * 3)
