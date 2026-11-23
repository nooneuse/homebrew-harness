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
                "error_info": value.get("error_info"),
                "changeset_id": value.get("changeset_id"),
                "workspace_sync": value.get("workspace_sync"),
                "ci_fix_attempts": value.get("ci_fix_attempts", 0),
                "progress": value.get("progress"),
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

    def retry(self, *, seconds=None, revision=None, request_id=None):
        revision = self.snapshot().revision if revision is None else revision
        return self.service.command(
            "work_retry",
            {"id": self.id, "revision": revision, "seconds": seconds},
            request_id,
        )

    def cancel(self, **kw):
        return self._control("work_cancel", **kw)

    def steer(self, text, **kw):
        return self._control("steer", text=text, **kw)

    def context(self, text, **kw):
        return self._control("work_context", text=text, **kw)

    def plan_recovery(self, text="", *, execute=False, revision=None, request_id=None):
        """Request a read-only Lead proposal, without resuming execution."""
        return self.service.command(
            "work_recovery_plan",
            {
                "id": self.id,
                "revision": self.snapshot().revision if revision is None else revision,
                "text": text,
                "execute": execute,
            },
            request_id,
        )

    def sources(self):
        return self.service.query("work_sources", {"id": self.id})

    def read_record(self, kind="index", identity=None, *, offset=0, limit=10000, expected_version=None):
        return self.service.query("work_record_read", {"id": self.id, "kind": kind,
            "identity": identity, "offset": offset, "limit": limit, "expected_version": expected_version})

    def read_source(
        self, source_ref, path, *, offset=0, limit=10000, expected_version=None
    ):
        return self.service.query(
            "work_source_read",
            {
                "id": self.id,
                "source_ref": source_ref,
                "path": path,
                "offset": offset,
                "limit": limit,
                "expected_version": expected_version,
            },
        )

    def compare_sources(
        self, before, after, *, offset=0, limit=100, path=None, expected_version=None
    ):
        return self.service.query(
            "work_source_compare",
            {
                "id": self.id,
                "before": before,
                "after": after,
                "offset": offset,
                "limit": limit,
                "path": path,
                "expected_version": expected_version,
            },
        )

    def observe_target(self, *, execution=None, request_id=None):
        return self.service.command(
            "work_observe_target", {"id": self.id, "execution": execution}, request_id
        )

    def continue_from(self, action, *, revision=None, execution=None, request_id=None):
        return self.service.command(
            "work_continue",
            {
                "id": self.id,
                "action": action,
                "revision": self.snapshot().revision if revision is None else revision,
                "execution": execution,
            },
            request_id,
        )

    def apply_recovery(
        self,
        *,
        job_id=None,
        start=False,
        revision=None,
        execution=None,
        request_id=None,
    ):
        return self.service.command(
            "work_apply_recovery",
            {
                "id": self.id,
                "job_id": job_id,
                "start": start,
                "revision": self.snapshot().revision if revision is None else revision,
                "execution": execution,
            },
            request_id,
        )

    def control_integration(
        self, operation, *, revision=None, execution=None, request_id=None
    ):
        return self.service.command(
            "work_integration_control",
            {
                "id": self.id,
                "operation": operation,
                "revision": self.snapshot().revision if revision is None else revision,
                "execution": execution,
            },
            request_id,
        )

    def progress(self):
        return self.service.query("work_progress", {"id": self.id})

    def revise(self, text, **kw):
        return self._control("work_revise", text=text, **kw)

    def executions(self, *, cursor=0, limit=20):
        return self.service.query(
            "work_executions", {"id": self.id, "cursor": cursor, "limit": limit}
        )

    def wait_execution(self, identity, seconds=10):
        return self.service.query(
            "work_execution_wait",
            {"id": self.id, "execution_id": identity, "seconds": seconds},
        )

    def execution(self, identity):
        return self.service.query(
            "work_execution_get", {"id": self.id, "execution_id": identity}
        )

    def execution_output(self, identity, *, cursor=0, limit=16000, stream="output"):
        return self.service.query(
            "work_execution_output",
            {
                "id": self.id,
                "execution_id": identity,
                "cursor": cursor,
                "limit": limit,
                "stream": stream,
            },
        )

    def stop_execution(
        self, identity, *, revision=None, execution_revision=None, request_id=None
    ):
        revision = self.snapshot().revision if revision is None else revision
        return self.service.command(
            "work_execution_stop",
            {
                "id": self.id,
                "revision": revision,
                "execution_id": identity,
                "execution_revision": execution_revision,
            },
            request_id,
        )

    def send_execution_input(self, identity, text, *, revision=None, request_id=None):
        revision = self.snapshot().revision if revision is None else revision
        return self.service.command(
            "work_execution_input",
            {
                "id": self.id,
                "revision": revision,
                "execution_id": identity,
                "text": text,
            },
            request_id,
        )

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

    def models(self):
        return self.query("service_models")

    def configure_ci_limits(
        self, *, ci_fix_limit=None, ci_fix_warning=2, request_id=None
    ):
        return self.command(
            "delivery_ci_limits",
            {"ci_fix_limit": ci_fix_limit, "ci_fix_warning": ci_fix_warning},
            request_id,
        )

    def configure_base_updates(self, mode="manual", *, request_id=None):
        return self.command("delivery_base_update", {"mode": mode}, request_id)

    def retry_approval_review(self, request, *, request_id=None):
        ref = request.ref if hasattr(request, "ref") else request
        if ref.target.kind != "service" or ref.target.id != self.id:
            raise ValueError("Review retry requires this Service's request reference.")
        return self.command(
            "approval_review_retry",
            {"id": ref.id, "revision": ref.revision},
            request_id,
        )

    def recover_model(self, profile, *, revision, request_id=None):
        return self.command(
            "model_recover", {"profile": profile, "revision": revision}, request_id
        )

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
        values = self.query("requests", {"work_id": work_id} if work_id else {})
        return self._human_requests(values)

    def requests_page(self, *, work_id=None, cursor=0, limit=20, expired=False):
        result = self.query(
            "requests_page",
            {"work_id": work_id, "cursor": cursor, "limit": limit, "expired": expired},
        )
        return {**result, "items": self._human_requests(result["items"])}

    def _human_requests(self, values):
        from homebrew_harness.contracts import HumanRequest, RequestRef

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
            if (
                ref.target.id != self.id
                or not ref.durable
                or set(values) - {"text", "allow_extra_attempt"}
            ):
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

    def discussions(self, *, cursor=0, limit=20, state=None):
        return self.query(
            "communication_page",
            {"kind": "discussions", "cursor": cursor, "limit": limit, "state": state},
        )

    def discussion(self, identity):
        return self.query("communication_get", {"kind": "discussions", "id": identity})

    def discussion_events(self, identity, *, cursor=0, limit=20):
        return self.query(
            "communication_events", {"id": identity, "cursor": cursor, "limit": limit}
        )

    def communication(self, identity):
        return self.query(
            "communication_get", {"kind": "communication_actions", "id": identity}
        )

    def communication_actions(self, *, cursor=0, limit=20, state=None):
        return self.query(
            "communication_page",
            {
                "kind": "communication_actions",
                "cursor": cursor,
                "limit": limit,
                "state": state,
            },
        )

    def communication_record(self, kind, identity, *, offset=0, limit=4000):
        return self.query(
            "communication_read",
            {"kind": kind, "identity": identity, "offset": offset, "limit": limit},
        )

    def control_discussion(
        self, identity, operation="assess", *, revision=None, request_id=None
    ):
        if operation not in {"assess", "pause", "reopen", "prepare_review"}:
            raise ValueError("Unknown discussion operation.")
        revision = (
            self.discussion(identity)["revision"] if revision is None else revision
        )
        return self.command(
            "discussion_" + operation,
            {"id": identity, "revision": revision},
            request_id,
        )

    def plan_communication(self, identity, plan, *, revision=None, request_id=None):
        revision = (
            self.discussion(identity)["revision"] if revision is None else revision
        )
        return self.command(
            "communication_plan",
            {"id": identity, "revision": revision, "plan": plan},
            request_id,
        )

    def request_communication_authority(
        self, identity, *, revision=None, request_id=None
    ):
        revision = (
            self.communication(identity)["revision"] if revision is None else revision
        )
        return self.command(
            "communication_request_authority",
            {"id": identity, "revision": revision},
            request_id,
        )

    def inspect_communication(
        self,
        identity,
        *,
        conclusion="reconcile",
        confirmed=False,
        revision=None,
        execution=None,
        request_id=None,
    ):
        payload = {
            "id": identity,
            "conclusion": conclusion,
            "confirmed": confirmed,
            "execution": execution,
        }
        if conclusion != "reconcile":
            payload["revision"] = (
                self.communication(identity)["revision"]
                if revision is None
                else revision
            )
        return self.command("communication_inspect", payload, request_id)

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
        self,
        identity,
        kind,
        *,
        revision=None,
        body=None,
        subject=None,
        draft=False,
        publication=None,
        request_id=None,
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
                "draft": draft,
                "publication": publication,
            },
            request_id,
        )

    def inspect_delivery(
        self,
        identity,
        *,
        conclusion="reconcile",
        confirmed=False,
        execution=None,
        request_id=None,
    ):
        return self.command(
            "delivery_inspect",
            {
                "id": identity,
                "conclusion": conclusion,
                "confirmed": confirmed,
                "execution": execution,
            },
            request_id,
        )

    def request_delivery(self, identity, *, request_id=None):
        return self.command("delivery_request", {"id": identity}, request_id)

    def refresh_delivery(self, identity, *, execution=None, request_id=None):
        return self.command(
            "delivery_refresh", {"id": identity, "execution": execution}, request_id
        )

    def executions(self, *, cursor=0, limit=20):
        return self.query("service_executions", {"cursor": cursor, "limit": limit})

    def execution(self, identity):
        return self.query("service_execution_get", {"execution_id": identity})

    def wait_execution(self, identity, seconds=10):
        return self.query(
            "service_execution_wait", {"execution_id": identity, "seconds": seconds}
        )

    def execution_output(self, identity, *, cursor=0, limit=16000, stream="output"):
        return self.query(
            "service_execution_output",
            {
                "execution_id": identity,
                "cursor": cursor,
                "limit": limit,
                "stream": stream,
            },
        )

    def stop_execution(self, identity, *, execution_revision=None, request_id=None):
        return self.command(
            "service_execution_stop",
            {"execution_id": identity, "execution_revision": execution_revision},
            request_id,
        )

    def send_execution_input(self, identity, text, *, request_id=None):
        return self.command(
            "service_execution_input",
            {"execution_id": identity, "text": text},
            request_id,
        )

    def batch(self, requests, *, request_id=None):
        return self.command(
            "batch_create",
            {
                "refs": [
                    (
                        r.ref.data()
                        if hasattr(r, "ref")
                        else r.data() if hasattr(r, "data") else r
                    )
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

    def record(self, kind, identity, *, offset=0, limit=4000, expected_version=None):
        return self.query(
            "service_read",
            {
                "kind": kind,
                "identity": identity,
                "offset": offset,
                "limit": limit,
                "expected_version": expected_version,
            },
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

    def control_job(self, identity, operation, *, text=None, request_id=None):
        payload = {"id": identity, "operation": operation}
        if text is not None:
            payload["text"] = text
        return self.command("job_control", payload, request_id)

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
