"""Common observation and waiting; execution control remains mode-specific."""

import math
import time
import threading
from functools import wraps
from .types import ExecutionEvent, WaitReport, ResultNotReady, SDKError, InvalidInput
from homebrew_harness.storage.api_state import digest


def host_operation(function):
    """Fence accepted host calls against bounded shutdown."""

    @wraps(function)
    def invoke(self, *args, **kwargs):
        with self.core.operation():
            try:
                return function(self, *args, **kwargs)
            except SDKError:
                raise
            except ValueError as error:
                raise InvalidInput(str(error)) from error
            except OSError as error:
                raise SDKError(
                    "Host persistence or resource operation failed ("
                    + type(error).__name__
                    + "). Inspect accepted state before retrying.",
                    code="operation_unknown",
                ) from error
            except RuntimeError as error:
                from homebrew_harness.storage.durable_runtime import RecoveryRequired
                from homebrew_harness.orchestration.workflow_runtime import (
                    WorkflowError,
                )

                if isinstance(error, RecoveryRequired):
                    raise SDKError(str(error), code="recovery_required") from error
                if isinstance(error, WorkflowError):
                    raise InvalidInput(str(error)) from error
                raise

    return invoke


class Handle:
    def __init__(self, core, ref, *, owned=False):
        self.core, self.ref, self.owned = core, ref, owned

    @property
    def id(self):
        return self.ref.id

    def wait(self, timeout=None, *, until="actionable"):
        if timeout is not None and (
            not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout < 0
        ):
            raise ValueError("Wait timeout must be finite nonnegative seconds.")
        if until not in {"actionable", "settled"}:
            raise ValueError("Wait until must be actionable or settled.")
        expires = None if timeout is None else time.monotonic() + timeout
        while True:
            snapshot = self.snapshot()
            done = snapshot.actionable if until == "actionable" else snapshot.settled
            if done:
                return WaitReport(snapshot, True)
            if expires is not None and time.monotonic() >= expires:
                return WaitReport(snapshot, False, True)
            self.core.changed.wait(
                0.05
                if expires is None
                else min(0.05, max(0, expires - time.monotonic()))
            )
            self.core.changed.clear()

    def watch(self, *, interval=0.1, stop_event=None):
        if (
            not isinstance(interval, (int, float))
            or not math.isfinite(interval)
            or interval <= 0
        ):
            raise ValueError("Watch interval must be finite positive seconds.")
        previous = None
        stop = stop_event or threading.Event()
        while not stop.is_set():
            snapshot = self.snapshot()
            identity = digest(snapshot.data())
            if identity != previous:
                yield ExecutionEvent(identity, "snapshot", snapshot)
                previous = identity
            if snapshot.settled:
                return
            stop.wait(interval)

    def requests(self):
        return self.core.requests.list(target=self.ref)

    def _require_result(self):
        if not self.snapshot().output_available:
            raise ResultNotReady("Execution has no confirmed result yet.", ref=self.ref)
