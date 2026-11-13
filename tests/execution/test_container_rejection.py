"""Creation refusal, uncertain outcomes and complete daemon absence proofs."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from homebrew_harness.execution.execution_backend import (
    BackendUnavailable,
    ContainerBackend,
    ContainerPolicy,
    ExecutionRequest,
)
from homebrew_harness.execution.model_gateway import Deadline


class CreationRejectionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.staging = self.root / "staging"
        self.staging.mkdir()
        self.name = "harness-test-create-rejection"
        self.backend = ContainerBackend(ContainerPolicy(
            "/not-installed-cli", "sha256:" + "d" * 64,
            ("/bin/true",), ("/native",),
        ))

    def unknown(self, rejected=False):
        self.backend.unknown_create.add(self.name)
        if rejected:
            self.backend.rejected_create.add(self.name)
        self.backend.pending[self.name] = (self.staging, self.root)

    def test_only_complete_precreation_daemon_refusal_is_classified(self):
        refusal = 'Error response from daemon: invalid mount config for type "bind": missing source'
        result = subprocess.CompletedProcess([], 1, "", refusal)
        self.assertTrue(self.backend._precreation_rejection(result))
        result.output_limit = True
        self.assertFalse(self.backend._precreation_rejection(result))
        for code, stderr in (
            (0, refusal),
            (1, "failed to connect to the Docker API"),
            (1, "Error response from daemon: unknown internal error"),
            (None, refusal),
        ):
            self.assertFalse(self.backend._precreation_rejection(
                subprocess.CompletedProcess([], code, "", stderr)
            ))

    def test_confirmed_refusal_and_absence_release_staging_and_pending_cleanup(self):
        self.unknown(rejected=True)
        with patch.object(self.backend, "_inspect", return_value=None), patch.object(
            self.backend, "_control", return_value=subprocess.CompletedProcess([], 0, "", "")
        ) as control:
            self.assertTrue(self.backend.wait_closed(.2))
        self.assertEqual(control.call_count, 1)
        self.assertIn("ls", control.call_args.args[0])
        self.assertFalse(self.staging.exists())
        self.assertFalse(self.backend.unknown_create)
        self.assertFalse(self.backend.rejected_create)
        record = json.loads((self.root / ".harness/containers" / (self.name + ".json")).read_text())
        self.assertEqual(record["phase"], "removed")

    def test_absence_alone_does_not_resolve_ambiguous_creation(self):
        self.unknown()
        with patch.object(self.backend, "_inspect", return_value=None), patch.object(
            self.backend, "_control"
        ) as control:
            self.assertFalse(self.backend.wait_closed(.2))
        control.assert_not_called()
        self.assertTrue(self.staging.exists())
        self.assertFalse(self.backend.status()["cleanup_complete"])

    def test_absence_query_must_be_successful_complete_and_exact(self):
        self.unknown(rejected=True)
        truncated = subprocess.CompletedProcess([], 0, "", "")
        truncated.output_limit = True
        for result in (
            subprocess.CompletedProcess([], 1, "", "daemon unreachable"),
            subprocess.CompletedProcess([], 0, self.name + "\n", ""),
            truncated,
        ):
            with self.subTest(result=result), patch.object(
                self.backend, "_inspect", return_value=None
            ), patch.object(self.backend, "_control", return_value=result):
                self.assertFalse(self.backend.wait_closed(.2))
                self.assertTrue(self.staging.exists())
                self.assertIn(self.name, self.backend.unknown_create)

    def test_foreign_container_is_not_removed_even_after_refusal(self):
        self.unknown(rejected=True)
        foreign = {"Config": {"Labels": {"homebrew-harness.run": "other"}}}
        with patch.object(self.backend, "_inspect", return_value=foreign), patch.object(
            self.backend, "_control"
        ) as control:
            self.assertFalse(self.backend.wait_closed(.2))
        control.assert_not_called()
        self.assertTrue(self.staging.exists())

    def test_launch_refusal_is_cleaned_without_starting_task_code(self):
        (self.root / "marker.txt").write_text("registered input")
        refusal = 'Error response from daemon: invalid mount config for type "bind": missing source'

        def control(argv, *args, **kwargs):
            if argv[1:3] == ["image", "inspect"]:
                return subprocess.CompletedProcess(argv, 0, "sha256:" + "d" * 64, "")
            if argv[1] == "create":
                return subprocess.CompletedProcess(argv, 1, "", refusal)
            if argv[1:3] == ["container", "ls"]:
                return subprocess.CompletedProcess(argv, 0, "", "")
            raise AssertionError("Unexpected Docker operation: " + repr(argv))

        request = ExecutionRequest(
            ("/native",), self.root, {}, Deadline.after(5),
            recipe_argv=("/native",), input_globs=("marker.txt",),
        )
        with patch.object(self.backend, "probe", return_value={"available": True}), patch.object(
            self.backend, "_control", side_effect=control
        ), patch.object(self.backend, "_inspect", return_value=None), patch.object(
            self.backend, "_start_process"
        ) as start, patch(
            "homebrew_harness.execution.execution_backend.tempfile.mkdtemp",
            return_value=str(self.staging),
        ):
            with self.assertRaisesRegex(BackendUnavailable, "Container creation failed"):
                self.backend.launch(request)
        start.assert_not_called()
        self.assertFalse(self.staging.exists())
        self.assertTrue(self.backend.status()["cleanup_complete"])
