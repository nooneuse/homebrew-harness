"""Small shared ownership rules for threads, polling and deferred shutdown."""

import math
import threading
import time
from contextvars import copy_context


class ClosePending(RuntimeError):
    """An owned close action has not completed; the host can retry it."""


class CloseProgress:
    """Completion of critical actions, independent of diagnostic observers."""

    def __init__(self):
        self.lock = threading.RLock()
        self.done = set()
        self.terminal = set()
        self.errors = []
        self.history = []

    def record_error(self, name, error):
        record = {"component": name, "error_type": type(error).__name__}
        with self.lock:
            self.errors[:] = [item for item in self.errors if item["component"] != name]
            self.errors.append(record)
            self.history.append(record)

    def step(self, name, action, *, repeat=False):
        with self.lock:
            if name in self.done and not repeat:
                return True
            if name in self.terminal and not repeat:
                return False
        try:
            action()
        except Exception as error:
            with self.lock:
                self.done.discard(name)
                self.record_error(name, error)
            return False
        with self.lock:
            self.done.add(name)
            self.terminal.discard(name)
            self.errors[:] = [item for item in self.errors if item["component"] != name]
        return True

    def require(self, names):
        with self.lock:
            pending = [
                name
                for name in names
                if name not in self.done and name not in self.terminal
            ]
        if pending:
            raise ClosePending(
                "Critical close actions remain pending: " + ", ".join(pending)
            )

    def terminal_failure(self, name):
        """The caller proved resources ended and can persist a failed close outcome."""
        with self.lock:
            if not any(item["component"] == name for item in self.errors):
                raise ValueError("A terminal close error must have a recorded failure.")
            self.terminal.add(name)

    def snapshot(self):
        with self.lock:
            return {
                "completed_steps": sorted(self.done),
                "failed_steps": sorted(self.terminal),
                "errors": list(self.errors),
                "history": list(self.history),
            }


class WorkAccepted(RuntimeError):
    outcome = "unknown"

    def __init__(self, thread):
        super().__init__(
            "Worker launch was interrupted after acceptance; execution remains owned."
        )
        self.thread = thread


class Workers:
    def __init__(self):
        self.condition = threading.Condition()
        self.threads = set()

    def start(
        self,
        target,
        *args,
        name="harness-worker",
        on_created=None,
        track_operation=True,
    ):
        from homebrew_harness.storage.durable_runtime import (
            settle_activity,
            track_activity,
        )

        context = copy_context()

        def run():
            try:
                context.run(target, *args)
            finally:
                settle_activity(operation, thread)
                with self.condition:
                    self.threads.discard(thread)
                    self.condition.notify_all()

        thread = threading.Thread(target=run, name=name, daemon=True)
        operation = track_activity(thread) if track_operation else None
        with self.condition:
            self.threads.add(thread)
        try:
            if on_created:
                on_created(thread)
            thread.start()
        except BaseException:
            if thread.ident is None:
                settle_activity(operation, thread)
                with self.condition:
                    self.threads.discard(thread)
                    self.condition.notify_all()
                raise
            raise WorkAccepted(thread)
        return thread

    def wait(self, timeout=5):
        end = None if timeout is None else time.monotonic() + max(0, timeout)
        with self.condition:
            while self.threads:
                if end is not None and time.monotonic() >= end:
                    return False
                self.condition.wait(
                    0.05 if end is None else min(0.05, max(0, end - time.monotonic()))
                )
        return True


class Shutdown:
    """One accepted cleanup job; a failed launch can be retried by the host."""

    def __init__(self):
        self.lock = threading.Lock()
        self.started = False
        self.finished = threading.Event()
        self.error = None
        self.workers = Workers()

    def request(self, action, timeout=5):
        with self.lock:
            if not self.started and not self.finished.is_set():

                def finish():
                    try:
                        action()
                    except BaseException as error:
                        with self.lock:
                            self.started = False
                            self.error = type(error).__name__
                    else:
                        self.finished.set()

                self.started = True
                try:
                    self.workers.start(
                        finish, name="harness-shutdown", track_operation=False
                    )
                    self.error = None
                except WorkAccepted:
                    self.error = None
                except BaseException as error:
                    self.started = False
                    self.error = type(error).__name__
        return self.finished.wait(max(0, timeout))


class Poller:
    def __init__(self, stop_event=None):
        self.stop = stop_event or threading.Event()
        self.workers = Workers()
        self.started = False

    def start(self, jobs, on_error=None):
        if self.started:
            return
        self.started = True
        try:
            for callback, interval, name in jobs:
                if (
                    not isinstance(interval, (int, float))
                    or not math.isfinite(interval)
                    or interval <= 0
                ):
                    raise ValueError(
                        "Polling interval must be finite positive seconds."
                    )

                def loop(callback=callback, interval=interval):
                    while not self.stop.wait(interval):
                        try:
                            callback()
                        except Exception as error:
                            if on_error:
                                on_error(error)

                self.workers.start(loop, name=name, track_operation=False)
        except BaseException:
            self.stop.set()
            self.started = False
            raise

    def close(self, timeout=2):
        self.stop.set()
        return self.workers.wait(timeout)
