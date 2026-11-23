"""Paged original evidence for the owning Work, including prior review jobs."""

import json

from homebrew_harness.runtime.limits import current_limits
from .protocol import fingerprint


def read(runtime, work_id, kind="index", identity=None, offset=0, limit=10000,
         expected_version=None):
    store = runtime.maintenance.store
    work = runtime.store.work(work_id, runtime.id)
    maximum = current_limits().operation_read_chars
    if (type(offset) is not int or offset < 0 or type(limit) is not int
            or not 1 <= limit <= maximum):
        raise ValueError("Work record pages require a nonnegative character offset and limit <= " + str(maximum))
    with store.db.connection() as conn:
        changes = [json.loads(raw) for (raw,) in conn.execute(
            "SELECT json_object('id',key,'commit',json_extract(value,'$.commit'),"
            "'state',json_extract(value,'$.state'),'run_id',json_extract(value,'$.run_id'),"
            "'requirements_revision',json_extract(value,'$.requirements_revision')) "
            "FROM records WHERE section='service_changesets' AND owner=? "
            "AND json_extract(value,'$.work_id')=? ORDER BY rowid",
            ("service:" + runtime.id, work_id),
        )]
        change_ids = {row["id"] for row in changes}
        jobs = [json.loads(raw) for (raw,) in conn.execute(
            "SELECT json_object('id',key,'kind',json_extract(value,'$.kind'),"
            "'state',json_extract(value,'$.state'),'target',json_extract(value,'$.target'),"
            "'completed_at',json_extract(value,'$.completed_at')) FROM records "
            "WHERE section='service_jobs_model' AND owner=? AND (json_extract(value,'$.work_id')=? "
            "OR json_extract(value,'$.target')=? OR json_extract(value,'$.target') IN "
            "(SELECT key FROM records WHERE section='service_changesets' AND owner=? "
            "AND json_extract(value,'$.work_id')=?)) ORDER BY rowid",
            ("service:" + runtime.id, work_id, work_id, "service:" + runtime.id, work_id),
        )]
        job_ids = {row["id"] for row in jobs}
        if kind == "index":
            value = {
                "work_id": work_id, "requirements_revision": work["requirements_revision"],
                "requirements": sorted(set(work.get("requirements_history", []))
                                        | ({work["requirements_id"]} if work.get("requirements_id") else set())),
                "changesets": [{k:row.get(k) for k in ("id", "commit", "state", "run_id", "requirements_revision")}
                               for row in changes],
                "jobs": [{k:row.get(k) for k in ("id", "kind", "state", "target", "completed_at")}
                         for row in jobs],
                "recovery_plan_available": bool(work.get("recovery_plan")),
                "instruction": "Read original review jobs and candidate records with service_read_work_record, following next_offset and expected_version. Historical results describe their recorded source and grant no authority or verification for changed code.",
            }
        elif kind == "work":
            if identity not in {None, work_id}:
                raise ValueError("Record belongs to another Work.")
            value = work
        elif kind == "recovery_plan":
            if identity not in {None, work_id}:
                raise ValueError("Record belongs to another Work.")
            value = work.get("recovery_plan") or {}
        elif kind == "jobs":
            if identity not in job_ids:
                raise ValueError("Record belongs to another Work.")
            row = store.get("jobs", identity, conn)
            value = {k:row.get(k) for k in ("id", "kind", "target", "work_id", "state",
                     "created_at", "completed_at", "result", "phase_outputs", "error", "error_detail", "error_info")}
        elif kind == "changesets":
            if identity not in change_ids:
                raise ValueError("Record belongs to another Work.")
            value = store.get(kind, identity, conn)
        elif kind == "requirements":
            owned = set(work.get("requirements_history", [])) | {work.get("requirements_id")}
            if not identity or identity not in owned:
                raise ValueError("Record belongs to another Work.")
            value = store.get(kind, identity, conn)
        elif kind == "evidence":
            value = store.get(kind, identity, conn)
            if value.get("work_id") != work_id:
                raise ValueError("Record belongs to another Work.")
        else:
            raise ValueError("Choose index, work, recovery_plan, jobs, changesets, requirements or evidence.")
    version = fingerprint(value)
    if expected_version is not None and expected_version != version:
        raise ValueError("Work record version changed; restart paging with the current version.")
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if offset > len(text):
        raise ValueError("Work record offset exceeds its character length.")
    end = min(len(text), offset + limit)
    return {"work_id": work_id, "kind": kind, "identity": identity or work_id,
            "version": version, "offset": offset, "next_offset": end if end < len(text) else None,
            "length": len(text), "text": text[offset:end], "complete": end == len(text)}
