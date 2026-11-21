"""Work owns accepted progress; Run identity remains execution provenance."""

import copy
import time

from homebrew_harness.services.protocol import request_key
from homebrew_harness.storage.work_snapshot import freeze, verify


def current(runtime, work):
    if not runtime.maintenance:
        return None
    reference = work.get("progress_ref")
    record = (
        runtime.maintenance.store.get("work_progress", reference) if reference else None
    )
    if (
        record
        and record["work_id"] == work["id"]
        and record["requirements_revision"] == work["requirements_revision"]
        and record.get("write_paths") == list(work["run_spec"]["write_paths"])
    ):
        return record
    return None


def view(runtime, work):
    record = current(runtime, work)
    candidate = (
        runtime.maintenance.store.get("changesets", work["changeset_id"])
        if runtime.maintenance
        and work.get("changeset_id")
        and runtime.maintenance.delivery
        else None
    )
    valid_candidate = (
        candidate
        and candidate["state"] != "superseded"
        and candidate["requirements_revision"] == work["requirements_revision"]
        and runtime.maintenance.delivery.store.policy_matches(candidate)
    )
    phase = (
        "delivery"
        if valid_candidate
        else "handoff_ready" if record and record["complete"] else "developing"
    )
    return {
        "phase": phase,
        "handoff_ref": record["id"] if record else None,
        "summary": record["summary"] if record else "",
        "remaining": record["remaining"] if record else [],
        "source_digest": (
            record["source"]["content_hash"]
            if record and record.get("source")
            else None
        ),
        "verification": record.get("evidence_refs", []) if record else [],
        "changeset_id": work.get("changeset_id"),
        "delivery": ({k: candidate.get(k) for k in
                      ("state", "error", "error_detail", "error_info", "reasoning_job_id", "test_failure", "ci_diagnosis",
                       "denied_action_id", "denied_request_id", "publication_feedback")}
                     if valid_candidate else None),
    }


def accept(runtime, work, progress):
    if (
        progress["requirements_revision"] != work["requirements_revision"]
        or progress["work_id"] != work["id"]
        or progress["service_id"] != runtime.id
        or progress.get("write_paths") != list(work["run_spec"]["write_paths"])
        or work.get("review_required")
    ):
        return None
    if progress["run_id"] not in set(work.get("run_history", [])) | {
        work.get("run_id")
    }:
        raise ValueError("Progress did not originate in this Work.")
    store = runtime.maintenance.store
    prior = store.db.get("service_work_progress", progress["id"])
    if prior:
        return prior
    if progress["complete"]:
        source = progress["source"]
        child = runtime.child_for(work)
        expected = child.workdir / ".harness/work-progress" / progress["id"] / "source"
        if source["workspace"] != str(expected):
            raise ValueError("Progress source is outside its owned snapshot.")
        verify(source)
    with store.db.write() as conn:
        runtime.store.guard(conn, runtime.header)
        latest = store.db._get(conn, "service_work", work["id"])
        if (
            latest["requirements_revision"] != progress["requirements_revision"]
            or latest.get("review_required")
            or list(latest["run_spec"]["write_paths"]) != progress.get("write_paths")
        ):
            return None
        prior = store.db._get(conn, "service_work_progress", progress["id"])
        if prior:
            return prior
        record = {**copy.deepcopy(progress), "accepted_at": time.time()}
        if not latest.get("original_baseline_ref") and latest.get("baseline"):
            record["original_baseline"] = copy.deepcopy(latest["baseline"])
            latest["original_baseline_ref"] = record["id"]
        store.put(conn, "work_progress", record)
        existing = store.db._get(
            conn, "service_work_progress", latest.get("progress_ref", "")
        )
        if not existing or existing["created_at"] <= record["created_at"]:
            latest.update(progress_ref=record["id"], revision=latest["revision"] + 1)
            store.db._put(conn, "service_work", latest["id"], latest)
        store.changed(conn, latest["id"])
    return record


def import_run(runtime, work, native, *, completed_only=False):
    child = runtime.child_for(work)
    values = child.store.items(
        "run_work_progress", owner="lead:" + native["session_id"]
    )
    for progress in sorted(values.values(), key=lambda value: value["created_at"]):
        if progress["run_id"] != native["id"]:
            continue
        if progress["complete"] and not progress.get("accepted"):
            continue
        if not completed_only or progress["complete"]:
            accept(runtime, work, progress)
    work = runtime.store.work(work["id"], runtime.id)
    prior = current(runtime, work)
    authoritative = (
        native["id"] == work.get("run_id")
        and not work.get("review_required")
        and (work.get("authority") or {}).get("requirements_revision")
        == work["requirements_revision"]
        and native["spec"]["write_paths"] == work["run_spec"]["write_paths"]
    )
    if (
        native["status"] == "completed"
        and native.get("cleanup_complete")
        and native.get("finalized_at")
        and authoritative
        and not (prior and prior["complete"])
    ):
        # The existing normal completion is itself an explicit model completion,
        # never inferred from TODOs, test counts or attention prose.
        identity = "handoff_" + request_key(
            "completed-work", runtime.id, work["id"] + ":" + native["id"]
        )
        progress = {
            "id": identity,
            "service_id": runtime.id,
            "work_id": work["id"],
            "run_id": native["id"],
            "requirements_revision": work["requirements_revision"],
            "summary": (
                (native.get("last_outcome") or {}).get("text")
                or "Development Run completed."
            )[:10000],
            "remaining": [],
            "complete": True,
            "created_at": native.get("finalized_at") or time.time(),
            "write_paths": list(native["spec"]["write_paths"]),
            "included_inputs": [],
            "evidence_refs": [],
            "source": freeze(
                child.workdir, identity, native["spec"]["snapshot_limits"]
            ),
        }
        accept(runtime, work, progress)
    if native["status"] in {"completed", "failed", "cancelled"} and native.get(
        "cleanup_complete"
    ):
        from .evidence import collect

        if not runtime.maintenance.store.db.get(
            "service_evidence",
            "evidence_" + request_key("evidence", runtime.id, native["id"]),
        ):
            collect(runtime, work)
    return current(runtime, runtime.store.work(work["id"], runtime.id))


def route(runtime, work):
    """Return True when existing accepted work owns the next stage."""
    if not runtime.maintenance:
        return False
    record = current(runtime, work)
    if not record or not record["complete"] or not runtime.maintenance.delivery:
        return False
    if (
        work.get("pause_requested")
        or work.get("cancel_requested")
        or (work.get("authority") or {}).get("revoked")
    ):
        return True
    candidate = (
        runtime.maintenance.store.get("changesets", work["changeset_id"])
        if work.get("changeset_id")
        else None
    )
    if (
        candidate
        and candidate.get("candidate")
        and candidate["requirements_revision"] == work["requirements_revision"]
        and runtime.maintenance.delivery.store.policy_matches(candidate)
        and (
            candidate.get("handoff_ref") == record["id"]
            or not candidate.get("handoff_ref")
            and candidate["run_id"] == record["run_id"]
        )
    ):
        return True
    child = runtime.child_for(work)
    native = child.run_store.get(record["run_id"])
    if not native.get("cleanup_complete") or not native.get("finalized_at"):
        if native["status"] in {"completed", "failed", "cancelled"}:
            child.runs.get(native["id"], control=True).supervise()
        return True
    with runtime.store.db.write() as conn:
        runtime.store.guard(conn, runtime.header)
        latest = runtime.store.db._get(conn, "service_work", work["id"])
        if (
            latest.get("progress_ref") != record["id"]
            or latest["requirements_revision"] != record["requirements_revision"]
        ):
            return True
        latest.update(state="review", error=None, revision=latest["revision"] + 1)
        runtime.store.db._put(conn, "service_work", latest["id"], latest)
        runtime.maintenance.delivery.store.create(conn, latest)
    return True


def seed(runtime_store, work):
    progress = runtime_store.get("service_work_progress", work.get("progress_ref", ""))
    return (
        {k: progress[k] for k in ("summary", "remaining", "complete")}
        if progress and progress["requirements_revision"] == work["requirements_revision"]
        and progress.get("write_paths") == list(work["run_spec"]["write_paths"])
        else None
    )


def install_context(runtime, work):
    if not work.get("run_id"):
        return
    from .evidence import receipts

    values = receipts(runtime, work)
    context = {
        "service_id": runtime.id,
        "work_id": work["id"],
        "requirements_revision": work["requirements_revision"],
        "progress": seed(runtime.store.runtime, work),
        "verification": [
            {
                k: v[k]
                for k in (
                    "run_id",
                    "recipe",
                    "status",
                    "test_scope",
                    "verification_basis",
                    "executed_at",
                )
                if k in v
            }
            for v in values[-8:]
        ],
    }
    target = runtime.child_for(work).store
    if target.get("run_work_context", work["run_id"]) != context:
        target.put("run_work_context", work["run_id"], context)


def preserve_completed_before_upgrade(store, identity, work_id):
    """Quiescent host adoption of an explicit completed Run before code overlay."""
    from types import SimpleNamespace
    from pathlib import Path
    from homebrew_harness.api import Harness
    from homebrew_harness.services.runtime import saved_config
    from homebrew_harness.services.maintenance_store import MaintenanceStore

    meta = store.get(identity)
    if meta["state"] != "stopped" or not meta["resources_closed"] or meta.get("owner"):
        raise ValueError("Stop and settle Service before preserving completed work.")
    work = store.work(work_id, identity)
    header = {key: meta[key] for key in ("id", "owner", "generation")}
    maintenance = SimpleNamespace(
        store=MaintenanceStore(store, header, meta["spec"]["maintenance"])
    )
    with Harness(
        workdir=Path(work.get("run_workspace") or meta["spec"]["workspace"]),
        config=saved_config(meta["config"]),
    ) as child:
        native = child.run_store.get(work["run_id"])
        if (
            native["status"] != "completed"
            or not native["cleanup_complete"]
            or not native.get("finalized_at")
        ):
            raise ValueError(
                "Only explicitly completed and finalized work can be adopted."
            )
        runtime = SimpleNamespace(
            id=identity,
            store=store,
            header=header,
            maintenance=maintenance,
            child_for=lambda w: child,
        )
        return import_run(runtime, work, native, completed_only=True)
