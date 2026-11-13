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
        self.requests = {}
        self.closed = False
        self.owner = owner
        self.review_cancels = {}

    def request(self, tool_name, arguments, workdir, reason):
        from homebrew_harness.security.permissions import PermissionDecision

        reviewer = getattr(self.owner, "approval_reviewer", None)
        automatic = reviewer is not None and reviewer.config.mode == "auto_review" and "tool" in reviewer.config.kinds
        if not self.trusted and not automatic:
            return PermissionDecision(
                "deny", "No authenticated interactive approval input is available."
            )
        identity = "approval_" + uuid.uuid4().hex
        future = Future()
        with self.lock:
            if self.closed:
                return PermissionDecision("deny", "Host input is closing.")
            self.review_cancels[identity] = threading.Event()
            self.pending[identity] = (
                future,
                copy.deepcopy(arguments),
                self.owner.inputs.active_id if self.owner else None,
            )
            self.requests[identity] = {
                "id": identity,
                "revision": 0,
                "kind": "tool",
                "tool": tool_name,
                "arguments": copy.deepcopy(arguments),
                "directory": str(workdir),
                "turn_id": self.owner.inputs.active_id if self.owner else None,
            }
        review_context = None
        review_identity = None
        if automatic:
            from homebrew_harness.security.approval_review import requester_binding
            from homebrew_harness.security.review_context import session_context
            requester = requester_binding()
            try:
                review_context = session_context(self.owner, tool_name, arguments, workdir, requester=requester)
                request = {"id": identity, "revision": 0, "kind": "tool", "tool": tool_name, "arguments": copy.deepcopy(arguments), "reason": reason, "checkpoint": self.owner.checkpoint_key}
                def current():
                    with self.lock:
                        valid = identity in self.pending and not self.pending[identity][0].done() and not self.closed
                    return session_context(self.owner, tool_name, arguments, workdir, requester=requester) if valid else None
                def publish(decision, review_id):
                    nonlocal review_identity
                    review_identity = review_id
                    with self.lock:
                        value = self.pending.get(identity)
                        if value is None or value[0].done() or self.closed or session_context(self.owner, tool_name, arguments, workdir, requester=requester) != review_context:
                            raise ValueError("Auto-review request changed before publication.")
                        value[0].set_result(PermissionDecision("allow" if decision == "approve" else "deny", "Independent approval review " + review_id, "auto_review"))
                        return {"decision": decision}
                reviewer.review(request, review_context, current, publish, requester=requester, cancel_event=self.review_cancels[identity])
            except Exception:
                pass
        if future.done():
            result = future.result()
            if result.action == "allow" and result.code == "auto_review" and review_context is not None:
                from homebrew_harness.security.review_context import bind_guard
                from homebrew_harness.security.permissions import _APPROVAL_CALL
                bind_guard(_APPROVAL_CALL.get(), lambda:
                    session_context(self.owner, tool_name, arguments, workdir, requester=requester) == review_context
                    and reviewer.execution_evidence_current(review_identity, workdir))
            with self.lock:
                self.pending.pop(identity, None); self.requests.pop(identity, None); self.review_cancels.pop(identity, None)
            return result
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
                self.requests.pop(identity, None)
                self.review_cancels.pop(identity, None)

    def records(self):
        with self.lock:
            return [
                copy.deepcopy(self.requests[key])
                for key, value in self.pending.items()
                if not value[0].done()
            ]

    def respond(self, identity, approved, *, revision=None, before=None):
        from homebrew_harness.security.permissions import PermissionDecision

        if type(approved) is not bool:
            raise ValueError("Approval must be an explicit boolean host response.")
        if not self.trusted:
            raise ValueError("Only trusted host input can answer an approval.")
        with self.lock:
            value = self.pending.get(identity)
            if value is None or value[0].done():
                raise ValueError("Approval ID is stale or unknown.")
            if revision is not None and self.requests[identity]["revision"] != revision:
                raise ValueError("Approval revision changed.")
            if before is not None:
                before()
            if identity in self.review_cancels:
                self.review_cancels[identity].set()
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
            for cancel in self.review_cancels.values():
                cancel.set()
            for future, *_ in self.pending.values():
                if not future.done():
                    future.set_result(PermissionDecision("deny", "Host input closed."))

    def supersede_turn(self, turn_id):
        from homebrew_harness.security.permissions import PermissionDecision

        with self.lock:
            for identity, (future, _, target) in self.pending.items():
                if target == turn_id and not future.done():
                    self.review_cancels[identity].set()
                    future.set_result(
                        PermissionDecision(
                            "deny", "User steering superseded this pending approval."
                        )
                    )


def short_decision(text):
    """Full matches only; mixed prose remains task text, never authorization."""
    text = text.strip().casefold()
    if text in {
        "yes",
        "y",
        "approve",
        "允许",
        "允许这次操作",
        "批准这次操作",
        "同意这次操作",
    }:
        return "approve"
    if text in {"no", "n", "deny", "拒绝", "拒绝这次操作", "不允许", "不同意"}:
        return "deny"
    return None
