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
        pending = conn.execute(
            "SELECT min(json_extract(value,'$.created_at')) FROM records WHERE section='service_decisions' AND owner=? AND json_extract(value,'$.state')='pending'",
            ("service:" + runtime.id,),
        ).fetchone()[0]
    free = shutil.disk_usage(runtime.store.root).free
    maintenance = runtime.maintenance
    delivery = maintenance.delivery if maintenance else None
    issues = []
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
            if worker and (worker.error or worker.finished.is_set()):
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
    if runtime.operations.error:
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
                k: r.get(k)
                for k in ("id", "next_poll", "last_success", "attempts", "error")
            }
            for r in sources
        ],
        "issues": issues,
        "waiting_for_operator": bool(counts["service_decisions"].get("pending", 0)),
        "active_work": meta.get("active_work"),
        "active_run": meta.get("active_run"),
    }
