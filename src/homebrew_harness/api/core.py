"""One SDK resource owner; execution remains in the established controllers."""

import threading
from contextlib import contextmanager
from pathlib import Path

from homebrew_harness.security.safe_files import canonical_root
from homebrew_harness.storage.durable_runtime import DurableStore
from homebrew_harness.runs.store import RunStore
from homebrew_harness.runtime.output import output_scope
from .config import HostConfig
from .types import CloseReport, ClosePending, SDKError


class Harness:
    def __init__(self, *, workdir=None, config=None):
        self.workdir = canonical_root(Path.cwd() if workdir is None else Path(workdir))
        self.config = config or HostConfig.from_files()
        if not isinstance(self.config, HostConfig):
            raise ValueError("Expected HostConfig.")
        self.store = DurableStore(self.workdir)
        self.run_store = RunStore(self.workdir)
        self.changed = threading.Event()
        self.closed = False
        self._closed_resources = False
        from homebrew_harness.runtime.runtime_control import Shutdown

        self.shutdown = Shutdown()
        from homebrew_harness.runtime.runtime_control import CloseProgress

        self.progress = CloseProgress()
        self._persistence_errors = {}
        self._condition = threading.Condition()
        self._active = 0
        self._admitted = threading.local()
        from .sessions import Sessions
        from .requests import Requests

        self.sessions = Sessions(self)
        self.requests = Requests(self)
        from .runs import Runs
        from .workflows import Workflows
        from .interactions import Interactions
        from .admin import Admin

        self.runs = Runs(self)
        self.workflows = Workflows(self)
        self.interactions = Interactions(self)
        self.admin = Admin(self)
        from .services import Services

        self.services = Services(self)

    def check_open(self):
        if self.closed and not getattr(self._admitted, "depth", 0):
            raise SDKError("SDK host is closing.", code="host_closed")

    @contextmanager
    def operation(self):
        with self._condition:
            self.check_open()
            self._active += 1
            self._admitted.depth = getattr(self._admitted, "depth", 0) + 1
        try:
            yield
        finally:
            with self._condition:
                self._admitted.depth -= 1
                self._active -= 1
                self._condition.notify_all()

    def _owned(self):
        return [
            *self.sessions.live.values(),
            *self.runs.owned.values(),
            *self.workflows.live.values(),
            *self.interactions.live.values(),
            *self.services.handles(),
        ]

    def close(self, timeout=None):
        seconds = self.config.settings.shutdown_seconds if timeout is None else timeout
        import math

        if (
            not isinstance(seconds, (int, float))
            or not math.isfinite(seconds)
            or seconds < 0
        ):
            raise ValueError("Close timeout must be finite nonnegative seconds.")
        with self._condition:
            self.closed = True
        self.shutdown.request(self._finish_close, seconds)
        return self._close_report()

    def _finish_close(self):
        with self._condition:
            while self._active:
                self._condition.wait()
        handles = self._owned()
        for handle in handles:

            def close(handle=handle):
                with output_scope():
                    handle.close()

            self.progress.step("close:" + handle.id, close, repeat=True)
        for handle in handles:

            def settle(handle=handle):
                while not handle.wait_closed(0.1):
                    runtime = getattr(handle, "runtime", None)
                    if (
                        runtime is not None
                        and hasattr(runtime, "shutdown_status")
                        and runtime.shutdown_status().get("retryable")
                    ):
                        raise SDKError(
                            "Owned cleanup needs a retry.",
                            code="cleanup_failed",
                            ref=handle.ref,
                        )
                    shutdown = getattr(handle, "shutdown", None)
                    if shutdown is not None and shutdown.error:
                        raise SDKError(
                            "Owned cleanup needs a retry.",
                            code="cleanup_failed",
                            ref=handle.ref,
                        )
                    if getattr(
                        handle, "supervisor", None
                    ) is not None and handle.workers.wait(0):
                        raise SDKError(
                            "Stopped Run cleanup requires inspection.",
                            code="recovery_required",
                            ref=handle.ref,
                        )
                if hasattr(handle, "runtime") and hasattr(
                    handle.runtime, "shutdown_status"
                ):
                    status = handle.runtime.shutdown_status()
                    if status.get("persistence_ok") is False:
                        self._persistence_errors[handle.id] = {
                            "component": handle.id,
                            "error_type": "persistence_failed",
                            "errors": status.get("errors", []),
                        }

            self.progress.step("settle:" + handle.id, settle)
        self.progress.require(
            [name + handle.id for handle in handles for name in ("close:", "settle:")]
        )
        components = self.config.components

        def close_components():
            if not components.own_client:
                return
            if components.model_registry is not None:
                components.model_registry.close()
            elif hasattr(components.client, "close"):
                components.client.close()

        self.progress.step("components", close_components)
        self.progress.step("run_store", lambda: self.run_store.close(permanent=True))
        self.progress.require(["components", "run_store"])
        self._closed_resources = True

    def _close_report(self):
        if self._closed_resources:
            errors = tuple(self._persistence_errors.values())
            return CloseReport(
                "error" if errors else "closed", not errors, True, errors=errors
            )
        pending = tuple(
            handle.ref for handle in self._owned() if not handle.wait_closed(0)
        )
        errors = tuple(self.progress.snapshot()["errors"])
        return CloseReport(
            "cleanup_pending", False, False, pending, errors, self._active
        )

    def wait_closed(self, timeout=5):
        self.shutdown.finished.wait(timeout)
        return self._close_report()

    def finish_scope(self, handle, error_type=None):
        if (
            not handle.wait_closed(self.config.settings.shutdown_seconds)
            and error_type is None
        ):
            raise ClosePending(
                CloseReport("cleanup_pending", False, False, (handle.ref,))
            )

    def __enter__(self):
        self.check_open()
        return self

    def __exit__(self, kind, error, tb):
        report = self.close()
        if report.state == "cleanup_pending" and kind is None:
            raise ClosePending(report)
        if report.state == "error" and kind is None:
            raise SDKError(
                "Owned resources closed with a persistence error.",
                code="close_failed",
                details=report.data(),
            )
