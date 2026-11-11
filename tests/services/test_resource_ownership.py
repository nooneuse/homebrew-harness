"""Cleanup gets its own deadline and releases ownership only after actual closure."""

import tempfile
import threading
import unittest
from pathlib import Path

from homebrew_harness.execution.execution_control import (
    check,
    cleanup_scope,
    control_scope,
)
from homebrew_harness.execution.execution_leases import ExecutionLeases
from homebrew_harness.execution.model_gateway import Deadline, ModelCancelled
from homebrew_harness.runtime.runtime_control import ClosePending
from homebrew_harness.services.resources import TestResources


class ResourceOwnership(unittest.TestCase):
    def test_cleanup_ignores_parent_cancel_and_deadline_but_honors_explicit_cancel(
        self,
    ):
        cancelled = threading.Event()
        cancelled.set()
        with control_scope(Deadline.after(0), cancel_events=(cancelled,)):
            with cleanup_scope(1):
                check()
                with self.assertRaises(ModelCancelled):
                    check(cancel_events=(cancelled,))

    def test_failed_cleanup_keeps_runner_and_lease_until_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            leases = ExecutionLeases(Path(folder).resolve())
            resources = TestResources(leases, "test-resource")

            class Runner:
                ready = False
                fail = True

                def close(self):
                    pass

                def wait_closed(self, seconds):
                    if self.fail:
                        raise OSError("Synthetic cleanup failure")
                    return self.ready

            runner = resources.own(Runner())
            for _ in range(2):
                with self.assertRaises(ClosePending):
                    resources.settle(0.01)
                self.assertTrue(leases.held("test-resource"))
                self.assertIs(resources.runner, runner)
                runner.fail = False
            runner.ready = True
            resources.settle(0.01)
            self.assertIsNone(resources.runner)
            self.assertFalse(leases.held("test-resource"))
            leases.close()
