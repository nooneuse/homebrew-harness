"""Run control records in the existing SQLite database."""

import copy
import json
import math
import os
import re
import time
import uuid
import sqlite3

from homebrew_harness.execution.execution_control import cleanup_scope
from homebrew_harness.security.safe_files import canonical_root
from homebrew_harness.storage.durable_runtime import DurableStore
from homebrew_harness.storage.storage_gate import storage_gate
from .spec import RunSpec, RunOutcome, TERMINAL

RUN_ID = re.compile(r"run_[0-9a-f]{32}\Z")


def run_id(value):
    if not isinstance(value, str) or not RUN_ID.fullmatch(value):
        raise ValueError("Invalid managed run ID.")
    return value


def owner_guard(db, conn, header):
    if not header:
        return None
    current = db._get(conn, "runs", header["run_id"])
    if (
        not current
        or current.get("owner") != header.get("owner")
        or current.get("generation") != header.get("generation")
    ):
        raise ValueError("Managed execution no longer owns this run.")
    return current


def close_controls(db, conn, run):
    if run["status"] not in TERMINAL:
        return
    owner = "lead:" + run["session_id"]
    for row in conn.execute(
        "SELECT key,value FROM records WHERE section='run_requests' AND owner=?",
        (owner,),
    ).fetchall():
        request = json.loads(row[1])
        if request["state"] in {"pending", "approved"}:
            db._put(
                conn,
                "run_requests",
                row[0],
                {**request, "state": "cancelled", "revision": request["revision"] + 1},
            )
    for row in conn.execute(
        "SELECT key,value FROM records WHERE section='run_outbox' AND owner=?", (owner,)
    ).fetchall():
        effect = json.loads(row[1])
        if effect["kind"] == "wake" and effect["state"] == "queued":
            db._put(
                conn,
                "run_outbox",
                row[0],
                {**effect, "state": "cancelled", "revision": effect["revision"] + 1},
            )


def commit_run(db, conn, update):
    current = owner_guard(db, conn, update["header"])
    value = update["value"]
    if current["revision"] != update["expected"]:
        base = update.get("base", {})
        changed = {
            key
            for key in current.keys() | base.keys()
            if current.get(key) != base.get(key)
        }
        if changed - {"revision", "test_attempts", "test_grants"}:
            raise ValueError("Managed run changed before checkpoint commit.")
        value = {
            **value,
            "revision": current["revision"] + 1,
            "test_attempts": current.get("test_attempts", {}),
            "test_grants": current.get("test_grants", {}),
        }
    if value["status"] == "completed":
        from homebrew_harness.storage.control_boundary import ProposalSuperseded

        if value["spec"].get("stages"):
            from .stages import completion_guard

            completion_guard(db, conn, value, update.get("stages"))
        rows = conn.execute(
            "SELECT value FROM records WHERE section='run_requests' AND owner=?",
            ("lead:" + value["session_id"],),
        ).fetchall()
        if (
            any(json.loads(row[0])["state"] in {"pending", "approved"} for row in rows)
            or conn.execute(
                "SELECT count(*) FROM inbox WHERE recipient=?",
                ("reference_" + value["id"],),
            ).fetchone()[0]
        ):
            raise ProposalSuperseded(
                "Managed completion still has a request or source input to consume."
            )
    db._put(conn, "runs", value["id"], value)
    close_controls(db, conn, value)
    if update.get("input_state"):
        state = update["input_state"]
        prior = db._get(conn, "run_input_applied", value["id"])
        db._put(conn, "run_input_applied", value["id"], state)
        if prior != state:
            from homebrew_harness.execution.test_runner import digest

            for raw in conn.execute(
                "SELECT value FROM records WHERE section='run_member_outcomes'"
            ).fetchall():
                outcome = json.loads(raw[0])
                if outcome["run_id"] == value["id"] and outcome["kind"] in {
                    "waiting_input",
                    "waiting_event",
                }:
                    db._send(
                        conn,
                        outcome["member"],
                        {
                            "id": outcome["id"] + ":input:" + digest(state)[:16],
                            "from": "lead",
                            "to": outcome["member"],
                            "type": "input_applied",
                            "content": "Parent input checkpoint committed.",
                            "ts": time.time(),
                            "metadata": {
                                "origin": "managed_input",
                                "run_id": value["id"],
                                **state,
                            },
                        },
                    )
    if (
        value["status"] in {"blocked", "completed"}
        and current["status"] != value["status"]
    ):
        from .effects import notice

        notice(
            db,
            conn,
            value,
            (value.get("last_outcome") or {}).get("reason_code", value["status"]),
        )
    if update.get("episode"):
        db._put(conn, "run_episodes", update["episode"]["id"], update["episode"])


class RunStore:
    def __init__(self, workdir):
        self.root = canonical_root(workdir)
        self.runtime = DurableStore(self.root)
        self._anchor = None
        self._anchor_identity = None
        self._permanently_closed = False
        if self.runtime.backend().exists():
            self._keep_database()

    def _keep_database(self):
        if self._permanently_closed:
            return
        path = self.runtime.backend().path
        info = os.stat(path, follow_symlinks=False)
        identity = (info.st_dev, info.st_ino)
        if self._anchor is not None and self._anchor_identity == identity:
            return
        self.close()
        # A service-lifetime read connection keeps WAL sidecars stable while
        # short, descriptor-checked operational connections open in other actors.
        with self.runtime.backend().connection() as checked:
            connection = sqlite3.connect(
                path.as_uri() + "?mode=rw",
                uri=True,
                isolation_level=None,
                check_same_thread=False,
                timeout=2,
            )
            try:
                connection.execute("PRAGMA journal_mode").fetchone()
                checked.binding()
            except BaseException:
                connection.close()
                raise
            self._anchor = connection
            self._anchor_identity = identity

    def close(self, *, permanent=False):
        if permanent:
            self._permanently_closed = True
        if self._anchor is not None:
            self._anchor.close()
            self._anchor = None
            self._anchor_identity = None

    def __del__(self):
        if getattr(self, "_anchor", None) is not None:
            self.close()

    def get(self, identity):
        run_id(identity)
        if not self.runtime.backend().exists():
            raise ValueError("Managed run not found.")
        self._keep_database()
        value = self.runtime.get("runs", run_id(identity))
        if not value or value.get("format") != 1:
            raise ValueError("Managed run not found or invalid.")
        return value

    def create(self, spec, *, now=None, _accept=None):
        if not isinstance(spec, RunSpec):
            raise ValueError("RunSpec is required.")
        now = time.time() if now is None else now
        if type(now) not in {int, float} or not math.isfinite(now):
            raise ValueError("Invalid run clock.")
        from .maintenance import require_space

        require_space(self.root, spec.min_free_bytes)
        if self.runtime.backend().exists():
            for existing in self.runtime.items("runs").values():
                if (
                    existing["status"] in TERMINAL
                    and existing.get("finalized_at") is None
                ):
                    self.finalize_terminal(existing["id"])
        identity = "run_" + uuid.uuid4().hex
        from .services import EMPTY_CONTROL_ID

        info = os.stat(self.root)
        session_id = uuid.uuid4().hex
        state = {
            "format": 1,
            "id": identity,
            "spec": spec.data(),
            "created_at": now,
            "deadline_at": now + spec.total_seconds,
            "last_clock": now,
            "status": "queued",
            "revision": 0,
            "generation": 0,
            "owner": None,
            "episode": 0,
            "session_id": session_id,
            "session_refs": [session_id],
            "evidence_session_id": session_id,
            "submitted": False,
            "finalized_at": None,
            "resume_request": None,
            "bindings": None,
            "selected_model": spec.model_profile,
            "used_fallbacks": [],
            "control_identity": EMPTY_CONTROL_ID,
            "notify_enabled": False,
            "source_enabled": False,
            "checkpoint": None,
            "boundary_phase": "idle",
            "workspace_identity": [info.st_dev, info.st_ino],
            "recovery_count": 0,
            "next_retry_at": None,
            "last_outcome": None,
            "cleanup_complete": True,
            "test_runs": 0,
            "test_seconds": 0,
            "test_grants": {},
            "git_head": None,
        }
        db = self.runtime.backend()
        with storage_gate(self.root), db.write() as conn:
            rows = conn.execute(
                "SELECT value FROM records WHERE section='runs'"
            ).fetchall()
            if any(
                json.loads(row[0])["status"] not in TERMINAL
                or json.loads(row[0]).get("finalized_at") is None
                for row in rows
            ):
                raise ValueError(
                    "Finish or cancel the existing managed run in this workspace first."
                )
            if _accept is not None:
                _accept(db, conn, state)
            from .stages import create_tasks

            create_tasks(db, conn, state, spec.stages)
            db._put(conn, "runs", identity, state)
            db._put(conn, "run_controls", identity, {"revision": 0, "action": "run"})
        return state

    def claim(self, identity, *, now=None, manage=False):
        now = time.time() if now is None else now
        db = self.runtime.backend()
        with storage_gate(self.root), db.write() as conn:
            value = db._get(conn, "runs", run_id(identity))
            if (
                not value
                or value["status"] in TERMINAL
                and not (manage and not value.get("cleanup_complete"))
            ):
                raise ValueError("A terminal run cannot be restarted.")
            if value["status"] in {"paused", "blocked"} and not manage:
                raise ValueError(
                    "Explicitly resume the run after resolving its blocker."
                )
            if value.get("next_retry_at") and now < value["next_retry_at"]:
                raise ValueError("Managed retry is not due yet.")
            if now < value["last_clock"] - 1:
                raise ValueError(
                    "Run clock moved backwards; host inspection is required."
                )
            value = {
                **value,
                "owner": uuid.uuid4().hex,
                "generation": value["generation"] + 1,
                "revision": value["revision"] + 1,
                "status": value["status"] if manage else "running",
                "cleanup_complete": False,
                "last_clock": now,
            }
            db._put(conn, "runs", identity, value)
            db._put(
                conn,
                "run_attempts",
                value["owner"],
                {
                    "id": value["owner"],
                    "run_id": identity,
                    "generation": value["generation"],
                    "started_at": now,
                    "state": "starting",
                    "pid": None,
                },
            )
        return value

    def pid(self, header, pid):
        db = self.runtime.backend()
        with storage_gate(self.root), db.write() as conn:
            owner_guard(db, conn, header)
            old = db._get(conn, "run_attempts", header["owner"])
            db._put(
                conn,
                "run_attempts",
                header["owner"],
                {**old, "pid": pid, "state": "running"},
            )

    def control(self, identity, action):
        if action not in {"run", "pause", "cancel"}:
            raise ValueError("Invalid managed control.")
        db = self.runtime.backend()
        with cleanup_scope(), storage_gate(self.root), db.write() as conn:
            value = db._get(conn, "runs", run_id(identity))
            if not value or value["status"] in TERMINAL:
                raise ValueError("Terminal run controls are closed.")
            if (
                action == "run"
                and value["status"] in {"paused", "blocked"}
                and not value.get("cleanup_complete")
            ):
                raise ValueError(
                    "Finish execution cleanup or inspect its saved operations before resuming."
                )
            old = db._get(conn, "run_controls", identity, {"revision": 0})
            db._put(
                conn,
                "run_controls",
                identity,
                {"revision": old["revision"] + 1, "action": action},
            )
            recipient = "user_" + identity
            db._put(
                conn,
                "input_epochs",
                recipient,
                db._get(conn, "input_epochs", recipient, 0) + 1,
            )
            if action == "cancel" or value["owner"] is None and action == "pause":
                db._put(
                    conn,
                    "runs",
                    identity,
                    {
                        **value,
                        "status": "paused" if action == "pause" else "cancelled",
                        "last_outcome": RunOutcome(
                            identity,
                            value["episode"],
                            "paused" if action == "pause" else "cancelled",
                            action,
                            value.get("checkpoint") or "lead:" + value["session_id"],
                            cleanup_complete=value["cleanup_complete"],
                        ).data(),
                        "revision": value["revision"] + 1,
                    },
                )
                if action == "cancel":
                    for row in conn.execute(
                        "SELECT key,value FROM records WHERE section='run_requests' AND owner=?",
                        ("lead:" + value["session_id"],),
                    ).fetchall():
                        request = json.loads(row[1])
                        if request["state"] in {"pending", "approved"}:
                            db._put(
                                conn,
                                "run_requests",
                                row[0],
                                {
                                    **request,
                                    "state": "cancelled",
                                    "revision": request["revision"] + 1,
                                },
                            )
            if action == "run" and value["status"] in {"paused", "blocked"}:
                value = {
                    **value,
                    "status": "queued",
                    "resume_request": {
                        "id": "resume_" + uuid.uuid4().hex,
                        "control_revision": old["revision"] + 1,
                        "faults": copy.deepcopy(
                            db._get(conn, "checkpoints", value.get("checkpoint"), {})
                            .get("usage", {})
                            .get("connection_faults", {})
                        ),
                        "at": time.time(),
                    },
                    "revision": value["revision"] + 1,
                    "next_retry_at": None,
                }
                db._put(conn, "runs", identity, value)
        return self.get(identity)

    def submit(
        self,
        identity,
        text,
        *,
        mode="steer",
        expected_turn_id=None,
        expected_generation=None,
        input_id=None,
        literal=False,
    ):
        from homebrew_harness.runtime.host_commands import HostCommand

        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > 64000
            or not literal
            and HostCommand.parse(text).name
        ):
            raise ValueError(
                "Managed input must be task text; host controls use their dedicated commands."
            )
        if mode not in {"steer", "interrupt", "queued"}:
            raise ValueError("Invalid managed input mode.")
        db = self.runtime.backend()
        rid = run_id(identity)
        mid = input_id or "usr_" + uuid.uuid4().hex
        if not isinstance(mid, str) or not re.fullmatch(r"usr_[0-9a-f]{32}", mid):
            raise ValueError("Invalid stable user input ID.")
        with storage_gate(self.root), db.write() as conn:
            prior = db._get(conn, "input_submissions", mid)
            if prior is not None:
                if (
                    prior["to"] != "user_" + rid
                    or prior["content"] != text
                    or prior["metadata"]["mode"] != mode
                ):
                    raise ValueError(
                        "Input ID was already accepted with another payload or target."
                    )
                return {"input_id": mid, "state": "queued"}
            value = db._get(conn, "runs", rid)
            if not value or value["status"] in TERMINAL:
                raise ValueError("Run no longer accepts input.")
            if (
                expected_generation is not None
                and value["generation"] != expected_generation
            ):
                raise ValueError("The targeted managed execution changed.")
            recipient = "user_" + rid
            turn = db._get(conn, "input_turns", recipient)
            if expected_turn_id is not None and expected_turn_id != turn:
                raise ValueError("The targeted managed turn changed.")
            if (
                conn.execute(
                    "SELECT count(*) FROM inbox WHERE recipient=?", (recipient,)
                ).fetchone()[0]
                >= 64
            ):
                raise ValueError("Managed inbox is full.")
            record = {
                "id": mid,
                "checkpoint": "lead:" + value["session_id"],
                "from": "user",
                "to": recipient,
                "type": "user_input",
                "content": text,
                "ts": time.time(),
                "metadata": {
                    "origin": "host_user",
                    "mode": mode,
                    "target": "lead",
                    "turn_id": turn,
                },
            }
            db._send(conn, recipient, record)
            db._put(conn, "input_submissions", mid, record)
            if mode == "interrupt":
                old = db._get(
                    conn, "run_controls", rid, {"revision": 0, "action": "run"}
                )
                db._put(
                    conn,
                    "run_controls",
                    rid,
                    {**old, "revision": old["revision"] + 1, "interrupt_id": mid},
                )
        return {"input_id": mid, "state": "queued"}

    def heartbeat(self, header, fields):
        db = self.runtime.backend()
        with cleanup_scope(), storage_gate(self.root), db.write() as conn:
            owner_guard(db, conn, header)
            db._put(
                conn,
                "run_heartbeats",
                header["run_id"],
                {
                    **fields,
                    "owner": header["owner"],
                    "generation": header["generation"],
                    "at": time.time(),
                },
            )

    def update(self, header, fields):
        db = self.runtime.backend()
        with cleanup_scope(), storage_gate(self.root), db.write() as conn:
            value = owner_guard(db, conn, header)
            current = value
            value = {
                **value,
                **copy.deepcopy(fields),
                "revision": value["revision"] + 1,
            }
            db._put(conn, "runs", value["id"], value)
            close_controls(db, conn, value)
            if (
                fields.get("status") in {"blocked", "completed"}
                and fields["status"] != current["status"]
            ):
                from .effects import notice

                notice(
                    db,
                    conn,
                    value,
                    (value.get("last_outcome") or {}).get(
                        "reason_code", fields["status"]
                    ),
                )
        return value

    def ended(self, header, *, cleanup=False):
        db = self.runtime.backend()
        with cleanup_scope(), storage_gate(self.root), db.write() as conn:
            value = owner_guard(db, conn, header)
            old = db._get(conn, "run_attempts", header["owner"])
            db._put(
                conn,
                "run_attempts",
                header["owner"],
                {
                    **old,
                    "state": "ended",
                    "ended_at": time.time(),
                    "cleanup_complete": cleanup,
                },
            )
            outcome = value.get("last_outcome")
            updated = {
                **value,
                "cleanup_complete": cleanup,
                "revision": value["revision"] + 1,
            }
            if outcome:
                updated["last_outcome"] = {**outcome, "cleanup_complete": cleanup}
            db._put(conn, "runs", value["id"], updated)

    def finalize_terminal(self, identity, *, supervisor=None):
        """Quiescent, idempotent retirement of a decided Run's active ownership."""
        from contextlib import ExitStack
        from .supervisor import RunSupervisor

        current = self.get(identity)
        if current["status"] not in TERMINAL or current.get("finalized_at") is not None:
            return current.get("finalized_at") is not None
        checker = supervisor or RunSupervisor(self)
        try:
            with ExitStack() as held:
                if supervisor is None:
                    held.enter_context(checker.leases.use("managed-supervisor"))
                if checker.worker_alive(current) or not current.get("cleanup_complete"):
                    return False
                held.enter_context(checker.leases.use("managed-services"))
                held.enter_context(checker.leases.use("session"))
                held.enter_context(checker.leases.use("workspace:" + str(self.root)))
                owned_tasks = [
                    row
                    for row in self.runtime.items("tasks").values()
                    if (row.get("stage") or {}).get("run_id") == identity
                ]
                for row in owned_tasks:
                    held.enter_context(checker.leases.use("task:" + row["id"]))
                    if row["worktree"]:
                        held.enter_context(
                            checker.leases.use(
                                "workspace:"
                                + str(self.root / ".worktrees" / row["worktree"])
                            )
                        )
                if not checker.recovery_safe(current, execution_owned=True):
                    return False
                db = self.runtime.backend()
                finalization_id = uuid.uuid4().hex
                try:
                    with cleanup_scope(), storage_gate(self.root), db.write() as conn:
                        value = db._get(conn, "runs", identity)
                        if value != current:
                            return bool(value) and value.get("finalized_at") is not None
                        audit = {"id": finalization_id, "tasks": [], "goals": []}
                        for key, raw in conn.execute(
                            "SELECT key,value FROM records WHERE section='tasks'"
                        ).fetchall():
                            task = json.loads(raw)
                            if (task.get("stage") or {}).get(
                                "run_id"
                            ) == identity and task["status"] in {
                                "pending",
                                "in_progress",
                            }:
                                audit["tasks"].append(
                                    {
                                        "id": key,
                                        "status": task["status"],
                                        "owner": task["owner"],
                                    }
                                )
                                db._put(
                                    conn,
                                    "tasks",
                                    key,
                                    {**task, "status": "abandoned", "owner": None},
                                )
                        audit["inputs"] = []
                        for recipient, mid, raw in conn.execute(
                            "SELECT recipient,id,record FROM inbox"
                        ).fetchall():
                            message = json.loads(raw)
                            if (
                                recipient
                                in {"user_" + identity, "reference_" + identity}
                                or message.get("metadata", {}).get("run_id") == identity
                            ):
                                audit["inputs"].append(
                                    {"id": mid, "recipient": recipient}
                                )
                                db._put(
                                    conn,
                                    "input_receipts",
                                    mid,
                                    {
                                        "input_id": mid,
                                        "checkpoint": "lead:" + value["session_id"],
                                        "revision": 0,
                                        "state": "cancelled",
                                        "target": recipient,
                                        "record": message,
                                    },
                                )
                                conn.execute(
                                    "DELETE FROM inbox WHERE recipient=? AND id=?",
                                    (recipient, mid),
                                )
                                event = db._get(conn, "run_events", mid)
                                if event and event["state"] == "queued":
                                    db._put(
                                        conn,
                                        "run_events",
                                        mid,
                                        {
                                            **event,
                                            "state": "ignored",
                                            "ignore_reason": "run_terminated",
                                            "revision": event["revision"] + 1,
                                        },
                                    )
                        goal_ids = {"goal_" + identity[4:]}
                        for sid in value["session_refs"]:
                            saved = db._get(conn, "checkpoints", "lead:" + sid, {})
                            if saved.get("goal_id"):
                                goal_ids.add(saved["goal_id"])
                        from homebrew_harness.orchestration.goal_loop import (
                            TERMINAL as GOAL_TERMINAL,
                        )

                        for key in goal_ids:
                            goal = db._get(conn, "goals", key)
                            if goal and goal["status"] not in GOAL_TERMINAL:
                                audit["goals"].append(
                                    {"id": key, "status": goal["status"]}
                                )
                                db._put(
                                    conn,
                                    "goals",
                                    key,
                                    {
                                        **goal,
                                        "status": "abandoned",
                                        "last_reason": "Run ended: " + value["status"],
                                        "revision": goal.get("revision", 0) + 1,
                                    },
                                )
                        value = {
                            **value,
                            "finalized_at": time.time(),
                            "finalization": audit,
                            "revision": value["revision"] + 1,
                        }
                        close_controls(db, conn, value)
                        db._put(conn, "runs", identity, value)
                except Exception:
                    finalized = self.runtime.get("runs", current["id"], {})
                    if (finalized.get("finalization") or {}).get(
                        "id"
                    ) != finalization_id:
                        raise
                return True
        finally:
            if supervisor is None:
                checker.close()

    def status(self, identity):
        value = self.get(identity)
        value["heartbeat"] = self.runtime.get("run_heartbeats", identity)
        value["mcp_resources"] = [
            row
            for row in self.runtime.items("mcp_resources").values()
            if row.get("run_id") == identity
        ]
        from .requests import HumanRequests

        requests = HumanRequests(self).records(identity)
        value["requests"] = [
            r for r in requests if r["state"] in {"pending", "approved"}
        ]
        value["progress"] = self.runtime.get("run_progress", identity)
        value["member_outcomes"] = self.runtime.items(
            "run_member_outcomes", prefix=identity + ":"
        )
        effects = self.runtime.items("run_outbox", owner="lead:" + value["session_id"])
        value["effects"] = {
            state: sum(r["state"] == state for r in effects.values())
            for state in {r["state"] for r in effects.values()}
        }
        value["sources"] = self.runtime.items(
            "run_sources", owner="lead:" + value["session_id"]
        )
        value["control"] = self.runtime.get("run_controls", identity)
        value["pending_inputs"] = [
            {"input_id": r["id"], "mode": r["metadata"]["mode"]}
            for r in self.runtime.mailbox("inspect", "user_" + identity)
        ]
        return value
