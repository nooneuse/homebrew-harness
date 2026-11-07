"""Safe snapshot fidelity includes executable bits without special permissions."""

import os
import tempfile
import unittest
from pathlib import Path

from homebrew_harness.execution.bash_policy import BashPolicy, bash_snapshot
from homebrew_harness.execution.execution_backend import (
    ContainerBackend,
    ContainerPolicy,
)
from homebrew_harness.execution.test_runner import snapshot


class SnapshotModeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.policy = BashPolicy(
            mode="container", cli="/missing", image="sha256:" + "a" * 64
        )

    def test_mode_changes_update_bash_and_test_snapshots(self):
        path = self.root / "script"
        path.write_text("fixture")
        path.chmod(0o644)
        before = bash_snapshot(self.root, self.policy)[0]
        tests_before = snapshot(self.root)
        path.chmod(0o755)
        after = bash_snapshot(self.root, self.policy)[0]
        tests_after = snapshot(self.root)
        self.assertFalse(before["script"]["executable"])
        self.assertTrue(after["script"]["executable"])
        self.assertNotEqual(before, after)
        self.assertNotEqual(tests_before, tests_after)

    def test_writable_staging_strips_special_bits_and_preserves_execute(self):
        script = self.root / "script"
        script.write_text("fixture")
        script.chmod(0o6755)
        plain = self.root / "plain"
        plain.write_text("fixture")
        plain.chmod(0o644)
        policy = ContainerPolicy(
            "/missing",
            "sha256:" + "a" * 64,
            ("/bin/true",),
            ("/bin/true",),
            uid=os.getuid(),
            workspace_writable=True,
        )
        ContainerBackend(policy)._prepare_staging(self.root)
        self.assertEqual(script.stat().st_mode & 0o7777, 0o700)
        self.assertEqual(plain.stat().st_mode & 0o7777, 0o600)
