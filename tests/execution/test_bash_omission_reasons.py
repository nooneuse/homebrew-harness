"""omission_reasons contract and the isolated-copy run_tests diagnostic.

New regression coverage only; protected baseline tests are untouched.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from homebrew_harness.execution.bash_policy import (
    BashPolicy,
    BashPolicyError,
    bash_snapshot,
    load_bash_policy,
)
from homebrew_harness.execution.bash_runner import ContainerShellRunner

# Constructed at runtime so this regression file itself stays visible to the
# isolated-copy content filter while still exercising secret-body exclusion.
SECRET_BODY = "-----BEGIN RSA PRIVATE " + "KEY-----\nmaterial\n"


def container_policy():
    return BashPolicy(
        mode="container",
        cli="/nonexistent/docker-cli",
        image="sha256:" + "b" * 64,
    )


class OmissionReasonsContract(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()

    def test_reasons_sum_to_omitted_entries_and_split_path_vs_content(self):
        (self.root / "ok.txt").write_text("hello")
        (self.root / ".git").mkdir()
        (self.root / ".git" / "config").write_text("private")
        (self.root / ".env").write_text("private")
        (self.root / "secret.txt").write_text(SECRET_BODY)
        (self.root / "key.pem").write_text("material")
        (self.root / "link").symlink_to(self.root, target_is_directory=True)
        os.mkfifo(self.root / "fifo")
        snapshot, stats = bash_snapshot(self.root, container_policy())
        self.assertEqual(set(snapshot), {"ok.txt"})
        self.assertEqual(
            set(stats),
            {"included_files", "omitted_entries", "omission_reasons", "input_bytes"},
        )
        reasons = stats["omission_reasons"]
        self.assertEqual(set(reasons), {"path", "content"})
        self.assertEqual(reasons["path"], 4)  # .git dir, .env, symlink dir, fifo
        self.assertEqual(reasons["content"], 2)  # secret body, .pem suffix
        self.assertEqual(stats["omitted_entries"], 6)
        self.assertEqual(sum(reasons.values()), stats["omitted_entries"])
        self.assertEqual(stats["included_files"], 1)

    def test_no_omitted_path_or_content_is_disclosed(self):
        (self.root / "ok.txt").write_text("hello")
        (self.root / ".env").write_text("private")
        (self.root / "secret.txt").write_text(SECRET_BODY)
        _, stats = bash_snapshot(self.root, container_policy())
        serialized = json.dumps(stats)
        for name in (".env", "secret.txt", "PRIVATE KEY", "material", "private"):
            self.assertNotIn(name, serialized)
        self.assertEqual(sum(stats["omission_reasons"].values()),
                         stats["omitted_entries"])

    def test_clean_workspace_reports_zero_omissions(self):
        (self.root / "ok.txt").write_text("hello")
        snapshot, stats = bash_snapshot(self.root, container_policy())
        self.assertEqual(set(snapshot), {"ok.txt"})
        self.assertEqual(stats["omitted_entries"], 0)
        self.assertEqual(stats["omission_reasons"], {"path": 0, "content": 0})
        self.assertEqual(sum(stats["omission_reasons"].values()),
                         stats["omitted_entries"])


class IsolatedCopyDiagnostic(unittest.TestCase):
    class FakeBackend:
        inputs = {
            "included_files": 1,
            "omitted_entries": 2,
            "omission_reasons": {"path": 1, "content": 1},
            "input_bytes": 10,
        }

        def status(self):
            return {"backend": "container", "cleanup_complete": True}

    def test_metadata_points_test_attempts_at_run_tests_without_leaking_paths(self):
        runner = ContainerShellRunner(container_policy())
        self.addCleanup(runner.close)
        metadata = runner._metadata("exec_diag", self.FakeBackend())
        self.assertEqual(metadata["workspace"], "isolated_copy")
        self.assertFalse(metadata["writeback"])
        notice = metadata["notice"]
        self.assertIn("run_tests", notice)
        self.assertIn("registered recipe", notice)
        self.assertIn("omitted paths and contents are never", notice)
        self.assertNotIn("/nonexistent", notice)
        self.assertEqual(metadata["inputs"], self.FakeBackend.inputs)

    def test_missing_container_configuration_never_falls_back_to_native(self):
        with tempfile.TemporaryDirectory() as outside, tempfile.TemporaryDirectory() as workspace:
            missing = Path(outside) / "absent.json"
            with self.assertRaisesRegex(BashPolicyError, "no fallback"):
                load_bash_policy(missing, workspace, mode="container", missing_ok=True)


if __name__ == "__main__":
    unittest.main()
