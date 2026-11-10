"""Small host service loop for durable wakeups, PR input and notifications."""

import json
import os
import re
import time
import uuid
from contextlib import ExitStack
from pathlib import Path

import httpx

from homebrew_harness.execution.model_gateway import Deadline
from homebrew_harness.execution.execution_leases import ExecutionLeases
from homebrew_harness.storage.storage_gate import storage_gate
from .effects import enqueue
from .requests import digest
from .sources import ChangeRequestSource
from homebrew_harness.platforms.http import JsonTransport, RemoteFailure
from .spec import RunOutcome, TERMINAL

EMPTY_CONTROL_ID = digest({"change_request": None, "notification": None})


def env_value(name, environment=None, *, required=True):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,100}", name):
        raise ValueError(
            "External service configuration requires an environment variable name."
        )
    value = (os.environ if environment is None else environment).get(name, "").strip()
    if required and not value:
        raise ValueError("Missing external service environment variable " + name)
    return value


class RunServices:
    def __init__(self, store, config=None, *, environment=None):
        self.store, self.source, self.notification = store, None, None
        self.started_clock, self.started_monotonic = time.time(), time.monotonic()
        raw = {"version": 1} if config is None else config
        if (
            not isinstance(raw, dict)
            or set(raw) - {"version", "change_request", "notification"}
            or type(raw.get("version")) is not int
            or raw["version"] != 1
        ):
            raise ValueError("Invalid managed service configuration.")
        with ExitStack() as resources:
            self.leases = ExecutionLeases(store.root)
            resources.callback(self.leases.close)
            feedback = raw.get("change_request")
            if feedback is not None:
                self.source = ChangeRequestSource(
                    feedback,
                    os.environ if environment is None else environment,
                    roots=(store.root,),
                )
                resources.callback(self.source.close)
            notification = raw.get("notification")
            if notification is not None:
                if (
                    not isinstance(notification, dict)
                    or set(notification) - {"url_env", "token_env"}
                    or "url_env" not in notification
                ):
                    raise ValueError("Invalid notification fields.")
                self.notification = JsonTransport(
                    env_value(notification["url_env"], environment),
                    env_value(notification["token_env"], environment)
                    if notification.get("token_env")
                    else "",
                )
                resources.callback(self.notification.close)
            self.identity = digest(
                {
                    "change_request": self.source.identity if self.source else None,
                    "notification": self.notification.identity
                    if self.notification
                    else None,
                }
            )
            resources.pop_all()

    @classmethod
    def load(cls, store, path, *, environment=None):
        if path is None:
            return cls(store, environment=environment)
        path = Path(path).expanduser().resolve()
        if path.is_relative_to(store.root):
            raise ValueError(
                "Managed service configuration must be outside the workspace."
            )
        with path.open("rb") as file:
            raw = file.read(100001)
        if len(raw) > 100000:
            raise ValueError("Managed service configuration exceeds its size limit.")
        return cls(store, json.loads(raw), environment=environment)

    def bind(self, identity):
        run = self.store.get(identity)
        old = run.get("control_identity", EMPTY_CONTROL_ID)
        if old != self.identity:
            if (
                old != EMPTY_CONTROL_ID
                or run["submitted"]
                or run["episode"]
                or run["bindings"] is not None
            ):
                raise ValueError(
                    "Managed service identity changed; restore the original host configuration."
                )
            self.store.update(
                {
                    "run_id": identity,
                    "owner": run["owner"],
                    "generation": run["generation"],
                },
                {
                    "control_identity": self.identity,
                    "notify_enabled": self.notification is not None,
                    "source_enabled": self.source is not None,
                },
            )

    def deadline(self, run):
        remaining = max(
            0,
            min(
                run["deadline_at"] - time.time(),
                run["deadline_at"]
                - self.started_clock
                - (time.monotonic() - self.started_monotonic),
            ),
        )
        return Deadline.after(min(10, remaining))

    def _reconcile_wait(self, identity):
        snapshot = self.store.get(identity)
        if snapshot["status"] != "waiting" or not snapshot["cleanup_complete"]:
            return
        db = self.store.runtime.backend()
        with storage_gate(self.store.root), db.write() as conn:
            run = db._get(conn, "runs", identity)
            if time.time() >= run["deadline_at"]:
                return
            rows = [
                json.loads(row[0])
                for row in conn.execute(
                    "SELECT value FROM records WHERE section='run_requests' AND owner=?",
                    ("lead:" + run["session_id"],),
                ).fetchall()
            ]
            expired = False
            for row in rows:
                if row["state"] not in {"pending", "approved"}:
                    continue
                status = (
                    "expired"
                    if time.time() > row["expires_at"]
                    else "stale"
                    if row["context"]["epoch"]
                    != db._get(conn, "input_epochs", "user_" + identity, 0)
                    else None
                )
                if status:
                    row.update(state=status, revision=row["revision"] + 1)
                    db._put(conn, "run_requests", row["id"], row)
                    expired |= status == "expired"
            control = db._get(conn, "run_controls", identity, {"action": "run"})
            if (
                run["status"] != "waiting"
                or not run["cleanup_complete"]
                or control["action"] != "run"
            ):
                return
            if expired:
                run.update(
                    status="blocked",
                    revision=run["revision"] + 1,
                    last_outcome=RunOutcome(
                        identity,
                        run["episode"],
                        "blocked",
                        "approval_expired",
                        run["checkpoint"],
                        required_host_actions=(
                            "Review expired requests and explicitly resume",
                        ),
                        cleanup_complete=True,
                    ).data(),
                )
                db._put(conn, "runs", identity, run)
                from .effects import notice

                notice(db, conn, run, "approval_expired")
                return
            if run.get("source_enabled") and "source" in (
                run.get("last_outcome") or {}
            ).get("pending_work", ()):
                sources = [
                    json.loads(row[0])
                    for row in conn.execute(
                        "SELECT value FROM records WHERE section='run_sources' AND owner=?",
                        ("lead:" + run["session_id"],),
                    ).fetchall()
                ]
                if not sources or any(r.get("failures", 0) for r in sources):
                    return
            incoming = conn.execute(
                "SELECT count(*) FROM inbox WHERE recipient=?", ("user_" + identity,)
            ).fetchone()[0]
            if incoming or not any(r["state"] == "pending" for r in rows):
                enqueue(db, conn, run, "wake", {}, "ready:" + str(run["revision"]))

    def _wake(self, effect):
        db = self.store.runtime.backend()
        with storage_gate(self.store.root), db.write() as conn:
            current = db._get(conn, "run_outbox", effect["id"])
            if current["state"] != "queued":
                return
            run = db._get(conn, "runs", current["run_id"])
            control = db._get(conn, "run_controls", run["id"], {"action": "run"})
            if (
                run["status"] == "waiting"
                and run["cleanup_complete"]
                and control["action"] == "run"
                and current["target"] == self.identity
            ):
                count = conn.execute(
                    "SELECT count(*) FROM inbox WHERE recipient=?",
                    ("user_" + run["id"],),
                ).fetchone()[0]
                pending = conn.execute(
                    "SELECT value FROM records WHERE section='run_requests' AND owner=?",
                    ("lead:" + run["session_id"],),
                ).fetchall()
                sources = conn.execute(
                    "SELECT value FROM records WHERE section='run_sources' AND owner=?",
                    ("lead:" + run["session_id"],),
                ).fetchall()
                source_wait = "source" in (run.get("last_outcome") or {}).get(
                    "pending_work", ()
                )
                healthy = (
                    not source_wait
                    or bool(sources)
                    and all(
                        not json.loads(row[0]).get("failures", 0) for row in sources
                    )
                )
                if (
                    count
                    or healthy
                    and not any(
                        json.loads(row[0])["state"] == "pending" for row in pending
                    )
                ):
                    db._put(
                        conn,
                        "runs",
                        run["id"],
                        {**run, "status": "queued", "revision": run["revision"] + 1},
                    )
            db._put(
                conn,
                "run_outbox",
                current["id"],
                {**current, "state": "delivered", "revision": current["revision"] + 1},
            )

    def _notification(self, effect, deadline):
        db = self.store.runtime.backend()
        owner = uuid.uuid4().hex
        with storage_gate(self.store.root), db.write() as conn:
            current = db._get(conn, "run_outbox", effect["id"])
            if current["state"] != "queued" or current["next_retry_at"] > time.time():
                return
            run = db._get(conn, "runs", current["run_id"])
            if self.notification is None or current["target"] != self.identity:
                return
            request = (
                db._get(conn, "run_requests", current["payload"].get("request_id"))
                if current["payload"].get("request_id")
                else None
            )
            if request is not None and request["state"] != "pending":
                db._put(
                    conn,
                    "run_outbox",
                    current["id"],
                    {
                        **current,
                        "state": "cancelled",
                        "revision": current["revision"] + 1,
                    },
                )
                return
            if (
                current["payload"]["status"] == "completed"
                and not run["cleanup_complete"]
            ):
                return
            accepted = {
                **current,
                "state": "started",
                "owner": owner,
                "attempts": current["attempts"] + 1,
                "revision": current["revision"] + 1,
            }
            db._put(conn, "run_outbox", current["id"], accepted)
        status, retry_at, error = "delivered", 0, None
        try:
            self.notification.request(
                "POST",
                "",
                deadline,
                body=accepted["payload"],
                headers={"Idempotency-Key": accepted["id"]},
                parse_json=False,
            )
        except RemoteFailure as failure:
            error = "http_" + str(failure.status)
            if failure.status == 429 and accepted["attempts"] < 3:
                status, retry_at = "queued", time.time() + 2 ** accepted["attempts"]
            else:
                status = "unknown" if failure.status >= 500 else "failed"
        except (httpx.ConnectError, httpx.ConnectTimeout) as failure:
            error = type(failure).__name__
            status = "queued" if accepted["attempts"] < 3 else "failed"
            retry_at = time.time() + 2 ** accepted["attempts"]
        except Exception as failure:
            status, error = "unknown", type(failure).__name__
        with storage_gate(self.store.root), db.write() as conn:
            current = db._get(conn, "run_outbox", effect["id"])
            if (
                current.get("owner") != owner
                or current["revision"] != accepted["revision"]
            ):
                raise ValueError("Notification ownership changed before settlement.")
            db._put(
                conn,
                "run_outbox",
                current["id"],
                {
                    **current,
                    "state": status,
                    "next_retry_at": retry_at,
                    "last_error": error,
                    "revision": current["revision"] + 1,
                },
            )

    def tick(self, identity):
        with self.leases.use("managed-services"):
            return self._tick(identity)

    def _tick(self, identity):
        run = self.store.get(identity)
        if (
            self.source is None
            and self.notification is None
            and run["status"] == "running"
        ):
            return
        if self.source is not None and run["status"] not in TERMINAL:
            self.source.poll(self.store, identity, self.deadline(run))
        self._reconcile_wait(identity)
        effects = self.store.runtime.items(
            "run_outbox", owner="lead:" + run["session_id"]
        )
        delivery_deadline = Deadline.after(2)
        if run["status"] not in TERMINAL:
            delivery_deadline = delivery_deadline.intersect(self.deadline(run))
        # An abandoned started notification is uncertain, never an automatic resend.
        db = self.store.runtime.backend()
        for effect in effects.values():
            if effect["state"] == "started":
                with storage_gate(self.store.root), db.write() as conn:
                    current = db._get(conn, "run_outbox", effect["id"])
                    if current["revision"] == effect["revision"]:
                        db._put(
                            conn,
                            "run_outbox",
                            effect["id"],
                            {
                                **current,
                                "state": "unknown",
                                "revision": current["revision"] + 1,
                            },
                        )
        queued = sorted(
            (e for e in effects.values() if e["state"] == "queued"),
            key=lambda e: e["created_at"],
        )
        for effect in queued[:64]:
            if effect["state"] == "queued":
                if effect["kind"] == "wake":
                    self._wake(effect)
                else:
                    if delivery_deadline.remaining() == 0:
                        break
                    try:
                        self._notification(effect, delivery_deadline)
                    except Exception:
                        # The started journal retains uncertainty; observer failure cannot change the task result.
                        continue

    def resolve(self, identity, effect_id, revision, outcome, reason):
        if type(revision) is not int or revision < 0:
            raise ValueError("Effect inspection needs a current integer revision.")
        if (
            outcome not in {"delivered", "not_sent"}
            or not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > 1000
        ):
            raise ValueError("Effect inspection needs an observed result and a reason.")
        db = self.store.runtime.backend()
        with storage_gate(self.store.root), db.write() as conn:
            effect = db._get(conn, "run_outbox", effect_id)
            if (
                not effect
                or effect["run_id"] != identity
                or effect["revision"] != revision
                or effect["state"] != "unknown"
                or effect["target"] != self.identity
            ):
                raise ValueError(
                    "Effect is changed or not an inspectable notification."
                )
            from homebrew_harness.security.data_boundary import redact

            updated = {
                **effect,
                "state": "delivered" if outcome == "delivered" else "cancelled",
                "revision": revision + 1,
                "inspection": {"outcome": outcome, "reason": redact(reason)},
            }
            db._put(conn, "run_outbox", effect_id, updated)
        return updated

    def close(self):
        try:
            if self.source is not None:
                self.source.close()
        finally:
            try:
                if self.notification is not None:
                    self.notification.close()
            finally:
                self.leases.close()
