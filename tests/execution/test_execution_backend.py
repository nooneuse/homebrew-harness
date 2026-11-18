import homebrew_harness.execution.background

"""Control-process cleanup bookkeeping; real isolation is tested separately."""
import subprocess
import threading
import tempfile
import time
from pathlib import Path
import unittest
from unittest.mock import patch
from homebrew_harness.execution.execution_backend import (
    ContainerBackend,
    ContainerPolicy,
)


class ControlCleanupTests(unittest.TestCase):
    def test_unreaped_control_process_keeps_backend_pending(self):
        released = threading.Event()

        class Control:
            def run(self, *args, **kwargs):
                return subprocess.CompletedProcess(args[0], 0, "linux", "")

            def close(self, *args):
                pass

            def wait_closed(self, *args):
                return released.is_set()

        policy = ContainerPolicy(
            "/missing-cli", "sha256:" + "a" * 64, ("/bin/true",), ("/native",)
        )
        backend = ContainerBackend(policy)
        with patch("homebrew_harness.execution.background.ShellRunner", Control):
            backend._control(["control"], 1)
        self.assertEqual(len(backend.control_runners), 1)
        self.assertFalse(backend.status()["cleanup_complete"])
        self.assertFalse(backend.wait_closed(0.01))
        released.set()
        self.assertTrue(backend.wait_closed(0.01))

    def test_control_management_retains_its_own_longer_deadline(self):
        from homebrew_harness.execution.execution_control import operation_deadline
        windows = []
        class Control:
            def run(self, *args, **kwargs):
                windows.append(operation_deadline(args[3]).remaining())
                return subprocess.CompletedProcess(args[0], 0, "", "")
            def close(self, *args):
                pass
            def wait_closed(self, *args):
                return True
        policy = ContainerPolicy("/missing-cli", "sha256:" + "a" * 64, ("/bin/true",), ("/native",))
        backend = ContainerBackend(policy)
        with patch("homebrew_harness.execution.background.ShellRunner", Control):
            backend._control(["control"], 60)
        self.assertGreater(windows[0], 59)

    def test_container_removal_does_not_renew_window_for_journal(self):
        from homebrew_harness.execution.execution_control import check
        from homebrew_harness.execution.model_gateway import ModelDeadlineExceeded
        policy = ContainerPolicy("/missing-cli", "sha256:" + "a" * 64, ("/bin/true",), ("/native",))
        backend = ContainerBackend(policy)
        def removed(*args):
            time.sleep(.03)
            return subprocess.CompletedProcess(args[0], 0, "", "")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            temporary = root / "staging"
            temporary.mkdir()
            with patch.object(backend, '_control', side_effect=removed), patch('homebrew_harness.storage.storage_admin.publish', side_effect=lambda *args: check()), self.assertRaises(ModelDeadlineExceeded):
                backend._remove('fixture', temporary, root, timeout=.02)
