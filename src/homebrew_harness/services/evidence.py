"""Freeze native execution facts for a particular service work version."""

import json

from .protocol import request_key


def collect(runtime, work):
    child = runtime.child_for(work)
    native = child.run_store.get(work["run_id"])
    checkpoint = "lead:" + native["session_id"]
    with child.store.backend().connection() as conn:
        rows = conn.execute(
            "SELECT value FROM records WHERE section='test_receipts' "
            "AND (owner=? OR json_extract(value,'$.scope.run_id')=?) "
            "ORDER BY rowid LIMIT 101",
            (checkpoint, native["id"]),
        ).fetchall()
    fields = (
        "run_id",
        "recipe",
        "selection",
        "test_scope",
        "status",
        "exit_code",
        "source_stable",
        "fingerprint",
        "role",
        "task_id",
        "grant_id",
        "elapsed_seconds",
        "execution_backend",
        "isolation",
        "error",
        "verification_basis",
        "executed_at",
    )
    receipts = [
        {k: v for k, v in json.loads(raw).items() if k in fields}
        for (raw,) in rows[:100]
    ]
    identity = "evidence_" + request_key("evidence", runtime.id, native["id"])
    authority_revision = (work.get("authority") or {}).get(
        "requirements_revision", work["requirements_revision"]
    )
    evidence = {
        "id": identity,
        "work_id": work["id"],
        "run_id": native["id"],
        "requirements_revision": authority_revision,
        "state": native["status"],
        "finalized_at": native["finalized_at"],
        "test_receipts": receipts,
        "receipts_complete": len(rows) <= 100,
        "result": native.get("last_outcome"),
    }
    store = runtime.maintenance.store
    with store.db.write() as conn:
        runtime.store.guard(conn, runtime.header)
        store.put(conn, "evidence", evidence)
    return {
        "id": identity,
        "run_id": native["id"],
        "requirements_revision": authority_revision,
        "receipt_count": len(receipts),
        "receipts_complete": evidence["receipts_complete"],
        "passed": sum(r["status"] == "passed" for r in receipts),
        "failed": sum(r["status"] == "failed" for r in receipts),
    }


def receipts(runtime, work):
    """Only evidence lifted from this Work's owned execution history is readable."""
    store = runtime.maintenance.store
    with store.db.connection() as conn:
        rows = conn.execute(
            "SELECT value FROM records WHERE section='service_evidence' AND owner=?",
            ("service:" + runtime.id,),
        ).fetchall()
    values = {}
    allowed = set(work.get("run_history", [])) | {work.get("run_id")}
    for (raw,) in rows:
        record = json.loads(raw)
        if (
            record.get("work_id") == work["id"]
            and record.get("run_id") in allowed
            and record.get("receipts_complete")
        ):
            for receipt in record.get("test_receipts", []):
                values[receipt["run_id"]] = receipt
    return list(values.values())
