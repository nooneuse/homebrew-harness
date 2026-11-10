"""Fixed read-only repository audits feed findings through existing WorkItems."""

import hashlib
import json
from pathlib import Path

from homebrew_harness.execution.execution_control import check
from homebrew_harness.execution.source_snapshot import snapshot, content_digest
from homebrew_harness.security.safe_files import SafeFile
from homebrew_harness.services.delivery.git import GitBackend
from homebrew_harness.services.protocol import request_key, fingerprint


def prepare(manager, row):
    runtime = manager.runtime
    if runtime.maintenance is None:
        raise ValueError("Repository audits need the maintenance profile.")
    if type(row["payload"].get("force", False)) is not bool:
        raise ValueError("Audit force must be a boolean.")
    if not manager.spec["git_executable"]:
        raise ValueError("Audits require a fixed host Git executable.")
    with runtime.lock:
        active = runtime.store.next_work(runtime.id)
        if (
            active
            and active["state"] == "in_progress"
            and manager.store.get(runtime.id).get("operations_phase", "active")
            == "active"
        ):
            raise ValueError("Wait for repository writers before auditing.")
        runtime.audit_preparing = True
    git = GitBackend(
        runtime.store.root,
        {
            "git_executable": manager.spec["git_executable"],
            "request_seconds": runtime.spec.run_spec("audit").request_seconds,
        },
        {},
    )
    try:
        root = Path(runtime.spec.workspace)
        limits = runtime.spec.run_spec("Audit repository").snapshot_limits
        files = snapshot(root, manager.spec["audit_globs"], **limits)
        head = git.head(root)
        key = fingerprint([head, content_digest(files), manager.spec["audit_globs"]])
        with manager.store.db.connection() as conn:
            old = conn.execute(
                "SELECT value FROM records WHERE section='service_audits' AND owner=? AND json_extract(value,'$.input_hash')=? AND json_extract(value,'$.state') IN ('queued','reviewing','completed') ORDER BY rowid DESC LIMIT 1",
                ("service:" + runtime.id, key),
            ).fetchone()
        if old and not row["payload"].get("force"):
            return {
                "skipped": True,
                "reason": "unchanged_repository",
                "audit_id": json.loads(old[0])["id"],
            }
        identity = "audit_" + request_key("audit", runtime.id, row["id"])
        directory = (
            manager.store.root / ".harness/service_audits" / identity / "workspace"
        )
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        for name, metadata in files.items():
            check()
            with SafeFile(root, name) as source:
                raw = source.snapshot(max_bytes=limits["max_bytes"])[0]
            if raw is None or hashlib.sha256(raw).hexdigest() != metadata["sha256"]:
                raise ValueError("Audit input changed during capture.")
            with SafeFile(directory, name, create_parents=True) as target:
                target.publish(
                    raw,
                    target.snapshot(max_bytes=limits["max_bytes"])[1],
                    0o555 if metadata["executable"] else 0o444,
                )
        frozen = snapshot(directory, manager.spec["audit_globs"], **limits)
        if (
            content_digest(frozen) != content_digest(files)
            or snapshot(root, manager.spec["audit_globs"], **limits) != files
            or git.head(root) != head
        ):
            raise ValueError("Repository changed during audit preparation.")
        audit = {
            "id": identity,
            "state": "reviewing",
            "revision": 0,
            "head": head,
            "input_hash": key,
            "files": frozen,
            "workspace": str(directory),
            "patterns": manager.spec["audit_globs"],
            "limits": limits,
            "result": None,
            "work_ids": [],
            "service_id": runtime.id,
            "checkpoint": "service:" + runtime.id,
        }
        with manager.store.db.write() as conn:
            manager.store.guard(conn, runtime.header)
            manager.store.db._put(conn, "service_audits", identity, audit)
            job = runtime.maintenance.store.job(
                conn, "audit", identity, 0, {}, identity
            )
        return {
            "audit_id": identity,
            "job_id": job["id"],
            "head": head,
            "input_hash": key,
        }
    finally:
        git.close()
        with runtime.lock:
            runtime.audit_preparing = False


def verify(runtime, audit):
    if (
        snapshot(Path(audit["workspace"]), audit["patterns"], **audit["limits"])
        != audit["files"]
    ):
        raise ValueError("Fixed audit candidate changed.")


def finish(store, conn, job, result):
    audit = store.get("audits", job["target"], conn)
    if audit["revision"] != job["revision"]:
        return "stale"
    work_ids = []
    for finding in result["findings"]:
        identity = store.id + ":" + fingerprint([finding["path"], finding["message"]])
        old = store.db._get(conn, "service_audit_findings", identity)
        if old and store.db._get(conn, "service_work", old["work_id"])["state"] not in {
            "delivered",
            "cancelled",
            "rejected",
        }:
            work_ids.append(old["work_id"])
            continue
        prompt = (
            "Repository audit finding for commit "
            + audit["head"]
            + ".\nPath: "
            + finding["path"]
            + "\n"
            + finding["message"]
            + "\nConfirm the finding against the current repository, preserve existing constraints, and propose verification before implementation."
        )
        work = store.store.enqueue(
            conn, store.id, prompt, "audit:" + identity + ":" + audit["input_hash"]
        )
        work.update(
            state="new",
            events=[],
            run_history=[],
            triage=None,
            authority=None,
            audit_id=audit["id"],
        )
        store.db._put(conn, "service_work", work["id"], work)
        store.job(conn, "triage", work["id"], 0, {}, work["id"] + ":0")
        store.db._put(
            conn,
            "service_audit_findings",
            identity,
            {
                "work_id": work["id"],
                "audit_id": audit["id"],
                "checkpoint": "service:" + store.id,
            },
        )
        store.changed(conn, work["id"])
        work_ids.append(work["id"])
    audit.update(
        state="completed",
        revision=audit["revision"] + 1,
        result=result,
        work_ids=work_ids,
    )
    store.put(conn, "audits", audit)
    store.changed(conn, detail={"audit_id": audit["id"], "work_ids": work_ids})
    return "completed"
