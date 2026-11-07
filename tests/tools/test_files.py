"""File-tool behavior and workspace boundaries, using temporary files only."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.support.tools import execute_fixture_tool
import homebrew_harness.tools.registry


class FileToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.workdir = self.root / "workspace"
        self.workdir.mkdir()
        self.outside = self.root / "workspace-peer"
        self.outside.mkdir()
        (self.outside / "outside.txt").write_text("keep me", encoding="utf-8")

    def invoke(self, name, **arguments):
        call = SimpleNamespace(
            type="function",
            function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
        )
        return json.loads(execute_fixture_tool(call, self.workdir))

    def test_utf8_round_trip_with_nested_paths_and_shell_metacharacters(self):
        name = "nested/quoted ' $(touch marker).txt"
        content = "你好\n'quotes' $HOME; echo no\r\n"
        written = self.invoke("write_file", path=name, content=content)
        self.assertEqual(written["bytes_written"], len(content.encode("utf-8")))
        self.assertEqual(written["path"], name)
        result = self.invoke("read_file", path=name)
        self.assertEqual(result["content"], content)
        self.assertFalse(result["truncated"])
        self.assertFalse((self.workdir / "marker").exists())

    def test_write_can_overwrite_and_create_empty_files(self):
        self.invoke("write_file", path="note.txt", content="original")
        self.invoke("write_file", path="note.txt", content="replacement")
        self.assertEqual((self.workdir / "note.txt").read_text(), "replacement")
        self.invoke("write_file", path="note.txt", content="")
        self.assertEqual((self.workdir / "note.txt").read_bytes(), b"")

    def test_read_line_limit_reports_omitted_content(self):
        self.invoke("write_file", path="lines.txt", content="one\r\ntwo\r\nthree")
        result = self.invoke("read_file", path="lines.txt", limit=1)
        self.assertEqual(result["content"], "one\r\n")
        self.assertTrue(result["truncated"])
        self.assertFalse(
            self.invoke("read_file", path="lines.txt", limit=3)["truncated"]
        )

    def test_read_character_budget_reports_truncation(self):
        self.invoke("write_file", path="long.txt", content="x" * 10_001)
        result = self.invoke("read_file", path="long.txt")
        self.assertEqual(len(result["content"]), 10_000)
        self.assertTrue(result["truncated"])
        self.invoke("write_file", path="exact.txt", content="x" * 10_000)
        self.assertFalse(self.invoke("read_file", path="exact.txt")["truncated"])

    def test_edit_replaces_first_exact_match_and_preserves_line_endings(self):
        original = "你好\r\nold old\r\n"
        self.invoke("write_file", path="edit.txt", content=original)
        result = self.invoke(
            "edit_file", path="edit.txt", old_text="old", new_text="new"
        )
        self.assertEqual(result["replacements"], 1)
        self.assertEqual(
            (self.workdir / "edit.txt").read_bytes(),
            "你好\r\nnew old\r\n".encode("utf-8"),
        )

    def test_edit_missing_match_or_empty_search_does_not_change_file(self):
        self.invoke("write_file", path="edit.txt", content="unchanged")
        for old in ("missing", ""):
            with self.subTest(old=old):
                self.assertIn(
                    "error",
                    self.invoke(
                        "edit_file", path="edit.txt", old_text=old, new_text="new"
                    ),
                )
                self.assertEqual((self.workdir / "edit.txt").read_text(), "unchanged")

    def test_edit_allows_whitespace_search_and_empty_replacement(self):
        self.invoke("write_file", path="edit.txt", content="a b")
        self.assertNotIn(
            "error",
            self.invoke("edit_file", path="edit.txt", old_text=" ", new_text=""),
        )
        self.assertEqual((self.workdir / "edit.txt").read_text(), "ab")

    def test_absolute_paths_inside_workdir_are_accepted(self):
        path = str(self.workdir / "absolute.txt")
        self.assertNotIn(
            "error", self.invoke("write_file", path=path, content="inside")
        )
        self.assertEqual(self.invoke("read_file", path=path)["content"], "inside")

    def test_parent_traversal_and_absolute_sibling_paths_are_rejected(self):
        for path in (
            "../workspace-peer/outside.txt",
            str(self.outside / "outside.txt"),
        ):
            for name, args in (
                ("read_file", {}),
                ("write_file", {"content": "changed"}),
                ("edit_file", {"old_text": "keep", "new_text": "changed"}),
            ):
                with self.subTest(path=path, tool=name):
                    self.assertIn("error", self.invoke(name, path=path, **args))
        self.assertEqual((self.outside / "outside.txt").read_text(), "keep me")
        self.assertIn(
            "error",
            self.invoke("write_file", path="../new-directory/new.txt", content="x"),
        )
        self.assertFalse((self.root / "new-directory").exists())

    def test_symlinks_to_outside_files_and_directories_are_rejected(self):
        (self.workdir / "link.txt").symlink_to(self.outside / "outside.txt")
        (self.workdir / "linked").symlink_to(self.outside, target_is_directory=True)
        for path in ("link.txt", "linked/outside.txt"):
            for name, args in (
                ("read_file", {}),
                ("write_file", {"content": "changed"}),
                ("edit_file", {"old_text": "keep", "new_text": "changed"}),
            ):
                with self.subTest(path=path, tool=name):
                    self.assertIn("error", self.invoke(name, path=path, **args))
        self.assertIn(
            "error", self.invoke("write_file", path="linked/new.txt", content="x")
        )
        self.assertFalse((self.outside / "new.txt").exists())
        self.assertEqual((self.outside / "outside.txt").read_text(), "keep me")

    def test_internal_symlink_is_rejected_by_no_follow_policy(self):
        self.invoke("write_file", path="inside.txt", content="inside")
        (self.workdir / "alias.txt").symlink_to(self.workdir / "inside.txt")
        self.assertIn("error", self.invoke("read_file", path="alias.txt"))

    def test_glob_is_recursive_sorted_file_only_and_handles_no_matches(self):
        for path in ("z.py", "src/b.py", "a.py", "src/note.txt"):
            self.invoke("write_file", path=path, content="")
        (self.workdir / "directory.py").mkdir()
        result = self.invoke("glob", pattern="**/*.py")
        self.assertEqual(result["paths"], ["a.py", "src/b.py", "z.py"])
        self.assertFalse(result["truncated"])
        self.assertEqual(self.invoke("glob", pattern="**/*.missing")["paths"], [])

    def test_glob_rejects_escape_patterns_and_filters_external_symlinks(self):
        (self.workdir / "linked").symlink_to(self.outside, target_is_directory=True)
        (self.workdir / "outside.txt").symlink_to(self.outside / "outside.txt")
        for pattern in ("../**/*", str(self.outside / "*.txt"), "linked/*.txt"):
            with self.subTest(pattern=pattern):
                self.assertIn("error", self.invoke("glob", pattern=pattern))
        self.assertEqual(self.invoke("glob", pattern="*.txt")["paths"], [])
        self.assertEqual(self.invoke("glob", pattern="*/*.txt")["paths"], [])

    def test_glob_caps_results(self):
        for index in range(201):
            (self.workdir / f"file-{index:03d}.txt").touch()
        result = self.invoke("glob", pattern="*.txt")
        self.assertEqual(len(result["paths"]), 200)
        self.assertEqual(result["paths"][0], "file-000.txt")
        self.assertTrue(result["truncated"])

    def test_io_and_decode_errors_are_tool_results(self):
        (self.workdir / "binary.bin").write_bytes(b"\xff\xfe")
        for path in ("missing.txt", ".", "binary.bin"):
            with self.subTest(path=path):
                self.assertIn("error", self.invoke("read_file", path=path))
        self.assertIn("error", self.invoke("write_file", path=".", content="x"))

    def test_invalid_arguments_never_reach_handlers(self):
        cases = [
            ("read_file", {"path": "a", "limit": 0}),
            ("read_file", {"path": "a", "limit": True}),
            ("read_file", {"path": "a", "limit": "1"}),
            ("read_file", {"path": "a", "extra": True}),
            ("write_file", {"path": "a"}),
            ("write_file", {"path": "a", "content": 42}),
            ("write_file", {"path": "a", "content": "x", "workdir": "outside"}),
            ("edit_file", {"path": "a", "old_text": "", "new_text": "x"}),
            ("glob", {"pattern": "  "}),
            ("read_file", {"path": None}),
        ]
        for name, arguments in cases:
            handler = Mock()
            with (
                self.subTest(tool=name, arguments=arguments),
                patch.dict(
                    homebrew_harness.tools.registry.TOOL_HANDLERS, {name: handler}
                ),
            ):
                self.assertIn("error", self.invoke(name, **arguments))
                handler.assert_not_called()


if __name__ == "__main__":
    unittest.main()
