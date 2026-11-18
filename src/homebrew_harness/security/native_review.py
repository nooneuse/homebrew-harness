"""Adapt durable Run requests to the shared reviewer and original decision publisher."""

import threading
import time
from contextlib import contextmanager
from homebrew_harness.configuration import DEFAULT_PERSISTENCE_SECONDS
from homebrew_harness.execution.execution_control import check
from homebrew_harness.execution.model_gateway import Deadline
from homebrew_harness.security.approval_review import requester_binding


class NativeReviewGate:
    """Live native review ownership registered before its request is published."""

    def __init__(self, control):
        self.control = control
        self.changed = threading.Condition()
        self.active = {}

    @contextmanager
    def creating(self, kind):
        session = self.control.session
        reviewer = getattr(session, "approval_reviewer", None)
        if reviewer is None or reviewer.config.mode != "auto_review" or kind not in reviewer.config.kinds:
            yield None
            return
        token = object()
        activity = {"request": None, "cancel": threading.Event(),
                    "deadline": self.control.deadline().intersect(Deadline.after(
                        reviewer.config.timeout_seconds + DEFAULT_PERSISTENCE_SECONDS, source="tool"))}
        with self.changed:
            self.active[token] = activity
        try:
            yield activity
        finally:
            with self.changed:
                self.active.pop(token, None)
                self.changed.notify_all()

    def wait(self):
        waited = False
        while True:
            with self.changed:
                activities = list(self.active.values())
            if not activities:
                return waited
            waited = True
            self.control.safe_point(reviewing=True)
            for activity in activities:
                request = activity["request"]
                if request is not None:
                    latest = self.control.requests.get(self.control.id, request["id"])
                    if latest["state"] != "pending" or latest["revision"] != request["revision"] or latest["expires_at"] <= time.time():
                        activity["cancel"].set()
                try:
                    check(activity["deadline"], (self.control.cancel, self.control.session.stop_event),
                          inherit=False, gateway=False)
                except BaseException:
                    activity["cancel"].set()
                    raise
            with self.changed:
                if self.active:
                    self.changed.wait(0.05)



def review_run(control, request, *, cancel_event=None):
    session = control.session
    reviewer = getattr(session, "approval_reviewer", None)
    if reviewer is None:
        return request
    if (
        request["state"] != "pending"
        or reviewer.config.mode != "auto_review"
        or request["kind"] not in reviewer.config.kinds
    ):
        return request
    requester = requester_binding()
    cwd = control.store.root / request["context"].get("cwd", ".")

    def host_scope(binding):
        scope = {
            "request_binding": binding,
            "write_paths": list(control.spec.write_paths),
            "tools": sorted(control.spec.effective_tools()),
            "preauthorized_tests": list(control.spec.allow_tests),
            "goal": control.spec.goal,
            "mcp_delegation": control.spec.mcp_delegation,
            "bash_backend": session.bash_policy.identity(),
        }
        if binding.get("task_id") and control.stages:
            scope["stage"] = control.stages.definition(
                session.workspace.store.load(binding["task_id"])
            )
        return scope

    snapshot = {
        "cwd": str(cwd),
        "intent": session.intent.to_dict(),
        "host_scope": host_scope(request["context"]),
        "lead_binding": session.lead_binding.identity(),
        "policy": reviewer.config.data(),
    }

    def current():
        value = control.requests.get(control.id, request["id"])
        if value["state"] != "pending" or value["revision"] != request["revision"]:
            return None
        if request["kind"] == "tool":
            from homebrew_harness.runs.requests import context

            scope = context(control, request["tool"], request["arguments"])
        else:
            from homebrew_harness.runs.test_authorization import valid

            if not valid(
                control.store, control.store.get(control.id), request["context"]
            ):
                return None
            scope = request["context"]
        return {
            "cwd": str(cwd),
            "intent": session.intent.to_dict(),
            "host_scope": host_scope(scope),
            "lead_binding": session.lead_binding.identity(),
            "policy": reviewer.config.data(),
        }

    def publish(decision, review_id):
        if request["kind"] == "tool":
            session._save_checkpoint()
        return control.requests.decide(
            control.id, request["id"], request["revision"], decision
        )

    reviewer.review(request, snapshot, current, publish, requester=requester, cancel_event=cancel_event)
    return control.requests.get(control.id, request["id"])
