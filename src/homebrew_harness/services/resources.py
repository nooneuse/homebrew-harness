"""Retain test resources and their lease until bounded cleanup is confirmed."""

import threading

from homebrew_harness.execution.execution_control import cleanup_scope
from homebrew_harness.runtime.runtime_control import ClosePending


class TestResources:
    def __init__(self, leases, name):
        self.leases, self.name = leases, name
        self.runner = None
        self.error = None
        self.lock = threading.RLock()

    def own(self, runner):
        with self.lock:
            if self.runner is not None:
                raise ClosePending("Previous test resources still need cleanup.")
            self.leases.acquire(self.name)
            self.runner = runner
        return runner

    def request_close(self):
        runner = self.runner
        if runner is not None:
            try:
                with cleanup_scope():
                    runner.close()
            except Exception as error:
                self.error = type(error).__name__

    def settle(self, seconds=1):
        if not self.lock.acquire(timeout=seconds):
            raise ClosePending("Test cleanup is already owned.")
        try:
            if self.runner is None:
                return
            try:
                with cleanup_scope(seconds + 0.5):
                    self.runner.close()
                    if not self.runner.wait_closed(seconds):
                        raise ClosePending("Test resources have not finished cleanup.")
            except Exception as error:
                self.error = type(error).__name__
                raise ClosePending("Test cleanup remains pending.") from error
            self.runner = None
            self.error = None
            self.leases.release(self.name)
        finally:
            self.lock.release()
