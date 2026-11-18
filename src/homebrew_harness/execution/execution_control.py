"""One monotonic deadline/cancellation contract for host tools and transports."""

import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from homebrew_harness.configuration import DEFAULT_PERSISTENCE_SECONDS, DEFAULT_SHUTDOWN_SECONDS

from homebrew_harness.execution.model_gateway import (
    Deadline,
    ModelCancelled,
    ModelDeadlineExceeded,
    RequestScope,
    current_scope,
)

_CLEANUP = ContextVar("execution_cleanup", default=None)
_CONTROL = ContextVar("execution_host_control", default=None)
_CONTROL_CANCEL = ContextVar("execution_control_cancel", default=())
_PERSISTENCE = ContextVar("execution_persistence", default=None)


@contextmanager
def cleanup_scope(seconds=DEFAULT_SHUTDOWN_SECONDS):
    deadline = Deadline.after(seconds, source="cleanup")
    if _CLEANUP.get() is not None:
        deadline = deadline.intersect(_CLEANUP.get())
    token = _CLEANUP.set(deadline)
    try:
        yield deadline
    finally:
        _CLEANUP.reset(token)


def current_persistence():
    return _PERSISTENCE.get()


@contextmanager
def persistence_scope(seconds=DEFAULT_PERSISTENCE_SECONDS, *, operation="state_commit", context_id=None):
    previous = _PERSISTENCE.get()
    deadline = Deadline.after(seconds, source="persistence")
    started = time.monotonic()
    if previous is not None:
        selected = previous["deadline"].intersect(deadline)
        if selected is previous["deadline"]:
            seconds, started = previous["seconds"], previous["started"]
        deadline = selected
        context_id = context_id or previous.get("context_id")
    if _CLEANUP.get() is not None:
        deadline = deadline.intersect(_CLEANUP.get())
    token = _PERSISTENCE.set({"deadline": deadline, "seconds": seconds,
                              "operation": operation, "context_id": context_id, "started": started})
    try:
        yield deadline
    finally:
        _PERSISTENCE.reset(token)


@contextmanager
def control_scope(deadline, *, cancel_events=()):
    token = _CONTROL.set(deadline)
    cancellation = _CONTROL_CANCEL.set((*_CONTROL_CANCEL.get(), *cancel_events))
    try:
        yield
    finally:
        _CONTROL.reset(token)
        _CONTROL_CANCEL.reset(cancellation)


def operation_deadline(timeout=None):
    persistence = _PERSISTENCE.get()
    independent = _CLEANUP.get()
    if persistence is not None:
        independent = persistence["deadline"].intersect(independent) if independent else persistence["deadline"]
    independent = independent or _CONTROL.get()
    scope = current_scope()
    parent = independent or (
        scope.gateway.operation_deadline() if scope.gateway else scope.deadline
    )
    return parent.intersect(Deadline.after(timeout, source="tool"))


def check(deadline=None, cancel_events=(), *, inherit=True, gateway=True):
    independent = _CLEANUP.get() is not None or _PERSISTENCE.get() is not None
    scope = current_scope() if inherit and not independent else RequestScope()
    if _CONTROL.get() and not independent:
        scope = RequestScope(deadline=_CONTROL.get(), cancel_events=scope.cancel_events)
    if any(
        event.is_set()
        for event in (
            *scope.cancel_events,
            *cancel_events,
            *(_CONTROL_CANCEL.get() if not independent else ()),
        )
    ):
        raise ModelCancelled("Execution cancelled; inspect any action already started.")
    if scope.gateway and gateway:
        scope.gateway.checkpoint()
    if (deadline or operation_deadline()).remaining() == 0:
        raise ModelDeadlineExceeded(
            "Execution deadline reached; inspect any action already started.",
            source=(deadline or operation_deadline()).source,
        )


@contextmanager
def acquired(lock, deadline=None, *, cancel_events=(), inherit=True):
    deadline = deadline or operation_deadline(120)
    while True:
        check(deadline, cancel_events, inherit=inherit, gateway=False)
        remaining = deadline.remaining()
        if lock.acquire(
            timeout=min(0.05, remaining) if remaining is not None else 0.05
        ):
            break
    try:
        check(deadline, cancel_events, inherit=inherit, gateway=False)
        yield
    finally:
        lock.release()


def wait_future(future, deadline, *, cancel_events=(), inherit=True):
    while True:
        check(deadline, cancel_events, inherit=inherit, gateway=False)
        try:
            return future.result(
                timeout=min(0.05, deadline.remaining())
                if deadline.remaining() is not None
                else 0.05
            )
        except TimeoutError:
            if future.done():
                return future.result()


class ControlledRLock:
    """RLock API with bounded context-manager waits; explicit nonblocking stays nonblocking."""

    def __init__(self):
        self.lock = threading.RLock()

    def acquire(self, blocking=True, timeout=-1):
        if not blocking or timeout != -1:
            return self.lock.acquire(blocking, timeout)
        deadline = operation_deadline(120)
        while True:
            check(deadline, gateway=False)
            if self.lock.acquire(timeout=min(0.05, deadline.remaining())):
                return True

    def release(self):
        self.lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_):
        self.release()
