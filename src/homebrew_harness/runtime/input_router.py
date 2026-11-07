"""One input owner routes exact approval IDs and ordinary user steering."""

import copy
import json
import threading
import uuid
from concurrent.futures import Future
from contextlib import contextmanager
from contextvars import ContextVar

from homebrew_harness.execution.execution_control import operation_deadline, wait_future
from homebrew_harness.security.data_boundary import redact

_BROKER = ContextVar("host_approval_broker", default=None)


def current_broker():
    return _BROKER.get()


@contextmanager
def approval_scope(broker):
    token = _BROKER.set(broker)
    try:
        yield
    finally:
        _BROKER.reset(token)


class ApprovalBroker:
    def __init__(self, *, trusted=False, announce=None, owner=None):
        self.trusted, self.announce = trusted, announce or print
        self.lock = threading.Lock()
        self.pending = {}
        self.closed = False
        self.owner = owner

    def request(self, tool_name, arguments, workdir, reason):
        from homebrew_harness.security.permissions import PermissionDecision

        if not self.trusted:
            return PermissionDecision(
                "deny", "No authenticated interactive approval input is available."
            )
        identity = "approval_" + uuid.uuid4().hex
        future = Future()
        with self.lock:
            if self.closed:
                return PermissionDecision("deny", "Host input is closing.")
            self.pending[identity] = (
                future,
                copy.deepcopy(arguments),
                self.owner.inputs.active_id if self.owner else None,
            )
        self.announce(
            "[permission] "
            + redact(
                json.dumps(
                    {
                        "id": identity,
                        "tool": tool_name,
                        "arguments": arguments,
                        "directory": str(workdir),
                        "reason": reason,
                    },
                    ensure_ascii=False,
                )
            )
            + "\nUse /approve ID yes or /approve ID no."
        )
        try:
            return wait_future(future, operation_deadline(120))
        finally:
            with self.lock:
                self.pending.pop(identity, None)

    def respond(self, identity, approved):
        from homebrew_harness.security.permissions import PermissionDecision

        if type(approved) is not bool:
            raise ValueError("Approval must be an explicit boolean host response.")
        with self.lock:
            value = self.pending.get(identity)
            if value is None or value[0].done():
                raise ValueError("Approval ID is stale or unknown.")
            value[0].set_result(
                PermissionDecision(
                    "allow" if approved else "deny",
                    "Host answered this exact pending invocation.",
                )
            )

    def close(self):
        from homebrew_harness.security.permissions import PermissionDecision

        with self.lock:
            self.closed = True
            for future, *_ in self.pending.values():
                if not future.done():
                    future.set_result(PermissionDecision("deny", "Host input closed."))

    def supersede_turn(self, turn_id):
        from homebrew_harness.security.permissions import PermissionDecision

        with self.lock:
            for future, _, target in self.pending.values():
                if target == turn_id and not future.done():
                    future.set_result(
                        PermissionDecision(
                            "deny", "User steering superseded this pending approval."
                        )
                    )
