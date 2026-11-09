"""One read boundary and independent search scanning through real dispatch."""

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from homebrew_harness import HarnessSession
from homebrew_harness.context.compaction import ArchiveStore
from homebrew_harness.execution.model_protocol import save_replay
from homebrew_harness.security.file_visibility import file_access_scope
from homebrew_harness.tools.files import run_edit, run_glob, run_read, run_write
from homebrew_harness.tools.queries import git_diff, git_status, search_text
from tests.support.clients import call, fake_client, reply


class FileVisibilityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()

    def test_reserved_writes_are_denied_before_creating_parents(self):
        from homebrew_harness.security.permissions import check_rules

        for name in (".harness", ".tasks", ".mailboxes", ".memory", ".git", ".venv"):
            for spelling in (name, name.upper()):
                for prefix in ("", "nested/"):
                    path = prefix + spelling + "/marker.txt"
                    with self.subTest(path=path):
                        self.assertEqual(
                            check_rules("write_file", {"path": path}, self.root).action,
                            "deny",
                        )
                        with self.assertRaises(ValueError):
                            run_write(path, "marker", self.root)
                        with self.assertRaises(ValueError):
                            run_edit(path, "old", "new", self.root)
        with self.assertRaises(ValueError):
            run_write(".HARNESS", "not a directory", self.root)
        with self.assertRaises(ValueError):
            run_write(".SCHEDULED_TASKS.json", "{}", self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_case_aliases_cannot_read_replay_or_write_runtime_through_dispatch(self):
        state = save_replay(self.root, "foreign", "fixture", {"text": "private-marker"})
        alias = state["reference"].replace(
            ".harness/protocol-state", ".HARNESS/PROTOCOL-STATE"
        )
        if not (self.root / alias).exists():
            self.skipTest("Filesystem does not alias differently cased paths")
        self.assertTrue((self.root / alias).samefile(self.root / state["reference"]))
        calls = [
            call("read", name="read_file", arguments=json.dumps({"path": alias})),
            call(
                "write",
                name="write_file",
                arguments=json.dumps(
                    {"path": ".HARNESS/marker.txt", "content": "marker"}
                ),
            ),
            call(
                "search",
                name="search_text",
                arguments=json.dumps(
                    {"pattern": ".HARNESS/PROTOCOL-STATE/**", "text": "private-marker"}
                ),
            ),
        ]
        client, snapshots = fake_client([reply(calls=calls), reply("Done")])
        with (
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
            HarnessSession(client, "fixture", self.root) as session,
        ):
            session.run_user("Inspect public files")
        self.assertTrue(session.wait_closed(5))
        results = [
            json.loads(m["content"]) for m in snapshots[-1] if m["role"] == "tool"
        ]
        self.assertTrue(all(r.get("error") for r in results), results)
        self.assertNotIn("private-marker", json.dumps(results))
        self.assertFalse((self.root / ".harness/marker.txt").exists())

    def test_archive_aliases_reject_without_changing_business_case(self):
        own = ArchiveStore(self.root, namespace="own")
        ref = own.save("tool-results", "own-marker")
        with file_access_scope(own, restrict_archives=True):
            self.assertIn("own-marker", run_read(ref, self.root))
            with self.assertRaises(ValueError):
                run_read(ref.replace(".harness", ".HARNESS"), self.root)
            with self.assertRaises(ValueError):
                run_glob(".HARNESS/**", self.root)
        run_write("Report.md", "business", self.root)
        self.assertIn("business", run_read("Report.md", self.root))
        self.assertIn("Report.md", json.loads(run_glob("*.md", self.root))["paths"])
        if not (self.root / "report.md").exists():
            run_write("report.md", "different", self.root)
            self.assertIn("business", run_read("Report.md", self.root))

    def test_actual_dispatch_rejects_private_replay_through_all_read_queries(self):
        state = save_replay(
            self.root,
            "other-context",
            "other-model",
            {"reasoning_content": "hidden-replay-marker"},
        )
        calls = [
            call(
                "read",
                name="read_file",
                arguments=json.dumps({"path": state["reference"]}),
            ),
            call(
                "search",
                name="search_text",
                arguments=json.dumps(
                    {
                        "text": "hidden-replay-marker",
                        "pattern": ".harness/protocol-state/**",
                    }
                ),
            ),
            call(
                "glob",
                name="glob",
                arguments=json.dumps({"pattern": ".harness/protocol-state/**"}),
            ),
        ]
        client, snapshots = fake_client([reply(calls=calls), reply("Done")])
        with (
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
            HarnessSession(
                client, "fixture", self.root, shutdown_timeout=0.05
            ) as session,
        ):
            session.run_user("Inspect public sources")
        self.assertTrue(session.wait_closed(5))
        results = [
            json.loads(m["content"]) for m in snapshots[-1] if m["role"] == "tool"
        ]
        self.assertEqual(len(results), 3)
        self.assertTrue(all(r.get("error") for r in results), results)
        self.assertNotIn("hidden-replay-marker", json.dumps(results))

    def test_restricted_archive_read_glob_and_search_share_ownership(self):
        own, foreign = (
            ArchiveStore(self.root, namespace="own"),
            ArchiveStore(self.root, namespace="foreign"),
        )
        mine = own.save("tool-results", "own-marker")
        theirs = foreign.save("tool-results", "foreign-marker")
        with file_access_scope(own, restrict_archives=True):
            self.assertIn("own-marker", run_read(mine, self.root))
            with self.assertRaises(ValueError):
                run_read(theirs, self.root)
            paths = json.loads(run_glob(".harness/**", self.root))["paths"]
            self.assertIn(mine, paths)
            self.assertNotIn(theirs, paths)
            results = json.loads(
                search_text("marker", workdir=self.root, pattern=".harness/**")
            )["matches"]
            self.assertEqual([r["path"] for r in results], [mine])

    def test_raw_bundles_and_private_path_aliases_are_rejected(self):
        path = self.root / ".harness/artifact-bundles/bundle/tool-results/output.txt"
        path.parent.mkdir(parents=True)
        path.write_text("hidden-bundle-marker")
        for target in (
            str(path),
            ".harness/artifact-bundles/bundle/tool-results/output.txt",
            "ordinary/../.harness/artifact-bundles/bundle/tool-results/output.txt",
        ):
            with self.subTest(target=target), self.assertRaises(ValueError):
                run_read(target, self.root)
        for pattern in (".harness/artifact-bundles/**", ".git/**"):
            with self.subTest(pattern=pattern), self.assertRaises(ValueError):
                search_text("marker", workdir=self.root, pattern=pattern)

    def test_git_queries_exclude_private_changes_and_rename_origins(self):
        def git(*args):
            return subprocess.run(
                ["git", "-c", "core.hooksPath=.git/hooks", *args],
                cwd=self.root,
                capture_output=True,
                text=True,
                check=True,
            )

        git("init", "-q")
        secret = self.root / ".harness/protocol-state/hidden.txt"
        secret.parent.mkdir(parents=True)
        secret.write_text("private-replay-marker")
        (self.root / "public.txt").write_text("public")
        git("add", ".harness/protocol-state/hidden.txt", "public.txt")
        git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        )
        secret.write_text("changed-private-marker")
        (self.root / "public.txt").write_text("changed-public")
        status = json.loads(git_status(workdir=self.root))
        self.assertEqual([r["path"] for r in status["files"]], ["public.txt"])
        self.assertNotIn("private-marker", git_diff(workdir=self.root))
        git("mv", ".harness/protocol-state/hidden.txt", "renamed.txt")
        self.assertNotIn("renamed.txt", git_status(workdir=self.root))
        self.assertNotIn("private-marker", git_diff(workdir=self.root))

    def test_search_checks_more_than_glob_display_limit_and_skips_metadata(self):
        (self.root / ".git").mkdir()
        (self.root / "src").mkdir()
        for n in range(300):
            (self.root / ".git" / f"object{n:03d}").write_text("irrelevant")
            (self.root / "src" / f"file{n:03d}.py").write_text(
                "target-marker" if n == 299 else "ordinary source"
            )
        paths = json.loads(run_glob("**/*", self.root))
        self.assertEqual(len(paths["paths"]), 200)
        self.assertTrue(paths["truncated"])
        self.assertFalse(any(p.startswith(".git/") for p in paths["paths"]))
        result = json.loads(search_text("target-marker", workdir=self.root))
        self.assertEqual([r["path"] for r in result["matches"]], ["src/file299.py"])
        self.assertFalse(result["truncated"])

    def test_search_long_line_snippet_contains_the_actual_hit(self):
        (self.root / "long.txt").write_text("x" * 2000 + "target-marker" + "y" * 2000)
        result = json.loads(search_text("target-marker", workdir=self.root))
        self.assertEqual(result["matches"][0]["column"], 2001)
        self.assertIn("target-marker", result["matches"][0]["text"])
        self.assertLessEqual(len(result["matches"][0]["text"]), 1000)

    def test_unreadable_or_large_input_reports_incomplete_search(self):
        (self.root / "large.txt").write_bytes(b"x" * 2000001)
        result = json.loads(search_text("target", workdir=self.root))
        self.assertTrue(result["truncated"])
        self.assertEqual(result["skipped_files"], 1)
        self.assertEqual(result["stop_reason"], "unreadable_file")
