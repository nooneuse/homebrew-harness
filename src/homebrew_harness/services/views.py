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
                "changeset_id": value.get("changeset_id"),
                "workspace_sync": value.get("workspace_sync"),
                "ci_fix_attempts": value.get("ci_fix_attempts", 0),
                "authority": value.get("authority"),
                "verification": value.get("verification"),
                "triage": value.get("triage_summary"),
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

    def revise(self, text, **kw):
        return self._control("work_revise", text=text, **kw)

    def request_authority(self, **kw):
        return self._control("work_request_authority", **kw)

    def revoke(self, **kw):
        return self._control("authority_revoke", **kw)

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
    def health(self):
        return self.query("ops_health")

    def operations(self, *, cursor=0, limit=20):
        return self.query("ops_list", {"cursor": cursor, "limit": limit})

    def operation(self, identity):
        return self.query("ops_get", {"id": identity})

    def operation_record(self, identity, *, offset=0, limit=4000):
        return self.query(
            "ops_read", {"id": identity, "offset": offset, "limit": limit}
        )

    def quiesce(self, *, request_id=None):
        return self.command("ops_quiesce", {}, request_id)

    def resume_operations(self, *, request_id=None):
        return self.command("ops_resume_operations", {}, request_id)

    def backup(self, *, request_id=None):
        return self.command("ops_backup", {}, request_id)

    def verify_backup(self, identity, *, request_id=None):
        return self.command("ops_verify_backup", {"id": identity}, request_id)

    def restore_backup(self, identity, *, request_id=None):
        return self.command("ops_restore_backup", {"id": identity}, request_id)

    def retention_preview(self, *, request_id=None):
        return self.command("ops_retention_preview", {}, request_id)

    def retention_apply(self, identity, *, request_id=None):
        return self.command("ops_retention_apply", {"id": identity}, request_id)

    def audit(self, *, force=False, request_id=None):
        return self.command("ops_audit", {"force": force}, request_id)

    def diagnose(self, identity, *, request_id=None):
        return self.command("ops_diagnose", {"id": identity}, request_id)

    def retry_operation(self, identity, *, request_id=None):
        return self.command("ops_retry", {"id": identity}, request_id)

    def prepare_upgrade(self, alias, *, request_id=None):
        return self.command("ops_upgrade_prepare", {"alias": alias}, request_id)

    def arm_upgrade(self, identity, *, request_id=None):
        return self.command("ops_upgrade_arm", {"id": identity}, request_id)

    def confirm_upgrade(self, identity, *, request_id=None):
        return self.command("ops_upgrade_confirm", {"id": identity}, request_id)

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
                        ExecutionRef(
                            ref["target"]["kind"],
                            ref["target"]["id"],
                            self.id if ref["target"]["kind"] == "run" else None,
                        ),
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
        if ref.target.kind == "service":
            if ref.target.id != self.id or not ref.durable or set(values) - {"text"}:
                raise ValueError("Invalid service authority target or decision fields.")
            return self.command(
                "service_decide",
                {
                    "id": ref.id,
                    "revision": ref.revision,
                    "decision": decision,
                    **values,
                },
                control_id,
            )
        return self.command(
            "decide", {"ref": ref.data(), "decision": decision, **values}, control_id
        )

    def delivery(self, identity, *, kind="changesets"):
        return self.query("delivery_status", {"kind": kind, "id": identity})

    def prepare_delivery(self, work_id, *, revision=None, request_id=None):
        revision = (
            self.work.get(work_id).snapshot().revision if revision is None else revision
        )
        return self.command(
            "delivery_prepare", {"id": work_id, "revision": revision}, request_id
        )

    def plan_delivery(
        self, identity, kind, *, revision=None, body=None, subject=None, request_id=None
    ):
        revision = self.delivery(identity)["revision"] if revision is None else revision
        return self.command(
            "delivery_plan",
            {
                "id": identity,
                "revision": revision,
                "kind": kind,
                "body": body,
                "subject": subject,
            },
            request_id,
        )

    def inspect_delivery(
        self, identity, *, conclusion="reconcile", confirmed=False, request_id=None
    ):
        return self.command(
            "delivery_inspect",
            {"id": identity, "conclusion": conclusion, "confirmed": confirmed},
            request_id,
        )

    def request_delivery(self, identity, *, request_id=None):
        return self.command("delivery_request", {"id": identity}, request_id)

    def refresh_delivery(self, identity, *, request_id=None):
        return self.command("delivery_refresh", {"id": identity}, request_id)

    def batch(self, requests, *, request_id=None):
        return self.command(
            "batch_create",
            {
                "refs": [
                    r.ref.data()
                    if hasattr(r, "ref")
                    else r.data()
                    if hasattr(r, "data")
                    else r
                    for r in requests
                ]
            },
            request_id,
        )

    def decide_batch(self, identity, decisions, *, revision=0, request_id=None):
        return self.command(
            "batch_decide",
            {"id": identity, "revision": revision, "decisions": decisions},
            request_id,
        )

    def records(self, kind, *, cursor=0, limit=20, state=None):
        return self.query(
            "service_page",
            {"kind": kind, "cursor": cursor, "limit": limit, "state": state},
        )

    def record(self, kind, identity, *, offset=0, limit=4000):
        return self.query(
            "service_read",
            {"kind": kind, "identity": identity, "offset": offset, "limit": limit},
        )

    def converse(
        self, text, *, context="default", work_id=None, revision=None, request_id=None
    ):
        return self.command(
            "conversation_submit",
            {
                "text": text,
                "context": context,
                "work_id": work_id,
                "revision": revision,
            },
            request_id,
        )

    def conversation(self, identity):
        return self.query("conversation_get", {"id": identity})

    def reports(self, *, context="default", cursor=0, limit=20):
        return self.query(
            "report_inbox", {"context": context, "cursor": cursor, "limit": limit}
        )

    def report(self, identity=None, *, request_id=None):
        return (
            self.query("report_get", {"id": identity})
            if identity
            else self.command("report_create", {}, request_id)
        )

    def acknowledge_report(self, identity, *, context="default", request_id=None):
        return self.command(
            "report_ack", {"id": identity, "context": context}, request_id
        )

    def retry_job(self, identity, *, request_id=None):
        return self.command("job_retry", {"id": identity}, request_id)

    def job(self, identity):
        return self.query("job_get", {"id": identity})

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
