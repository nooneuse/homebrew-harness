"""Live identities for synchronous work; durable outcomes stay with their owners."""

import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

_CURRENT = ContextVar("interactive_activities", default=None)


class Activities:
    def __init__(self):
        self.lock = threading.RLock()
        self.live = {}
        self.recent = []

    def snapshot(self):
        with self.lock:
            return [dict(row, state="stopping" if event.is_set() else row["state"], elapsed=max(0, time.monotonic() - row["started"]))
                for row, event in self.live.values()] + [dict(row) for row in self.recent]

    def stop(self, identity):
        with self.lock:
            item = self.live.get(identity)
            if item is None:
                raise ValueError("This operation is no longer active.")
            item[1].set()
            return {"id": identity, "state": "stop_requested"}


@contextmanager
def activity_scope(registry):
    token = _CURRENT.set(registry)
    try:
        yield
    finally:
        _CURRENT.reset(token)


@contextmanager
def activity(kind, *, identity=None, name="", workdir=None, model=None):
    registry = _CURRENT.get()
    cancel = threading.Event()
    identity = identity or "activity_" + uuid.uuid4().hex
    row = {"id": identity, "kind": kind, "name": name, "model": model,
        "cwd": str(workdir) if workdir else None, "state": "running",
        "started": time.monotonic(), "stoppable": True}
    if registry:
        with registry.lock:
            registry.live[identity] = (row, cancel)
    failed = False
    try:
        yield cancel
    except BaseException:
        failed = True
        raise
    finally:
        if registry:
            with registry.lock:
                registry.live.pop(identity, None)
                registry.recent.append({**row, "state": "returned", "cancel_requested": cancel.is_set(), "failed": failed,
                    "elapsed": max(0, time.monotonic() - row["started"]), "stoppable": False})
                registry.recent[:] = registry.recent[-20:]
