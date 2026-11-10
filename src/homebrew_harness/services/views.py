"""Transport-independent work views without execution or database imports."""

import uuid
from .protocol import request_key
from homebrew_harness.observation import Handle
from homebrew_harness.contracts import ExecutionRef, ExecutionSnapshot, ExecutionResult


class WorkHandle(Handle):
    def __init__(self, service, identity):
        super().__init__(service.core, ExecutionRef("work", identity, service.id))
        self.service = service

    def snapshot(self):
        value = self.service.query("work_get", {"id": self.id})
        execution = value.get("execution") or {}
        state = value["state"]
        if state == "in_progress":
            state = execution.get("state", "starting")
            if state in {"completed", "failed", "cancelled"}:
                state = "settling"
        pending = value["state"] == "in_progress" and (
            state == "settling" or execution.get("execution_pending", True)
        )
        return ExecutionSnapshot(
            self.ref,
            state,
            value["revision"],
            value.get("error") or "",
            value["requirements_revision"],
            output_available="result" in value
            or value["state"] in {"cancelled", "failed"}
            and value["run_id"] is None,
            persistence_confirmed=True,
            execution_pending=pending,
            details={
                "run_id": value["run_id"],
                "native_execution": execution,
                "pause_requested": value["pause_requested"],
                "prompt_preview": value.get("prompt_preview", ""),
                "input_length": value.get("input_length"),
            },
        )

    def result(self):
        self._require_result()
        value = self.service.query("work_get", {"id": self.id})
        result = value.get("result") or {
            "reason_code": value.get("error") or "cancelled_before_launch"
        }
        return ExecutionResult(
            self.ref,
            value["state"],
            text=result.get("text"),
            value=result,
        )

    def requests(self):
        return self.service.requests(self.id)

    def input(self, *, offset=0, limit=4000, amendment=None):
        return self.service.query(
            "work_input",
            {"id": self.id, "offset": offset, "limit": limit, "amendment": amendment},
        )

    def _control(self, method, *, revision=None, text=None, request_id=None):
        revision = self.snapshot().revision if revision is None else revision
        payload = {"id": self.id, "revision": revision}
        if text is not None:
            payload["text"] = text
        return self.service.command(method, payload, request_id)

    def pause(self, **kw):
        return self._control("work_pause", **kw)

    def resume(self, **kw):
        return self._control("work_resume", **kw)

    def cancel(self, **kw):
        return self._control("work_cancel", **kw)

    def steer(self, text, **kw):
        return self._control("steer", text=text, **kw)

    def interrupt(self, text, **kw):
        return self._control("interrupt", text=text, **kw)


class WorkService:
    _api_service = True

    def __init__(self, service):
        self.service = service

    def submit(self, text, *, request_id=None, goal=None, priority=0):
        value = self.service.command(
            "submit", {"text": text, "goal": goal, "priority": priority}, request_id
        )
        return WorkHandle(self.service, value["id"])

    def get(self, identity):
        self.service.query("work_get", {"id": identity})
        return WorkHandle(self.service, identity)

    def list(self, *, state=None, cursor=0, limit=20):
        return self.service.query(
            "work_list", {"state": state, "cursor": cursor, "limit": limit}
        )


class ServiceControls:
    def requests(self, work_id=None):
        from homebrew_harness.contracts import HumanRequest, RequestRef

        values = self.query("requests", {"work_id": work_id} if work_id else {})
        result = []
        for value in values:
            ref = value.pop("ref")
            result.append(
                HumanRequest(
                    RequestRef(
                        ref["id"],
                        ref["revision"],
                        ExecutionRef("run", ref["target"]["id"], self.id),
                        ref["durable"],
                    ),
                    **value,
                )
            )
        return tuple(result)

    def decide(self, ref, decision, *, decision_id=None, **values):
        if hasattr(ref, "ref"):
            ref = ref.ref
        decision_id = decision_id or "decision_" + uuid.uuid4().hex
        control_id = "decision_" + request_key("decision", str(ref.data()), decision_id)
        return self.command(
            "decide", {"ref": ref.data(), "decision": decision, **values}, control_id
        )

    def schedules(self, *, cursor=0, limit=20):
        return self.query("schedules", {"cursor": cursor, "limit": limit})

    def receipt(self, request_id):
        return self.query("receipt", {"request_id": request_id})

    def tick(self, *, request_id=None):
        return self.command("tick", {}, request_id)

    def focus(
        self, identity=None, *, context="default", revision=None, request_id=None
    ):
        if identity is not None and revision is None:
            revision = self.work.get(identity).snapshot().revision
        return self.command(
            "focus",
            {"context": context, "id": identity, "revision": revision},
            request_id,
        )

    def current_focus(self, *, context="default"):
        return self.query("focus_get", {"context": context})

    def pause(self, scope="execution", *, revision=None, request_id=None):
        return self.command(
            "pause",
            {
                "scope": scope,
                "revision": self.snapshot().revision if revision is None else revision,
            },
            request_id,
        )

    def resume(self, *, revision=None, request_id=None):
        return self.command(
            "resume",
            {"revision": self.snapshot().revision if revision is None else revision},
            request_id,
        )

    def stop(self, *, revision=None, request_id=None):
        return self.command(
            "stop",
            {"revision": self.snapshot().revision if revision is None else revision},
            request_id,
        )
