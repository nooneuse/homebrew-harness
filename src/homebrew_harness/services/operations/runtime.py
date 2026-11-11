"""Durable host operations, with a live control plane while native writers park."""

import json
import threading
import time

from homebrew_harness.execution.execution_control import control_scope, check
from homebrew_harness.execution.model_gateway import Deadline
from homebrew_harness.services.protocol import fingerprint, request_key
from .health import snapshot


class Operations:
    def __init__(self, runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.spec = runtime.spec.operations
        self.stop = threading.Event()
        self.finished = threading.Event()
        self.started = False
        self.error = None
        self.restoring = False

    def health_snapshot(self):
        return snapshot(self.runtime)

    def get(self, identity):
        row = self.store.runtime.get("service_operations", identity)
        if not row or row["service_id"] != self.runtime.id:
            raise ValueError("Unknown service operation.")
        return row

    def put(self, conn, row):
        row = {
            **row,
            "service_id": self.runtime.id,
            "checkpoint": "service:" + self.runtime.id,
        }
        self.store.db._put(conn, "service_operations", row["id"], row)
        return row

    def update(self, row, **changes):
        with self.store.db.write() as conn:
            self.store.guard(conn, self.runtime.header)
            value = self.get(row["id"])
            value.update(changes, revision=value["revision"] + 1)
            return self.put(conn, value)

    def enqueue(self, kind, payload, request_id):
        identity = "op_" + request_key("service-operation", self.runtime.id, request_id)

        def accept(conn):
            meta = self.store.guard(conn, self.runtime.header)
            if meta["state"] in {"stopping", "stopped"}:
                raise ValueError("Service is stopping.")
            old = self.store.db._get(conn, "service_operations", identity)
            if old:
                return old
            if kind == "quiesce":
                if meta.get("operations_phase", "active") not in {"active", "parked"}:
                    raise ValueError("Service is already draining.")
                if meta.get("operations_phase") != "parked":
                    meta.update(
                        state="paused",
                        scope="all",
                        operations_phase="draining",
                        revision=meta["revision"] + 1,
                    )
                self.store.db._put(conn, "service_meta", self.runtime.id, meta)
            elif (
                kind not in {"audit", "diagnose", "verify_backup"}
                and meta.get("operations_phase") != "parked"
            ):
                raise ValueError("Park service writers before this operation.")
            return self.put(
                conn,
                {
                    "id": identity,
                    "kind": kind,
                    "payload": payload,
                    "state": "queued",
                    "revision": 0,
                    "created_at": time.time(),
                    "deadline_at": None,
                    "eligible_at": time.time(),
                    "result": None,
                    "error": None,
                },
            )

        return self.store.command(
            self.runtime.header, "ops_" + kind, payload, request_id, accept
        )

    def describe(self, row, *, listing=False):
        value = dict(row)
        if listing:
            return {
                **{
                    k: v
                    for k, v in value.items()
                    if k
                    in {
                        "id",
                        "kind",
                        "state",
                        "revision",
                        "created_at",
                        "completed_at",
                        "deadline_at",
                        "error",
                    }
                },
                "result_available": row.get("result") is not None,
            }
        if len(json.dumps(value)) > 24000:
            value.pop("result", None)
            value.update(
                result_available=row.get("result") is not None,
                read_full="Use operation_record with offset and limit.",
            )
        return value

    def page(self, cursor=0, limit=20):
        if (
            type(cursor) is not int
            or cursor < 0
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise ValueError("Invalid operations page.")
        with self.store.db.connection() as conn:
            rows = conn.execute(
                "SELECT rowid,value FROM records WHERE section='service_operations' AND owner=? AND rowid>? ORDER BY rowid LIMIT ?",
                ("service:" + self.runtime.id, cursor, limit + 1),
            ).fetchall()
        return {
            "items": [
                self.describe(json.loads(r[1]), listing=True) for r in rows[:limit]
            ],
            "next_cursor": rows[limit - 1][0] if len(rows) > limit else None,
        }

    def claim(self):
        active = self.store.next_work(self.runtime.id)
        with self.store.db.write() as conn:
            meta = self.store.guard(conn, self.runtime.header)
            busy = bool(
                active
                and active["state"] == "in_progress"
                and meta.get("operations_phase", "active") == "active"
            )
            raw = conn.execute(
                "SELECT value FROM records WHERE section='service_operations' AND owner=? "
                "AND json_extract(value,'$.state') IN ('queued','running') "
                "AND coalesce(json_extract(value,'$.eligible_at'),0)<=? "
                "AND NOT (? AND json_extract(value,'$.kind')='audit') "
                "ORDER BY CASE json_extract(value,'$.kind') WHEN 'quiesce' THEN 0 ELSE 1 END,rowid LIMIT 1",
                ("service:" + self.runtime.id, time.time(), busy),
            ).fetchone()
            if raw is None:
                return None
            row = json.loads(raw[0])
            if row["state"] == "queued":
                row["deadline_at"] = time.time() + self.spec["operation_seconds"]
            row.update(
                state="running",
                generation=self.runtime.header["generation"],
                revision=row["revision"] + 1,
            )
            return self.put(conn, row)

    def loop(self):
        self.started = True
        next_health = 0
        try:
            while not self.stop.wait(0.05):
                meta = self.store.get(self.runtime.id)
                if meta["state"] == "stopping":
                    return
                if time.monotonic() >= next_health:
                    self.observe()
                    next_health = time.monotonic() + self.spec["health_seconds"]
                row = self.claim()
                if row is None:
                    continue
                try:
                    with (
                        control_scope(
                            Deadline.after(max(0, row["deadline_at"] - time.time()))
                        ),
                        self.runtime.leases.use("service-operation"),
                    ):
                        check(cancel_events=(self.stop,))
                        result = self.perform(row)
                    self.update(
                        row,
                        state="completed",
                        result=result,
                        error=None,
                        completed_at=time.time(),
                    )
                except Exception as error:
                    self.restoring = False
                    self.update(
                        row,
                        state="blocked",
                        error=type(error).__name__,
                        error_detail=str(error)[:2000],
                    )
        except Exception as error:
            self.error = type(error).__name__
        finally:
            self.finished.set()

    def observe(self):
        health = snapshot(self.runtime)
        from .retention import refresh

        refresh(self)
        diagnoses = []
        with self.store.db.write() as conn:
            self.store.guard(conn, self.runtime.header)
            active = {
                fingerprint([i["code"], i["target"]]): i for i in health["issues"]
            }
            prior = {
                r[0]: json.loads(r[1])
                for r in conn.execute(
                    "SELECT key,value FROM records WHERE section='service_incidents' AND owner=? AND json_extract(value,'$.state')='open'",
                    ("service:" + self.runtime.id,),
                )
            }
            for key, issue in active.items():
                identity = "incident_" + key[:32]
                if identity not in prior:
                    value = {
                        "id": identity,
                        "state": "open",
                        "issue": issue,
                        "created_at": time.time(),
                        "service_id": self.runtime.id,
                        "checkpoint": "service:" + self.runtime.id,
                        "revision": 0,
                    }
                    self.store.db._put(conn, "service_incidents", identity, value)
                    if self.spec["diagnose_incidents"] and self.runtime.maintenance:
                        diagnoses.append(identity)
            for key, value in prior.items():
                if key[9:] not in {k[:32] for k in active}:
                    value.update(
                        state="resolved",
                        resolved_at=time.time(),
                        revision=value["revision"] + 1,
                    )
                    self.store.db._put(conn, "service_incidents", key, value)
        for identity in diagnoses:
            self.enqueue("diagnose", {"id": identity}, "diagnose:" + identity)

    def park(self):
        if self.runtime.maintenance:
            self.runtime.maintenance.close()
        with self.runtime.lock:
            work = self.store.next_work(self.runtime.id)
            if work and work.get("run_id"):
                handle = self.runtime._handle(work)
                native = self.runtime.child_for(work).run_store.get(handle.id)
                if native["status"] not in {"completed", "failed", "cancelled"}:
                    if not work["pause_requested"]:
                        self.store.update_work(
                            self.runtime.header, work["id"], service_paused=True
                        )
                    handle.pause()
                    if not handle.owned:
                        handle.supervise()
            handles = [
                h
                for child in self.runtime.children.values()
                for h in child.runs.owned.values()
            ]
            for handle in handles:
                handle.close()
        for handle in handles:
            while not handle.wait_closed(0.05):
                check(cancel_events=(self.stop,))
        with self.runtime.lock:
            self.runtime.tick()
        if (
            self.runtime.maintenance
            and self.runtime.maintenance.delivery
            and self.runtime.maintenance.delivery.git.processes
        ):
            raise ValueError("Delivery processes still own resources.")
        check(cancel_events=(self.stop,))
        self.store.update(
            self.runtime.header,
            operations_phase="parked",
            parked_at=time.time(),
            error=None,
        )
        return {
            "parked": True,
            "native_runs": len(handles),
            "generation": self.runtime.header["generation"],
        }

    def unpark(self):
        meta = self.store.get(self.runtime.id)
        if meta.get("operations_phase") != "parked":
            raise ValueError("Service is not parked.")
        from homebrew_harness.services.spec import runtime_binding

        upgrade = (
            self.store.runtime.get("service_upgrade_plans", meta.get("upgrade_plan"))
            if meta.get("upgrade_plan")
            else None
        )
        if upgrade and upgrade["state"] in {"armed", "probation"}:
            raise ValueError(
                "Complete or roll back the recorded upgrade before resuming business."
            )
        if meta["binding"]["runtime"] != runtime_binding():
            raise ValueError("Installed service binding changed.")
        if self.runtime.maintenance:
            from homebrew_harness.services.maintenance import Maintenance

            if self.runtime.maintenance.finished.is_set():
                self.runtime.maintenance = Maintenance(self.runtime)
            if self.runtime.maintenance.identity() != meta["binding"]["maintenance"]:
                raise ValueError("Maintenance binding changed.")
            self.runtime.start_business()
        self.store.update(
            self.runtime.header,
            operations_phase="active",
            state="running",
            scope="execution",
            error=None,
            revision=meta["revision"] + 1,
        )
        return {"resumed": True}

    def perform(self, row):
        kind = row["kind"]
        if kind == "quiesce":
            return self.park()
        if kind == "resume_operations":
            return self.unpark()
        if kind in {"backup", "verify_backup", "restore_backup"}:
            from .backup import perform

            return perform(self, row)
        if kind in {"retention_preview", "retention_apply"}:
            from .retention import perform

            return perform(self, row)
        if kind == "audit":
            from .audit import prepare

            result = prepare(self, row)
            if (
                not result.get("skipped")
                and self.store.get(self.runtime.id).get("operations_phase") == "parked"
            ):
                self.reason(row, "audit", result["audit_id"], job_id=result["job_id"])
            return result
        if kind.startswith("upgrade_"):
            from .upgrade import perform

            return perform(self, row)
        if kind == "diagnose":
            return self.reason(row, "incident", row["payload"]["id"])
        raise ValueError("Unsupported operation.")

    def reason(self, row, kind, identity, *, job_id=None):
        if not self.runtime.maintenance:
            raise ValueError("Operations reasoning needs repository maintenance.")
        ms = self.runtime.maintenance.store
        with self.store.db.write() as conn:
            self.store.guard(conn, self.runtime.header)
            target = ms.get(
                {
                    "incident": "incidents",
                    "upgrade_assessment": "upgrade_plans",
                    "audit": "audits",
                }[kind],
                identity,
                conn,
            )
            job = (
                ms.job_get(job_id)
                if job_id
                else ms.job(
                    conn, kind, identity, target["revision"], {}, row["id"] + ":" + kind
                )
            )
            if job["state"] == "completed":
                return {"job_id": job["id"], "assessment": job["result"]}
            if job["attempts"] >= ms.policy["job_attempts"]:
                raise ValueError("Operations reasoning attempts exhausted.")
            job.update(
                state="running",
                revision=target["revision"],
                attempts=job["attempts"] + 1,
                generation=self.runtime.header["generation"],
            )
            ms.put(conn, "jobs", job)
        from homebrew_harness.services.agent import ServiceAgent

        actor = ServiceAgent(self.runtime, ms, self.stop)
        try:
            result = actor.run(job)
            ms.finish(job, result)
            return {"job_id": job["id"], "assessment": result}
        except Exception as error:
            ms.finish(job, error=error)
            raise
        finally:
            actor.close()

    def close(self):
        self.stop.set()
        if not self.started:
            self.finished.set()
        self.finished.wait()
