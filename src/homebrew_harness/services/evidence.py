"""Freeze native execution facts for a particular service work version."""

import json

from .protocol import request_key


def collect(runtime, work):
    """Collect every owned receipt in bounded, independently readable pages."""
    from homebrew_harness.runtime.limits import current_limits
    child = runtime.child_for(work)
    native = child.run_store.get(work["run_id"])
    checkpoint = "lead:" + native["session_id"]
    fields = ("run_id", "recipe", "selection", "test_scope", "status", "exit_code",
              "source_stable", "fingerprint", "role", "task_id", "grant_id", "elapsed_seconds",
              "execution_backend", "isolation", "error", "verification_basis", "executed_at")
    identity = "evidence_" + request_key("evidence", runtime.id, native["id"])
    store = runtime.maintenance.store
    authority_revision = (work.get("authority") or {}).get("requirements_revision", work["requirements_revision"])
    pages, count, passed, failed = [], 0, 0, 0
    with child.store.backend().connection() as source, store.db.write() as conn:
        runtime.store.guard(conn, runtime.header)
        cursor = source.execute(
            "SELECT value FROM records WHERE section='test_receipts' "
            "AND (owner=? OR json_extract(value,'$.scope.run_id')=?) ORDER BY rowid",
            (checkpoint, native["id"]))
        while rows := cursor.fetchmany(current_limits().evidence_page_rows):
            receipts = [{k: v for k, v in json.loads(raw).items() if k in fields} for (raw,) in rows]
            page_id = identity + "_page_" + str(len(pages))
            store.put(conn, "evidence", {"id": page_id, "page_of": identity,
                "work_id": work["id"], "run_id": native["id"], "requirements_revision": authority_revision,
                "test_receipts": receipts, "receipts_complete": True})
            pages.append(page_id)
            count += len(receipts)
            passed += sum(r['status'] == 'passed' for r in receipts)
            failed += sum(r['status'] == 'failed' for r in receipts)
        evidence = {"id": identity, "work_id": work["id"], "run_id": native["id"],
            "requirements_revision": authority_revision, "state": native["status"],
            "finalized_at": native["finalized_at"], "test_receipts": [], "receipt_pages": pages,
            "receipt_count": count, "receipts_complete": True, "result": native.get("last_outcome")}
        store.put(conn, "evidence", evidence)
    return {"id": identity, "run_id": native["id"], "requirements_revision": authority_revision,
            "receipt_count": count, "receipts_complete": True, "passed": passed, "failed": failed}


def receipts(runtime, work, *, recipe=None, basis=None, contracts=None):
    """Read owned pages, optionally retaining only relevant current evidence."""
    from homebrew_harness.runtime.limits import current_limits
    store = runtime.maintenance.store
    allowed = set(work.get("run_history", [])) | {work.get("run_id")}
    values = {}
    def accept(receipt):
        if recipe is not None and receipt.get("recipe") != recipe:
            return
        if basis is None:
            values[receipt["run_id"]] = receipt
            return
        observed = receipt.get("verification_basis") or {}
        if any(observed.get(k) != basis[k] for k in ("version", "inputs", "backend", "role")):
            return
        if contracts is not None and observed.get("contract") not in contracts:
            return
        if receipt.get("status") == "passed":
            if observed.get("selection") != basis["selection"] or observed.get("test_scope") != basis["test_scope"]:
                return
            key = (observed.get("contract"), "passed")
        elif receipt.get("status") in {"failed", "timeout", "cancelled"}:
            key = (observed.get("contract"), "failure")
        else:
            return
        if key not in values or receipt.get("executed_at", 0) > values[key].get("executed_at", 0):
            values[key] = receipt
    with store.db.connection() as conn:
        cursor = conn.execute(
            "SELECT value FROM records WHERE section='service_evidence' AND owner=? "
            "AND json_extract(value,'$.work_id')=? AND json_extract(value,'$.page_of') IS NULL",
            ("service:" + runtime.id, work["id"]))
        while rows := cursor.fetchmany(current_limits().evidence_page_rows):
            for (raw,) in rows:
                record = json.loads(raw)
                if record.get("run_id") not in allowed or not record.get("receipts_complete"):
                    continue
                for receipt in record.get("test_receipts", []):
                    accept(receipt)
                for identity in record.get("receipt_pages", []):
                    row = conn.execute("SELECT value FROM records WHERE section='service_evidence' AND owner=? AND key=?",
                                       ("service:" + runtime.id, identity)).fetchone()
                    page = json.loads(row[0]) if row else None
                    if (not page or page.get("page_of") != record["id"] or page.get("work_id") != work["id"]
                            or page.get("run_id") != record["run_id"] or not page.get("receipts_complete")):
                        raise ValueError("Owned receipt evidence page is missing or changed.")
                    for receipt in page.get("test_receipts", []):
                        accept(receipt)
    return list(values.values())
