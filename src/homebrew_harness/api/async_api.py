"""Async host views share controllers; observer cancellation leaves execution owned."""

import asyncio
import math
import threading
import time
from concurrent.futures import Future

from homebrew_harness.runtime.runtime_control import Workers, WorkAccepted
from homebrew_harness.storage.api_state import digest
from .core import Harness
from .base import Handle
from .types import WaitReport, ExecutionEvent, SDKError, ClosePending, CloseReport


class _Calls:
    def __init__(self):
        self.workers = Workers()
        self.slots = threading.BoundedSemaphore(16)
        self.cleanup_slots = threading.BoundedSemaphore(2)
        self.lock = threading.Lock()
        self.pending = set()
        self.closed = False

    async def call(self, function, *args, critical=False, **kw):
        if self.closed and not critical:
            raise SDKError("Async API host is closing.", code="host_closed")
        slots = self.cleanup_slots if critical else self.slots
        if not slots.acquire(blocking=False):
            raise SDKError("Too many concurrent host API calls.", code="api_busy")
        future = Future()
        if not critical:
            with self.lock:
                self.pending.add(future)

        def invoke():
            try:
                result = function(*args, **kw)
            except BaseException as error:
                future.set_exception(error)
            else:
                future.set_result(result)
            finally:
                slots.release()
                if not critical:
                    with self.lock:
                        self.pending.discard(future)

        try:
            self.workers.start(invoke, name="sdk-api-call", track_operation=False)
        except WorkAccepted:
            pass
        except BaseException:
            slots.release()
            if not critical:
                with self.lock:
                    self.pending.discard(future)
            raise
        # Cancellation cannot undo a committed submission or lose its owner.
        wrapped = asyncio.wrap_future(future)

        def observed(done):
            if not done.cancelled():
                done.exception()

        wrapped.add_done_callback(observed)
        return await asyncio.shield(wrapped)

    def busy(self):
        with self.lock:
            return any(not future.done() for future in self.pending)


class _View:
    def __init__(self, root, target):
        self._root, self._target = root, target

    def __getattr__(self, name):
        value = getattr(self._target, name)
        if not callable(value):
            return value

        async def invoke(*args, **kw):
            result = await self._root._calls.call(value, *args, **kw)
            return self._root._wrap(result)

        return invoke

    async def __aenter__(self):
        return self

    async def __aexit__(self, kind, error, tb):
        await self.close()
        if hasattr(self._target, "wait_closed"):
            closed = await self._root._calls.call(
                self._target.wait_closed,
                self._root._sync.config.settings.shutdown_seconds,
                critical=True,
            )
            if not closed and kind is None:
                raise ClosePending(
                    CloseReport("cleanup_pending", False, False, (self._target.ref,))
                )


class AsyncHandle(_View):
    async def execute(self, text, *, timeout=None, request_id=None, goal=None):
        turn = await self.submit(text, request_id=request_id, goal=goal)
        return await turn.wait(timeout)

    async def wait(self, timeout=None, *, until="actionable"):
        if timeout is not None and (
            not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout < 0
        ):
            raise ValueError("Invalid wait timeout.")
        if until not in {"actionable", "settled"}:
            raise ValueError("Unknown wait condition.")
        expires = None if timeout is None else time.monotonic() + timeout
        while True:
            snapshot = await self.snapshot()
            done = snapshot.actionable if until == "actionable" else snapshot.settled
            if done:
                return WaitReport(snapshot, True)
            if expires is not None and time.monotonic() >= expires:
                return WaitReport(snapshot, False, True)
            await asyncio.sleep(
                0.05
                if expires is None
                else min(0.05, max(0, expires - time.monotonic()))
            )

    async def watch(self, *, interval=0.1):
        if (
            not isinstance(interval, (int, float))
            or not math.isfinite(interval)
            or interval <= 0
        ):
            raise ValueError("Invalid watch interval.")
        previous = None
        while True:
            snapshot = await self.snapshot()
            identity = digest(snapshot.data())
            if identity != previous:
                previous = identity
                yield ExecutionEvent(identity, "snapshot", snapshot)
            if snapshot.settled:
                return
            await asyncio.sleep(interval)


class AsyncHarness:
    def __init__(self, *, workdir=None, config=None):
        self._sync = Harness(workdir=workdir, config=config)
        self._calls = _Calls()
        for name in (
            "sessions",
            "runs",
            "workflows",
            "interactions",
            "requests",
            "admin",
        ):
            setattr(self, name, _View(self, getattr(self._sync, name)))

    def _wrap(self, value):
        return AsyncHandle(self, value) if isinstance(value, Handle) else value

    async def close(self, timeout=None):
        seconds = (
            self._sync.config.settings.shutdown_seconds if timeout is None else timeout
        )
        if (
            type(seconds) not in {int, float}
            or not math.isfinite(seconds)
            or seconds < 0
        ):
            raise ValueError("Invalid close timeout.")
        self._calls.closed = True
        report = await self._calls.call(self._sync.close, seconds, critical=True)
        if self._calls.busy():
            return CloseReport(
                "cleanup_pending",
                False,
                False,
                report.pending,
                report.errors,
                max(1, report.pending_calls),
            )
        return report

    async def wait_closed(self, timeout=5):
        if self._calls.closed:
            return await self.close(timeout)
        return await self._calls.call(self._sync.wait_closed, timeout, critical=True)

    async def __aenter__(self):
        self._sync.check_open()
        return self

    async def __aexit__(self, kind, error, tb):
        report = await self.close()
        if report.state == "cleanup_pending" and kind is None:
            raise ClosePending(report)
        if report.state == "error" and kind is None:
            raise SDKError(
                "Owned resources closed with a persistence error.",
                code="close_failed",
                details=report.data(),
            )
