"""One monotonic deadline/cancellation contract for host tools and transports."""

import threading
from contextlib import contextmanager
from contextvars import ContextVar

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


@contextmanager
def cleanup_scope(seconds=2):
    token = _CLEANUP.set(_CLEANUP.get() or Deadline.after(seconds, source="cleanup"))
    try:
        yield
    finally:
        _CLEANUP.reset(token)


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
    independent = _CLEANUP.get() or _CONTROL.get()
    scope = current_scope()
    parent = independent or (
        scope.gateway.operation_deadline() if scope.gateway else scope.deadline
    )
    return parent.intersect(Deadline.after(timeout, source="tool"))


def check(deadline=None, cancel_events=(), *, inherit=True, gateway=True):
    scope = current_scope() if inherit and not _CLEANUP.get() else RequestScope()
    if _CONTROL.get() and not _CLEANUP.get():
        scope = RequestScope(deadline=_CONTROL.get(), cancel_events=scope.cancel_events)
    if any(
        event.is_set()
        for event in (
            *scope.cancel_events,
            *cancel_events,
            *(_CONTROL_CANCEL.get() if not _CLEANUP.get() else ()),
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
