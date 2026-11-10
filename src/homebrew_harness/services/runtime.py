"""A serial service coordinator; all agent execution stays in native Runs."""

import json
import threading
import time
import uuid
from pathlib import Path

from homebrew_harness.api.config import HostConfig
from homebrew_harness.api.types import SDKError, ExecutionRef, RequestRef
from homebrew_harness.execution.execution_leases import ExecutionLeases
from homebrew_harness.runtime.runtime_control import Workers, Shutdown
from .control import ControlServer
from .protocol import validate_method
from .spec import ServiceSpec, runtime_binding, fingerprint, request_key


def saved_config(values):
    return HostConfig.from_files(**values)


def prepare_binding(core, spec):
    from homebrew_harness.api.core import Harness

    spec.validate_location(core.workdir)
    core.config.require_reconstructable()
    core.config.settings.worker_arguments(core.workdir)
    with Harness(workdir=spec.workspace, config=core.config) as host:
        bindings, _head, controls = host.runs.prepare(
            spec.run_spec("Validate service execution scope")
        )
    return {"runtime": runtime_binding(), "execution": bindings, "controls": controls}


class ServiceRuntime:
    def __init__(self, core, store, identity, *, token=None):
        self.core, self.store, self.id = core, store, identity
        self.spec = ServiceSpec(**store.get(identity)["spec"])
        self.spec.validate_location(store.root)
        self.lock = threading.RLock()
        self.workers = Workers()
        self.shutdown = Shutdown()
        self.finished = threading.Event()
        self.closed = False
        self.server = None
        self.child = None
        self.current = None
        self.error = None
        self.start_error = None
        self.next_close_retry = 0
        self.leases = ExecutionLeases(store.root)
        self.workspace_lease = ExecutionLeases(Path(self.spec.workspace))
        try:
            self.leases.acquire("service")
            self.workspace_lease.acquire("service-workspace")
            meta = store.get(identity)
            if token is not None and (
                (meta.get("launch") or {}).get("token") != token
                or (meta.get("launch") or {}).get("phase") != "pending"
            ):
                raise ValueError("Service startup token was superseded.")
            from homebrew_harness.api.core import Harness

            self.child = Harness(
                workdir=self.spec.workspace, config=saved_config(meta["config"])
            )
            binding = self._binding()
            self.header = store.claim(identity, binding)
            self.server = ControlServer(
                store.root,
                self.dispatch,
                service_id=self.id,
                port=self.spec.control_port,
            )
            self.token = token
            self.started = False
            self.server_started = False
        except BaseException:
            if self.server is not None:
                self.server.server_close()
            if self.child is not None:
                self.child.close()
            self.workspace_lease.close()
            self.leases.close()
            raise

    def start(self):
        from homebrew_harness.runtime.runtime_control import WorkAccepted

        if self.started:
            return
        self.started = True
        try:
            self.workers.start(
                lambda: self.server.serve_forever(poll_interval=0.05),
                name="service-control",
                track_operation=False,
            )
        except WorkAccepted:
            pass
        self.server_started = True
        try:
            self.workers.start(
                self.loop, name="service-coordinator", track_operation=False
            )
        except WorkAccepted:
            pass
        self.store.update(
            self.header, launch={"token": self.token, "phase": "accepted"}, error=None
        )

    def _binding(self):
        bindings, _head, controls = self.child.runs.prepare(
            self.spec.run_spec("Validate service execution scope")
        )
        return {
            "runtime": runtime_binding(),
            "execution": bindings,
            "controls": controls,
        }

    def snapshot(self):
        meta = self.store.get(self.id)
        with self.store.db.connection() as conn:
            counts = {
                state: count
                for state, count in conn.execute(
                    "SELECT json_extract(value,'$.state'),count(*) FROM records WHERE section='service_work' AND owner=? GROUP BY json_extract(value,'$.state')",
                    ("service:" + self.id,),
                )
            }
        return {
            "id": self.id,
            "state": meta["state"],
            "scope": meta["scope"],
            "revision": meta["revision"],
            "generation": meta["generation"],
            "resources_closed": meta["resources_closed"],
            "live_owner": self.leases.held("service") and not self.finished.is_set(),
            "heartbeat_at": meta["heartbeat_at"],
            "error": meta.get("error"),
            "work_counts": counts,
            "active_work": meta.get("active_work"),
            "active_run": meta.get("active_run"),
            "launch": meta.get("launch"),
            "binding": meta["binding"],
        }

    def loop(self):
        heartbeat = 0
        while not self.finished.wait(self.spec.poll_seconds):
            try:
                meta = self.store.get(self.id)
                if meta["state"] == "stopping":
                    self.closed = True
                    if time.monotonic() >= self.next_close_retry:
                        self.shutdown.request(self._finish_close, 0)
                        self.next_close_retry = time.monotonic() + 2
                    continue
                with self.lock:
                    self.recover_commands()
                    if time.time() < meta.get("last_clock", meta["created_at"]) - 1:
                        self.store.update(
                            self.header,
                            state="paused",
                            scope="all",
                            error="clock_changed",
                            revision=meta["revision"] + 1,
                        )
                    else:
                        self.tick()
                if time.monotonic() - heartbeat >= 1:
                    self.store.update(self.header, heartbeat_at=time.time())
                    self.store.update(self.header, last_clock=time.time())
                    heartbeat = time.monotonic()
            except Exception as error:
                self.error = type(error).__name__
                try:
                    self.store.update(self.header, error=self.error)
                except Exception:
                    pass

    def recover_commands(self):
        with self.store.db.connection() as conn:
            rows = conn.execute(
                "SELECT value FROM records WHERE section='service_commands' AND owner=? AND json_extract(value,'$.result') IS NULL ORDER BY rowid LIMIT 16",
                ("service:" + self.id,),
            ).fetchall()
        for (raw,) in rows:
            value = json.loads(raw)
            try:
                self._external(value["method"], value["arguments"], value["request_id"])
            except Exception as error:
                key = request_key("command", self.id, value["request_id"])
                with self.store.db.write() as conn:
                    self.store.guard(conn, self.header)
                    current = self.store.db._get(conn, "service_commands", key)
                    self.store.db._put(
                        conn,
                        "service_commands",
                        key,
                        {
                            **current,
                            "result": {
                                "error": type(error).__name__,
                                "code": getattr(error, "code", "command_failed"),
                            },
                        },
                    )

    def _handle(self, work):
        handle = self.child.runs.get(work["run_id"], control=True)
        if self.current is None or self.current.id != handle.id:
            self.current = handle
        return self.current

    def tick(self):
        meta = self.store.get(self.id)
        if meta["state"] == "running":
            self.store.poll_schedules(self.header)
        work = self.store.next_work(self.id)
        if work is None:
            if meta.get("active_work"):
                self.store.update(self.header, active_work=None, active_run=None)
            return
        if not work["run_id"]:
            if (
                meta["state"] != "running"
                or work["pause_requested"]
                or work["cancel_requested"]
            ):
                return
            if meta["binding"]["runtime"] != runtime_binding():
                raise ValueError(
                    "Installed service runtime changed; restart with its pinned version."
                )
            self.store.update_work(self.header, work["id"], state="in_progress")
            try:
                handle = self.child.runs.start(
                    self.spec.run_spec(work["prompt"], work["goal"]),
                    request_id=work["launch_id"],
                )
            except Exception as error:
                # External work may occupy this workspace. Never control or cancel it.
                with self.child.store.backend().connection() as conn:
                    busy = conn.execute(
                        "SELECT 1 FROM records WHERE section='runs' AND (json_extract(value,'$.status') NOT IN ('completed','failed','cancelled') OR json_extract(value,'$.finalized_at') IS NULL) LIMIT 1"
                    ).fetchone()
                self.store.update_work(
                    self.header,
                    work["id"],
                    state="approved" if busy else "failed",
                    error="workspace_busy" if busy else type(error).__name__,
                )
                return
            self.current = handle
            work = self.store.update_work(
                self.header, work["id"], run_id=handle.id, error=None
            )
        handle = self._handle(work)
        state = self.child.run_store.get(handle.id)
        self.store.update(self.header, active_work=work["id"], active_run=handle.id)
        controls = self.child.store.get("run_controls", handle.id, {})
        if work["cancel_requested"] and state["status"] not in {
            "completed",
            "failed",
            "cancelled",
        }:
            if controls.get("action") != "cancel":
                handle.cancel()
        elif meta["state"] == "paused" or work["pause_requested"]:
            if (
                state["status"] in {"queued", "running", "waiting", "retrying"}
                and controls.get("action") != "pause"
            ):
                if meta["state"] == "paused":
                    self.store.update_work(self.header, work["id"], service_paused=True)
                handle.pause()
        elif work.get("service_paused"):
            if state["status"] == "paused" and state["cleanup_complete"]:
                handle.resume()
            if state["status"] in {
                "queued",
                "running",
                "waiting",
                "retrying",
                "paused",
            }:
                self.store.update_work(self.header, work["id"], service_paused=False)
        elif state["status"] not in {
            "blocked",
            "paused",
            "completed",
            "failed",
            "cancelled",
        }:
            if handle.supervisor is None or handle.workers.wait(0):
                handle.supervise()
        snapshot = handle.snapshot()
        if state["status"] in {"completed", "failed", "cancelled"}:
            if handle.supervisor is None:
                handle.supervise()
            latest = self.child.run_store.get(handle.id)
            if latest["cleanup_complete"] and latest.get("finalized_at"):
                outcome = latest.get("last_outcome") or {}
                self.store.update_work(
                    self.header,
                    work["id"],
                    state="review"
                    if latest["status"] == "completed"
                    else latest["status"],
                    error=outcome.get("reason_code")
                    if latest["status"] != "completed"
                    else None,
                    result=outcome,
                )
                if work.get("job"):
                    with self.store.db.write() as conn:
                        self.store.guard(conn, self.header)
                        job = self.store.db._get(conn, "service_jobs", work["job"])
                        job["last_completed"] = time.time()
                        self.store.db._put(conn, "service_jobs", work["job"], job)
                self.current = None
        if snapshot.reason:
            self.store.update(self.header, error=None)

    def work_snapshot(self, identity):
        work = self.store.work(identity, self.id)
        work = self.store.summary(work)
        if work["run_id"]:
            work = {
                **work,
                "execution": self.child.runs.get(work["run_id"]).snapshot().data(),
            }
        return work

    def requests(self, work_id=None):
        work = (
            self.store.work(work_id, self.id)
            if work_id
            else self.store.next_work(self.id)
        )
        if not work or not work["run_id"]:
            return []
        return [
            r.data()
            for r in self.child.requests.list(
                target=ExecutionRef("run", work["run_id"])
            )
        ]

    def _local(self, method, payload, request_id):
        def apply(conn):
            meta = self.store.guard(conn, self.header)
            if method == "submit":
                if meta["state"] in {"stopped", "stopping"}:
                    raise ValueError("Service is stopping; no new work is accepted.")
                work = self.store.enqueue(
                    conn,
                    self.id,
                    payload["text"],
                    request_id,
                    payload.get("goal"),
                    payload.get("priority", 0),
                )
                return {"id": work["id"], "accepted": True, "request_id": request_id}
            if method == "focus":
                from .spec import NAME

                context = payload["context"]
                if not isinstance(context, str) or not NAME.fullmatch(context):
                    raise ValueError("Invalid control context.")
                work = (
                    self.store.db._get(conn, "service_work", payload["id"])
                    if payload.get("id")
                    else None
                )
                if payload.get("id") and (
                    not work
                    or work["service_id"] != self.id
                    or work["revision"] != payload["revision"]
                ):
                    raise ValueError("Work focus changed; refresh its snapshot.")
                focus = {
                    "work_id": work["id"] if work else None,
                    "revision": work["revision"] if work else None,
                    "checkpoint": "service:" + self.id,
                }
                self.store.db._put(
                    conn, "service_focus", self.id + ":" + context, focus
                )
                return focus
            expected = payload.get("revision")
            if type(expected) is not int or expected != meta["revision"]:
                raise ValueError("Service revision changed; refresh its snapshot.")
            if method == "pause":
                if payload.get("scope") not in {"all", "execution"}:
                    raise ValueError("Pause scope must be all or execution.")
                meta.update(state="paused", scope=payload["scope"])
            elif method == "resume":
                if meta["state"] == "stopping":
                    raise ValueError("Service is still closing.")
                meta.update(
                    state="running",
                    scope="execution",
                    last_clock=time.time(),
                    error=None,
                )
            elif method == "stop":
                meta.update(state="stopping", scope="all")
            else:
                raise ValueError("Unknown local service command.")
            meta["revision"] += 1
            self.store.db._put(conn, "service_meta", self.id, meta)
            return {
                "id": self.id,
                "accepted": True,
                "revision": meta["revision"],
                "request_id": request_id,
            }

        return self.store.command(self.header, method, payload, request_id, apply)

    def _external(self, method, payload, request_id):
        key = request_key("command", self.id, request_id)
        signature = fingerprint([method, payload])
        with self.store.db.write() as conn:
            meta = self.store.guard(conn, self.header)
            prior = self.store.db._get(conn, "service_commands", key)
            if prior:
                if prior["payload"] != signature:
                    raise ValueError("Control request_id has another payload.")
                if prior.get("result") is not None:
                    if "error" in prior["result"]:
                        raise SDKError(
                            "Saved command failed (" + prior["result"]["error"] + ").",
                            code=prior["result"].get("code", "command_failed"),
                        )
                    return prior["result"]
                target = prior["target"]
            else:
                if meta["state"] == "stopping":
                    raise ValueError("Service is stopping.")
                if method == "decide":
                    ref = payload["ref"]
                    rid = ref["target"]["id"]
                    row = conn.execute(
                        "SELECT value FROM records WHERE section='service_work' AND owner=? AND json_extract(value,'$.run_id')=? LIMIT 1",
                        ("service:" + self.id, rid),
                    ).fetchone()
                    if not row or ref["target"]["kind"] != "run" or not ref["durable"]:
                        raise ValueError("Request belongs to another service.")
                    target = json.loads(row[0])
                else:
                    target = self.store.db._get(conn, "service_work", payload["id"])
                    if not target or target["service_id"] != self.id:
                        raise ValueError("Work item belongs to another service.")
                    if (
                        type(payload.get("revision")) is not int
                        or target["revision"] != payload["revision"]
                    ):
                        raise ValueError("Work revision changed; refresh its snapshot.")
                prior = {
                    "service_id": self.id,
                    "method": method,
                    "payload": signature,
                    "arguments": payload,
                    "request_id": request_id,
                    "target": target,
                    "result": None,
                    "checkpoint": "service:" + self.id,
                }
                self.store.db._put(conn, "service_commands", key, prior)
        target = self.store.work(target["id"], self.id)
        if not target["run_id"]:
            if method not in {"work_cancel", "work_pause", "work_resume"}:
                raise ValueError("Work has no native execution yet.")
            changes = (
                {"cancel_requested": True, "state": "cancelled"}
                if method == "work_cancel"
                else {"pause_requested": method == "work_pause"}
            )
            result = {"id": target["id"], "accepted": True, "request_id": request_id}
        else:
            handle = self._handle(target)
            state = handle.snapshot().state
            changes = {}
            if method == "work_cancel":
                if state not in {"completed", "cancelled", "failed"}:
                    handle.cancel()
                changes = {"cancel_requested": True}
            elif method == "work_pause":
                if state not in {"completed", "cancelled", "failed", "paused"}:
                    handle.pause()
                changes = {"pause_requested": True}
            elif method == "work_resume":
                if self.store.get(self.id)["state"] != "running":
                    raise ValueError("Resume the service before resuming a Run.")
                if state in {"paused", "blocked"}:
                    handle.resume()
                elif state not in {"completed", "cancelled", "failed"}:
                    handle.supervise()
                changes = {"pause_requested": False, "service_paused": False}
            elif method in {"steer", "interrupt"}:
                getattr(handle, method)(
                    payload["text"], request_id="service-control-" + key
                )
                changes = {
                    "amendments": [*target.get("amendments", []), payload["text"]],
                    "requirements_revision": target["requirements_revision"] + 1,
                }
            elif method == "decide":
                ref = payload["ref"]
                reference = RequestRef(
                    ref["id"],
                    ref["revision"],
                    ExecutionRef(**ref["target"]),
                    ref["durable"],
                )
                if reference.target.owner_id not in {None, self.id}:
                    raise ValueError("Request belongs to another service owner.")
                reference = RequestRef(
                    ref["id"],
                    ref["revision"],
                    ExecutionRef("run", ref["target"]["id"]),
                    ref["durable"],
                )
                action = payload["decision"]
                if action not in {"approve", "deny", "answer", "renew_tests"}:
                    raise ValueError("Invalid decision type.")
                arguments = {
                    k: payload[k] for k in ("text", "runs", "seconds") if k in payload
                }
                result = getattr(self.child.requests, action)(
                    reference, decision_id="service-control-" + key, **arguments
                )
            else:
                raise ValueError("Unknown work command.")
            if method != "decide":
                result = {
                    "id": target["id"],
                    "accepted": True,
                    "request_id": request_id,
                }
        with self.store.db.write() as conn:
            self.store.guard(conn, self.header)
            if changes:
                current = self.store.db._get(conn, "service_work", target["id"])
                self.store.db._put(
                    conn,
                    "service_work",
                    target["id"],
                    {**current, **changes, "revision": current["revision"] + 1},
                )
            current = self.store.db._get(conn, "service_commands", key)
            self.store.db._put(
                conn, "service_commands", key, {**current, "result": result}
            )
        return result

    def dispatch(self, identity, method, payload, request_id=None):
        if identity != self.id:
            raise ValueError("Control command targets another service.")
        validate_method(method, payload)
        with self.lock:
            if method == "snapshot":
                return self.snapshot()
            if method == "focus_get":
                return self.store.runtime.get(
                    "service_focus", self.id + ":" + payload["context"]
                )
            if method == "work_get":
                return self.work_snapshot(payload["id"])
            if method == "work_input":
                return self.store.input_page(
                    self.id,
                    payload["id"],
                    **{k: v for k, v in payload.items() if k != "id"},
                )
            if method == "work_list":
                return self.store.page(self.id, **payload)
            if method == "schedules":
                return self.store.page(self.id, section="service_jobs", **payload)
            if method == "requests":
                return self.requests(payload.get("work_id"))
            if method == "receipt":
                return self.store.runtime.get(
                    "service_commands",
                    request_key("command", self.id, payload["request_id"]),
                )
            request_id = request_id or "command_" + uuid.uuid4().hex
            if method == "tick":
                return self.store.command(
                    self.header,
                    method,
                    payload,
                    request_id,
                    lambda conn: {
                        "accepted": True,
                        "request_id": request_id,
                        "triggers": self.store.poll_schedules(
                            self.header, connection=conn
                        ),
                    },
                )
            if method in {"submit", "pause", "resume", "stop", "focus"}:
                return self._local(method, payload, request_id)
            return self._external(method, payload, request_id)

    def close(self, timeout=0):
        if self.finished.is_set():
            return self.snapshot()
        meta = self.store.get(self.id)
        if meta["state"] != "stopping":
            self.store.update(
                self.header,
                state="stopping",
                scope="all",
                revision=meta["revision"] + 1,
            )
        self.closed = True
        self.next_close_retry = 0
        self.shutdown.request(self._finish_close, timeout)
        return self.snapshot()

    def _finish_close(self):
        with self.lock:
            work = self.store.next_work(self.id)
            if work is not None and work["run_id"]:
                handle = self._handle(work)
                native = self.child.run_store.get(handle.id)
                if native["status"] in {"queued", "running", "waiting", "retrying"}:
                    if not work["pause_requested"]:
                        self.store.update_work(
                            self.header, work["id"], service_paused=True
                        )
                    controls = self.child.store.get("run_controls", handle.id, {})
                    if controls.get("action") != "pause":
                        handle.pause()
                if (
                    native["status"] not in {"completed", "failed", "cancelled"}
                    and not handle.owned
                ):
                    handle.supervise()
            report = self.child.close(self.spec.shutdown_seconds)
        while not report.resources_closed:
            report = self.child.wait_closed(0.1)
            if self.child.shutdown.error:
                raise SDKError(
                    "Native Run cleanup requires inspection or retry.",
                    code="cleanup_pending",
                )
        if self.server_started:
            self.server.close()
        else:
            self.server.server_close()
            self.server.remove_descriptor()
        self.store.update(
            self.header,
            state="stopped",
            resources_closed=True,
            owner=None,
            pid=None,
            launch={
                "token": (self.store.get(self.id).get("launch") or {}).get("token"),
                "phase": "ended",
            },
            error=self.start_error if report.persistence_ok else "persistence_failed",
        )
        self.workspace_lease.close()
        self.leases.close()
        self.finished.set()

    def wait_closed(self, timeout=5):
        return self.finished.wait(timeout)
