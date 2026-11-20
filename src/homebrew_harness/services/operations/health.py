"""Deterministic liveness, execution readiness and progress facts; no model calls."""

import json
import shutil
import time


def snapshot(runtime):
    meta = runtime.store.get(runtime.id)
    now = time.time()
    counts = {}
    with runtime.store.db.connection() as conn:
        for section in (
            "service_work",
            "service_decisions",
            "service_actions",
            "service_changesets",
            "service_jobs_model",
            "service_communication_actions",
            "service_discussions",
            "service_review_operations",
        ):
            counts[section] = {
                state: n
                for state, n in conn.execute(
                    "SELECT json_extract(value,'$.state'),count(*) FROM records WHERE section=? AND owner=? GROUP BY json_extract(value,'$.state')",
                    (section, "service:" + runtime.id),
                )
            }
        sources = [
            {**json.loads(r[1]), "id": r[0]}
            for r in conn.execute(
                "SELECT key,value FROM records WHERE section='service_sources' AND owner=?",
                ("service:" + runtime.id,),
            )
        ]
        delivery_blockers = [json.loads(raw) for raw, in conn.execute(
            "SELECT candidate.value FROM records AS candidate JOIN records AS work "
            "ON work.section='service_work' AND work.owner=candidate.owner "
            "AND work.key=json_extract(candidate.value,'$.work_id') "
            "AND json_extract(work.value,'$.changeset_id')=candidate.key "
            "AND json_extract(work.value,'$.state')='review' "
            "WHERE candidate.section='service_changesets' AND candidate.owner=? "
            "AND json_extract(candidate.value,'$.state') IN ('failed','quality_failed','blocked','verification_blocked','repair_waiting','repair_waiting_authority')",
            ("service:" + runtime.id,))]

    free = shutil.disk_usage(runtime.store.root).free
    maintenance = runtime.maintenance
    delivery = maintenance.delivery if maintenance else None
    from homebrew_harness.services.request_views import summary
    decisions = summary(maintenance) if maintenance else {"actionable": 0, "unavailable": 0, "unavailable_examples": [], "oldest_actionable": None}
    pending = decisions["oldest_actionable"]
    issues = []
    for row in delivery_blockers:
        issues.append({"code": "delivery_blocked", "target": row["work_id"],
                       "changeset_id": row["id"], "state": row["state"],
                       "message": row.get("error_detail") or row.get("error") or "Delivery awaits a current repair decision.",
                       "reason_code": (row.get("error_info") or {}).get("code"),
                       "reasoning_job_id": row.get("reasoning_job_id"),
                       "diagnostic_ref": {"record": "changesets", "id": row["id"]}})
    active_faults = {k: v for k, v in meta.get("runtime_failures", {}).items() if not v.get("resolved_at")}
    age = now - (meta["heartbeat_at"] or meta["created_at"])
    if age > runtime.spec.operations["stale_seconds"]:
        issues.append({"code": "heartbeat_stale", "target": runtime.id})
    if free < runtime.spec.operations["min_free_bytes"]:
        issues.append({"code": "storage_space_low", "target": runtime.id})
    phase = meta.get("operations_phase", "active")
    if phase == "active":
        if maintenance and maintenance.agent:
            for model in maintenance.agent.model_control.view():
                if model["blocked"]:
                    issues.append(
                        {"code": "model_connection_blocked", "target": model["profile"]}
                    )
        communication = (
            getattr(maintenance, "communication", None) if maintenance else None
        )
        for name, worker in (
            ("maintenance", maintenance),
            ("delivery", delivery),
            ("communication", communication),
        ):
            if worker and (worker.error or worker.finished.is_set()) and name + ":loop" not in active_faults:
                issues.append({"code": name + "_stopped", "target": runtime.id})
    for row in sources:
        if row.get("error"):
            issues.append({"code": "source_failed", "target": row["id"]})
    with runtime.store.db.connection() as conn:
        sync_count = conn.execute(
            "SELECT count(*) FROM records WHERE section='service_work' AND owner=? AND json_extract(value,'$.workspace_sync.state') IN ('pending','conflict')",
            ("service:" + runtime.id,),
        ).fetchone()[0]
    if sync_count:
        issues.append(
            {"code": "workspace_synchronization_pending", "target": runtime.id}
        )
    if runtime.operations.error and "operations:loop" not in active_faults:
        issues.append({"code": "operations_stopped", "target": runtime.id})
    unknown = counts["service_actions"].get("unknown", 0) + counts[
        "service_actions"
    ].get("started", 0)
    if unknown:
        issues.append({"code": "delivery_inspection_required", "target": runtime.id})
    communication_unknown = counts["service_communication_actions"].get(
        "unknown", 0
    ) + counts["service_communication_actions"].get("started", 0)
    if communication_unknown:
        issues.append(
            {"code": "communication_inspection_required", "target": runtime.id}
        )
    unknown += communication_unknown
    for key, fault in active_faults.items():
        issues.append({"code": "runtime_failed", "target": key,
                       "message": fault.get("error_detail"), "diagnostic_ref": {"query": "snapshot", "field": "runtime_failures", "key": key}})
    pending_fault = getattr(runtime, "unpersisted_failure", None)
    if pending_fault:
        issues.append({"code": "runtime_failure_not_persisted", "target": runtime.id, "message": pending_fault["error_detail"]})
    if meta.get("error"):
        issues.append({"code": meta["error"], "target": runtime.id})
    live = runtime.leases.held("service") and not runtime.finished.is_set()
    ready = live and meta["state"] == "running" and phase == "active" and not issues
    return {
        "service_id": runtime.id,
        "observed_at": now,
        "live": live,
        "ready": ready,
        "state": meta["state"],
        "operations_phase": phase,
        "heartbeat_age_seconds": max(0, age),
        "counts": counts,
        "disk_free_bytes": free,
        "pending_decision_age_seconds": max(0, now - pending) if pending else None,
        "unknown_actions": unknown,
        "workspace_sync_pending": sync_count,
        "sources": [
            {
                **{k: r.get(k) for k in ("id", "next_poll", "last_success", "attempts", "error")},
                "reason": (r.get("error_detail") or "")[:500],
                "diagnostic_ref": {"record": "sources", "id": r["id"]} if r.get("error") else None
            }
            for r in sources
        ],
        "issues": issues,
        "waiting_for_operator": bool(decisions["actionable"]),
        "decisions": decisions,
        "active_work": meta.get("active_work"),
        "active_run": meta.get("active_run"),
    }
