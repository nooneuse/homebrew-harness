import homebrew_harness.execution.background

"""Control-process cleanup bookkeeping; real isolation is tested separately."""
import subprocess
import threading
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
