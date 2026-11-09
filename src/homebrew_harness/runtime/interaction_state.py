"""Atomic host references and input acceptance on the existing runtime database."""

import re
import time
import uuid

from homebrew_harness.storage.durable_runtime import DurableStore
from homebrew_harness.storage.storage_gate import storage_gate


class InteractionState:
    def __init__(self, workdir):
        self.runtime = DurableStore(workdir)
        self.root = self.runtime.workdir

    def get(self, identity):
        value = self.runtime.get("interaction_tasks", identity)
        if value is None:
            raise ValueError("Interaction task not found.")
        return value

    def active(self):
        identity = self.runtime.get("interaction_hosts", "workspace", {}).get("active")
        return self.get(identity) if identity else None

    def accept_input(self, text, mode, target, identity=None, *, literal=False):
        identity = identity or "usr_" + uuid.uuid4().hex
        if not isinstance(identity, str) or not re.fullmatch(
            r"usr_[0-9a-f]{32}", identity
        ):
            raise ValueError("Input ID must be a stable usr_ identifier.")
        payload = {"text": text, "mode": mode, "target": target}
        if literal:
            payload["literal"] = True
        db = self.runtime.backend()
        with storage_gate(self.root), db.write() as conn:
            prior = db._get(conn, "interaction_inputs", identity)
            if prior:
                if prior["payload"] != payload:
                    raise ValueError(
                        "Input ID was already accepted with another payload."
                    )
                return prior
            active = db._get(conn, "interaction_hosts", "workspace", {}).get("active")
            task = db._get(conn, "interaction_tasks", active) if active else None
            if mode == "queue" or task is None:
                queued = conn.execute(
                    "SELECT count(*) FROM records WHERE section='interaction_tasks' AND json_extract(value,'$.state')='queued'"
                ).fetchone()[0]
                if queued >= 64:
                    raise ValueError("Interaction task queue is full.")
                task = {
                    "id": "interaction_" + uuid.uuid4().hex,
                    "state": "queued",
                    "revision": 0,
                    "intent": {"original": text, "amendments": []},
                    "created_at": time.time(),
                    "input_id": identity,
                    "target": None,
                    "route": None,
                    "config": None,
                    "lifetime": "attached",
                    **({"literal": True} if literal else {}),
                    "previous_task": active
                    or db._get(conn, "interaction_hosts", "workspace", {}).get("last"),
                }
                db._put(conn, "interaction_tasks", task["id"], task)
                if not active:
                    db._put(
                        conn,
                        "interaction_hosts",
                        "workspace",
                        {
                            **db._get(conn, "interaction_hosts", "workspace", {}),
                            "active": task["id"],
                        },
                    )
                delivery = "initial"
            else:
                task = {
                    **task,
                    "revision": task["revision"] + 1,
                    "intent": {
                        **task["intent"],
                        "amendments": [*task["intent"]["amendments"], text],
                    },
                }
                db._put(conn, "interaction_tasks", task["id"], task)
                delivery = "pending"
            record = {
                "id": identity,
                "payload": payload,
                "task_id": task["id"],
                "checkpoint": "interaction:" + task["id"],
                "state": delivery,
                "at": time.time(),
            }
            db._put(conn, "interaction_inputs", identity, record)
            return record

    def update(self, identity, **fields):
        db = self.runtime.backend()
        with storage_gate(self.root), db.write() as conn:
            prior = db._get(conn, "interaction_tasks", identity)
            value = {**prior, **fields}
            db._put(conn, "interaction_tasks", identity, value)
        return value

    def accept(self, expected, route, config, target, *, db=None, conn=None):
        if db is None:
            db = self.runtime.backend()
            with storage_gate(self.root), db.write() as transaction:
                return self.accept(
                    expected, route, config, target, db=db, conn=transaction
                )
        current = db._get(conn, "interaction_tasks", expected["id"])
        if current != expected:
            raise ValueError(
                "Input changed while task configuration was being prepared."
            )
        value = {
            **current,
            "state": "accepted",
            "route": route.data(),
            "config": config,
            "target": target,
            "accepted_at": time.time(),
        }
        db._put(conn, "interaction_tasks", value["id"], value)
        return value

    def delivered(self, record):
        self.runtime.put(
            "interaction_inputs", record["id"], {**record, "state": "delivered"}
        )

    def pending(self, identity):
        return sorted(
            (
                v
                for v in self.runtime.items(
                    "interaction_inputs", owner="interaction:" + identity
                ).values()
                if v["task_id"] == identity and v["state"] == "pending"
            ),
            key=lambda v: v["at"],
        )

    def finish(self, identity, *, state="finished", **fields):
        db = self.runtime.backend()
        with storage_gate(self.root), db.write() as conn:
            current = db._get(conn, "interaction_tasks", identity)
            db._put(
                conn,
                "interaction_tasks",
                identity,
                {**current, **fields, "state": state},
            )
            if (
                db._get(conn, "interaction_hosts", "workspace", {}).get("active")
                == identity
            ):
                queued = [
                    db._get(conn, "interaction_tasks", row[0])
                    for row in conn.execute(
                        "SELECT key FROM records WHERE section='interaction_tasks' AND json_extract(value,'$.state')='queued'"
                    ).fetchall()
                ]
                next_task = (
                    min(queued, key=lambda v: v["created_at"]) if queued else None
                )
                db._put(
                    conn,
                    "interaction_hosts",
                    "workspace",
                    {
                        "active": next_task["id"] if next_task else None,
                        "last": identity,
                    },
                )

    def adopt_run(self, run, config):
        if self.active():
            return
        identity = "interaction_" + run["id"][4:]
        db = self.runtime.backend()
        with storage_gate(self.root), db.write() as conn:
            if db._get(conn, "interaction_hosts", "workspace", {}).get("active"):
                return
            db._put(
                conn,
                "interaction_tasks",
                identity,
                {
                    "id": identity,
                    "state": "accepted",
                    "revision": 0,
                    "intent": {"original": run["spec"]["prompt"], "amendments": []},
                    "created_at": run["created_at"],
                    "input_id": None,
                    "target": {"kind": "run", "id": run["id"]},
                    "route": None,
                    "config": config,
                    "lifetime": "attached",
                },
            )
            db._put(conn, "interaction_hosts", "workspace", {"active": identity})
