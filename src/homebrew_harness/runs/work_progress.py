from homebrew_harness.runtime.limits import current_limits
"""Model-declared work progress uses the Run's existing durable ownership."""

import json
import time
import uuid

from homebrew_harness.storage.work_snapshot import freeze


def submit(
    control,
    *,
    workdir,
    summary,
    remaining=(),
    complete=False,
    evidence_refs=(),
    included_inputs=()
):
    control.safe_point()
    context = context_for(control)
    if not context:
        raise ValueError("This Run has no Service work handoff.")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > current_limits().progress_summary_chars:
        raise ValueError(
            "Progress summary must be nonempty and at most 10000 characters."
        )
    if (
        type(complete) is not bool
        or not isinstance(remaining, (list, tuple))
        or (current_limits().progress_items is not None and len(remaining) > current_limits().progress_items)
        or any(
                not isinstance(v, str) or not v.strip()
                or current_limits().progress_item_chars is not None and len(v) > current_limits().progress_item_chars for v in remaining
        )
        or sum(len(v) for v in remaining) > current_limits().progress_remaining_chars
    ):
        raise ValueError(
            "Progress requires a boolean completion and bounded remaining work."
        )
    if complete and remaining:
        raise ValueError(
            "A complete development handoff cannot declare remaining development work."
        )
    if (
        not isinstance(evidence_refs, (list, tuple))
        or (current_limits().progress_items is not None and len(evidence_refs) > current_limits().progress_items)
        or any(not isinstance(v, str) or current_limits().progress_ref_chars is not None and len(v) > current_limits().progress_ref_chars for v in evidence_refs)
    ):
        raise ValueError("Progress evidence references are invalid.")
    if (
        not isinstance(included_inputs, (list, tuple))
        or (current_limits().progress_items is not None and len(included_inputs) > current_limits().progress_items)
        or any(
            not isinstance(v, str) or not control.write_allowed(workdir, v)
            for v in included_inputs
        )
    ):
        raise ValueError("Proposed existing inputs exceed the Work write scope.")
    session = control.session
    if complete:
        from homebrew_harness.execution.operations import CURRENT

        own = CURRENT.get()
        if (
            any(
                row["execution_pending"] and (not own or row["id"] != own[1])
                for row in session.operations.snapshots()
            )
            or session._recovery_blocked
            or any(
                r["active"] or r["state"] == "unknown"
                for r in session.recovery_operations()
                if r["call_id"] != _current_call()
            )
        ):
            raise ValueError(
                "Finish or inspect outstanding execution before development handoff."
            )
        if session.inputs.pending():
            raise ValueError("Apply pending user input before development handoff.")
        from homebrew_harness.storage.durable_runtime import active_writer

        writer = active_writer()
        assistant = (
            next(
                (
                    m
                    for m in reversed(writer.messages or [])
                    if m.get("role") == "assistant"
                ),
                {},
            )
            if writer
            else {}
        )
        if len(assistant.get("tool_calls", [])) != 1:
            raise ValueError(
                "Submit a complete handoff by itself, without other tool proposals."
            )
    available = session.runtime_store.items(
        "test_receipts", owner="lead:" + control.state["evidence_session_id"]
    )
    prior_refs = {v["run_id"] for v in context.get("verification", [])}
    if set(evidence_refs) - set(available) - prior_refs:
        raise ValueError("Progress evidence is not readable evidence of this Work.")
    identity = "handoff_" + uuid.uuid4().hex
    source = (
        freeze(control.store.root, identity, control.spec.snapshot_limits)
        if complete
        else None
    )
    value = {
        "id": identity,
        "run_id": control.id,
        "checkpoint": session.checkpoint_key,
        "work_id": context["work_id"],
        "service_id": context["service_id"],
        "requirements_revision": context["requirements_revision"],
        "intent": session.intent.to_dict(),
        "summary": summary,
        "remaining": list(remaining),
        "complete": complete,
        "evidence_refs": list(evidence_refs),
        "source": source,
        "created_at": time.time(),
        "included_inputs": list(included_inputs),
        "write_paths": list(control.spec.write_paths),
        "owner_generation": control.header["generation"],
        "accepted": False,
    }
    with session.runtime_store.backend().write() as connection:
        from homebrew_harness.runs.store import owner_guard

        owner_guard(session.runtime_store.backend(), connection, control.header)
        session.runtime_store.backend()._put(
            connection, "run_work_progress", identity, value
        )
    control.work_progress = value
    return json.dumps({"accepted": True, "handoff_ref": identity, "complete": complete})


def _current_call():
    from homebrew_harness.storage.durable_runtime import current_operation

    key = current_operation()
    return key[1].split(":", 1)[-1] if key else None


def view(control, *, workdir, record_kind=None, identity=None, offset=0, limit=10000, expected_version=None):
    current = getattr(control, "work_progress", None)
    context = context_for(control)
    if record_kind is not None:
        from homebrew_harness.services.remote import RemoteService
        if not all(context.get(k) for k in ("control_root", "service_id", "work_id")):
            raise ValueError("Original Work records require this Run's bound Service context.")
        with RemoteService.from_root(context["control_root"]) as service:
            if service.id != context["service_id"]:
                raise ValueError("Original Work records belong to another Service.")
            work = service.query("work_get", {"id":context["work_id"]})
            if not work.get("run_id") or work.get("run_id") != control.id:
                raise ValueError("This Run is not the current execution of its owning Work.")
            return json.dumps(service.query("work_record_read", {"id":context["work_id"],
                "kind":record_kind, "identity":identity, "offset":offset, "limit":limit,
                "expected_version":expected_version}), ensure_ascii=False)
    return json.dumps(
        {
            "work_id": context.get("work_id"),
            "phase": "developing",
            "progress": (
                {k: current[k] for k in ("id", "summary", "remaining", "complete")}
                if current
                else context.get("progress")
            ),
            "verification": context.get("verification", []),
            "evidence_navigation": {"tool": "get_work_progress", "record_kind": "index"}
                if context.get("control_root") else None,
            "instruction": "Historical evidence grants no execution authority. Use matching evidence or actual tests; complete development before Service delivery.",
        },
        ensure_ascii=False,
    )


def completed(control, calls, messages):
    value = getattr(control, "work_progress", None)
    if value:
        if not value["complete"] or value["intent"] != control.session.intent.to_dict():
            return False
        from homebrew_harness.execution.source_snapshot import content_digest, snapshot

        if (
            content_digest(snapshot(control.store.root, **control.spec.snapshot_limits))
            != value["source"]["content_hash"]
        ):
            return False
        text = value["summary"]
    else:
        if calls:
            return False
        text = next(
            (
                m.get("content", "")
                for m in reversed(messages)
                if m.get("role") == "assistant"
            ),
            "",
        )
    pending = [v for v in control.pending_work() if v != "goal"]
    if pending:
        return False
    session = control.session
    candidate = session.goal.propose_after_turn(text, pending_work=pending)
    decision = candidate[0]
    if decision.action not in {"allow", "achieved"}:
        return False
    control.safe_point()
    token = session.inputs.token()
    if value:
        value["ready_for_accept"] = True
    control.stage_result(text, decision, pending)
    if not session.inputs.commit_end(
        messages, session.todo, session.compactor, token, session.goal, candidate
    ):
        return False
    return True


def context_for(control):
    context = control.spec.work_context
    current = control.store.runtime.get("run_work_context", control.id)
    if current and all(
        current.get(k) == context.get(k) for k in ("service_id", "work_id")
    ):
        return current
    return context
