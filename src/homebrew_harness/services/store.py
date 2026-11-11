"""Service commands, work acceptance and schedules in the existing SQLite store."""

import json
import time
import uuid
from contextlib import nullcontext

from homebrew_harness.storage.durable_runtime import DurableStore
from .spec import ServiceSpec, SERVICE_ID, WORK_ID, fingerprint, request_key


class ServiceStore:
    def __init__(self, root):
        self.runtime = DurableStore(root)
        self.root = self.runtime.workdir
        self.db = self.runtime.backend()

    def get(self, identity):
        if not isinstance(identity, str) or not SERVICE_ID.fullmatch(identity):
            raise ValueError("Invalid service reference.")
        value = self.runtime.get("service_meta", identity)
        if not value:
            raise ValueError("Unknown service reference.")
        return value

    @staticmethod
    def summary(value):
        return {
            **{
                k: v
                for k, v in value.items()
                if k
                not in {
                    "run_spec",
                    "prompt",
                    "amendments",
                    "events",
                    "triage",
                    "run_history",
                    "baseline",
                }
            },
            "triage_summary": {
                k: (value.get("triage") or {}).get(k)
                for k in ("disposition", "category", "scope")
            },
            "event_count": len(value.get("events", [])),
            "run_count": len(value.get("run_history", [])) + bool(value.get("run_id")),
            "prompt_preview": value["prompt"][:800],
            "input_length": len(value["prompt"]),
            "amendment_count": len(value.get("amendments", [])),
        }

    def input_page(self, service_id, identity, offset=0, limit=4000, amendment=None):
        value = self.work(identity, service_id)
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 4000
        ):
            raise ValueError("Invalid input page bounds.")
        text = value["prompt"]
        if amendment is not None:
            if type(amendment) is not int or not 0 <= amendment < len(
                value.get("amendments", [])
            ):
                raise ValueError("Invalid amendment index.")
            text = value["amendments"][amendment]
        return {
            "id": identity,
            "revision": value["requirements_revision"],
            "text": text[offset : offset + limit],
            "next_offset": offset + limit if offset + limit < len(text) else None,
        }

    def create(self, spec, config, binding, request_id):
        key = request_key("service", str(self.root), request_id)
        payload = fingerprint([spec.data(), config, binding])
        with self.db.write() as conn:
            previous = self.db._get(conn, "service_submissions", key)
            if previous:
                if previous["payload"] != payload:
                    raise ValueError(
                        "Service request_id has another specification or binding."
                    )
                return self.db._get(conn, "service_meta", previous["id"])
            # One persistent service per control directory; independent directories remain independent.
            if conn.execute(
                "SELECT 1 FROM records WHERE section='service_meta' LIMIT 1"
            ).fetchone():
                raise ValueError(
                    "This control directory already has a service; start its existing reference."
                )
            identity = "svc_" + key
            now = time.time()
            value = {
                "id": identity,
                "spec": spec.data(),
                "config": config,
                "binding": binding,
                "state": "stopped",
                "scope": "all",
                "revision": 0,
                "generation": 0,
                "owner": None,
                "pid": None,
                "created_at": now,
                "heartbeat_at": None,
                "resources_closed": True,
                "error": None,
                "checkpoint": "service:" + identity,
            }
            self.db._put(conn, "service_meta", identity, value)
            self.db._put(
                conn, "service_submissions", key, {"id": identity, "payload": payload}
            )
            for job in spec.schedules:
                self.db._put(
                    conn,
                    "service_jobs",
                    identity + ":" + job.name,
                    {
                        **job.__dict__,
                        "service_id": identity,
                        "next_due": now + job.every_seconds,
                        "sequence": 0,
                        "last_trigger": None,
                        "last_completed": None,
                        "last_work": None,
                        "checkpoint": "service:" + identity,
                    },
                )
            return value

    def claim(self, identity, binding):
        with self.db.write() as conn:
            value = self.db._get(conn, "service_meta", identity)
            if value["binding"] != binding:
                plan = (
                    self.db._get(
                        conn, "service_upgrade_plans", value.get("upgrade_plan")
                    )
                    if value.get("upgrade_plan")
                    else None
                )
                if (
                    plan
                    and plan["state"] == "armed"
                    and plan["expires_at"] > time.time()
                    and plan["generation"] == value["generation"]
                    and plan["old_binding"] == value["binding"]
                    and plan["new_binding"] == binding
                    and value.get("operations_phase") == "parked"
                ):
                    plan.update(state="probation", revision=plan["revision"] + 1)
                    self.db._put(conn, "service_upgrade_plans", plan["id"], plan)
                    value.update(binding=binding, state="paused", scope="all")
                else:
                    raise ValueError(
                        "Service runtime or execution binding changed; restore its original configuration."
                    )
            import os

            value = {
                **value,
                "generation": value["generation"] + 1,
                "owner": uuid.uuid4().hex,
                "pid": os.getpid(),
                "resources_closed": False,
                "heartbeat_at": time.time(),
                "error": None,
            }
            # A deliberate stop starts normally; crash recovery preserves pause/stopping intent.
            if value["state"] == "stopped":
                value.update(
                    state="paused"
                    if value.get("operations_phase") in {"parked", "draining"}
                    else "running",
                    scope="all"
                    if value.get("operations_phase") in {"parked", "draining"}
                    else "execution",
                )
            self.db._put(conn, "service_meta", identity, value)
            return value

    def guard(self, conn, header):
        value = self.db._get(conn, "service_meta", header["id"])
        if not value or any(value[k] != header[k] for k in ("owner", "generation")):
            raise ValueError("Service execution owner changed.")
        return value

    def update(self, header, **changes):
        with self.db.write() as conn:
            value = self.guard(conn, header)
            value = {**value, **changes}
            self.db._put(conn, "service_meta", value["id"], value)
            return value

    def command(self, header, method, payload, request_id, action):
        key = request_key("command", header["id"], request_id)
        signature = fingerprint([method, payload])
        with self.db.write() as conn:
            self.guard(conn, header)
            prior = self.db._get(conn, "service_commands", key)
            if prior:
                if prior["payload"] != signature:
                    raise ValueError("Control request_id has another command payload.")
                return prior["result"]
            result = json.loads(json.dumps(action(conn), allow_nan=False))
            self.db._put(
                conn,
                "service_commands",
                key,
                {
                    "service_id": header["id"],
                    "method": method,
                    "payload": signature,
                    "result": result,
                    "checkpoint": "service:" + header["id"],
                },
            )
            return result

    def work(self, identity, service_id=None):
        if not isinstance(identity, str) or not WORK_ID.fullmatch(identity):
            raise ValueError("Invalid work reference.")
        value = self.runtime.get("service_work", identity)
        if not value or service_id is not None and value["service_id"] != service_id:
            raise ValueError("Work item does not belong to this service.")
        return value

    def page(
        self, service_id, *, section="service_work", state=None, cursor=0, limit=20
    ):
        self.get(service_id)
        if (
            type(cursor) is not int
            or cursor < 0
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise ValueError("Invalid service page bounds.")
        if section not in {"service_work", "service_jobs"}:
            raise ValueError("Unsupported service list.")
        if state is not None and state not in {
            "new",
            "needs_info",
            "proposed",
            "deferred",
            "rejected",
            "merged",
            "approved",
            "in_progress",
            "review",
            "failed",
            "cancelled",
        }:
            raise ValueError("Invalid work state filter.")
        if not self.db.exists():
            return {"items": [], "next_cursor": None}
        with self.db.connection() as conn:
            extra = " AND json_extract(value,'$.state')=?" if state else ""
            parameters = [
                section,
                "service:" + service_id,
                cursor,
                *([state] if state else []),
                limit + 1,
            ]
            rows = conn.execute(
                "SELECT rowid,value FROM records WHERE section=? AND owner=? AND rowid>?"
                + extra
                + " ORDER BY rowid LIMIT ?",
                parameters,
            ).fetchall()
        selected = rows[:limit]
        return {
            "items": [
                self.summary(json.loads(raw))
                if section == "service_work"
                else json.loads(raw)
                for _, raw in selected
            ],
            "next_cursor": selected[-1][0] if len(rows) > limit else None,
        }

    def enqueue(
        self, conn, service_id, prompt, request_id, goal=None, priority=0, job=None
    ):
        if type(priority) is not int or not 0 <= priority <= 100:
            raise ValueError("Priority must be an integer between 0 and 100.")
        meta = self.db._get(conn, "service_meta", service_id)
        spec = ServiceSpec(**meta["spec"]).run_spec(prompt, goal)
        identity = "work_" + request_key("work", service_id, request_id)
        value = {
            "id": identity,
            "service_id": service_id,
            "prompt": prompt,
            "goal": goal,
            "priority": priority,
            "state": "approved",
            "revision": 0,
            "requirements_revision": 0,
            "run_spec": json.loads(json.dumps(spec.data(), allow_nan=False)),
            "run_id": None,
            "launch_id": "service_" + request_key("launch", service_id, identity),
            "created_at": time.time(),
            "error": None,
            "job": job,
            "pause_requested": False,
            "cancel_requested": False,
            "checkpoint": "service:" + service_id,
        }
        old = self.db._get(conn, "service_work", identity)
        if old:
            if any(old[k] != value[k] for k in ("prompt", "goal", "priority")):
                raise ValueError("Work request_id already has another payload.")
            return old
        count = conn.execute(
            "SELECT count(*) FROM records WHERE section='service_work' AND owner=? AND json_extract(value,'$.state') IN ('new','needs_info','proposed','approved','in_progress')",
            ("service:" + service_id,),
        ).fetchone()[0]
        if count >= ServiceSpec(**meta["spec"]).queue_limit:
            raise ValueError("Service work queue is full.")
        self.db._put(conn, "service_work", identity, value)
        return value

    def next_work(self, service_id):
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT value FROM records WHERE section='service_work' AND owner=? AND json_extract(value,'$.state') IN ('approved','in_progress') AND (json_extract(value,'$.state')='in_progress' OR coalesce(json_extract(value,'$.pause_requested'),0)=0) ORDER BY CASE json_extract(value,'$.state') WHEN 'in_progress' THEN 0 ELSE 1 END, json_extract(value,'$.priority') DESC, rowid LIMIT 1",
                ("service:" + service_id,),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def update_work(self, header, identity, *, clear_result=False, **changes):
        with self.db.write() as conn:
            self.guard(conn, header)
            work = self.db._get(conn, "service_work", identity)
            if not work or work["service_id"] != header["id"]:
                raise ValueError("Work item belongs to another service.")
            value = {**work, **changes}
            if clear_result:
                value.pop("result", None)
            if value != work:
                value["revision"] += 1
                self.db._put(conn, "service_work", identity, value)
            return value

    def poll_schedules(self, header, now=None, *, connection=None):
        now = time.time() if now is None else now
        with self.db.write() if connection is None else nullcontext(connection) as conn:
            meta = self.guard(conn, header)
            if meta["state"] != "running":
                return 0
            rows = conn.execute(
                "SELECT key,value FROM records WHERE section='service_jobs' AND owner=?",
                ("service:" + header["id"],),
            ).fetchall()
            fired = 0
            for key, raw in rows:
                job = json.loads(raw)
                if now < job["next_due"]:
                    continue
                if job["last_work"]:
                    old = self.db._get(conn, "service_work", job["last_work"])
                    if old and old["state"] in {
                        "new",
                        "needs_info",
                        "proposed",
                        "approved",
                        "in_progress",
                    }:
                        continue
                # Accept one coalesced trigger, then move to the next future interval.
                missed = int((now - job["next_due"]) // job["every_seconds"]) + 1
                job.update(
                    sequence=job["sequence"] + 1,
                    last_trigger=now,
                    next_due=job["next_due"] + missed * job["every_seconds"],
                )
                if job["action"] == "review_repository":
                    old = (
                        self.db._get(
                            conn, "service_operations", job.get("last_operation")
                        )
                        if job.get("last_operation")
                        else None
                    )
                    if old and old["state"] in {"queued", "running"}:
                        self.db._put(conn, "service_jobs", key, job)
                        continue
                    op_id = "op_" + request_key(
                        "scheduled-audit",
                        header["id"],
                        key + ":" + str(job["sequence"]),
                    )
                    self.db._put(
                        conn,
                        "service_operations",
                        op_id,
                        {
                            "id": op_id,
                            "kind": "audit",
                            "payload": {"force": False},
                            "state": "queued",
                            "revision": 0,
                            "created_at": now,
                            "deadline_at": None,
                            "eligible_at": now,
                            "result": None,
                            "error": None,
                            "schedule": key,
                            "service_id": header["id"],
                            "checkpoint": "service:" + header["id"],
                        },
                    )
                    job["last_operation"] = op_id
                elif job["action"] == "submit_work":
                    work = self.enqueue(
                        conn,
                        header["id"],
                        job["prompt"],
                        key + ":" + str(job["sequence"]),
                        job=key,
                    )
                    if meta["spec"]["profile"] == "repository_maintenance":
                        from .maintenance_store import MaintenanceStore

                        maintenance = MaintenanceStore(
                            self, header, meta["spec"]["maintenance"]
                        )
                        work.update(
                            state="new",
                            events=[],
                            run_history=[],
                            triage=None,
                            authority=None,
                        )
                        self.db._put(conn, "service_work", work["id"], work)
                        maintenance.job(
                            conn, "triage", work["id"], 0, {}, work["id"] + ":0"
                        )
                        maintenance.changed(conn, work["id"])
                    job["last_work"] = work["id"]
                else:
                    job["last_completed"] = now
                self.db._put(conn, "service_jobs", key, job)
                fired += 1
            return fired
