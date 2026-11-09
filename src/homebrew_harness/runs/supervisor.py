"""Own a worker process; restart only after real execution ownership is free."""

import os
import subprocess
import sys
import time

from homebrew_harness.execution.execution_leases import ExecutionLeases
from homebrew_harness.execution.execution_control import cleanup_scope
from homebrew_harness.security.safe_files import SafeFile
from .spec import RunSpec, RunOutcome, TERMINAL


class RunSupervisor:
    def __init__(
        self,
        store,
        *,
        worker_command=None,
        host_arguments=(),
        services=None,
        model_handoff=None,
        on_update=None,
    ):
        self.store = store
        from homebrew_harness.runtime.observability import Observations
        from homebrew_harness.runtime.runtime_control import Workers

        self.on_update = on_update
        self._updates, self._observations = Workers(), Observations()
        self._next_update = 0
        self.model_handoff = model_handoff
        self.worker_command = tuple(
            worker_command
            or (sys.executable, "-I", "-B", "-m", "homebrew_harness.runs.worker")
        )
        self.host_arguments = tuple(host_arguments)
        self.leases = ExecutionLeases(store.root)
        self.process = None
        from .services import RunServices

        self.services = services or RunServices(store)

    def _tick(self, identity):
        self.services.tick(identity)
        callback = self.on_update
        if (
            callback is not None
            and time.monotonic() >= self._next_update
            and self._updates.wait(0)
        ):
            self._next_update = time.monotonic() + 0.2
            self._updates.start(
                lambda: self._observations.run("on_update", lambda: callback(identity)),
                name="run-observer",
                track_operation=False,
            )

    def execution_free(self):
        try:
            self.leases.acquire("session")
        except ValueError:
            return False
        else:
            self.leases.release("session")
            return True

    def worker_alive(self, value):
        if not value.get("owner"):
            return False
        attempt = self.store.runtime.get("run_attempts", value["owner"], {})
        pid = attempt.get("pid")
        if type(pid) is not int or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _close_process(self, process, seconds):
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=seconds)

    def recovery_safe(self, value, *, execution_owned=False):
        if not execution_owned and not self.execution_free():
            return False
        runtime = self.store.runtime
        for row in runtime.items("mcp_resources").values():
            if row.get("run_id") == value["id"] and row["phase"] != "closed":
                return False
        from homebrew_harness.tools.mcp.selection import safe_read

        for sid in value.get("session_refs", [value["session_id"]]):
            scopes = [
                runtime.items("operations", owner="lead:" + sid),
                runtime.items("operations", owner_prefix="member:" + sid + ":"),
                runtime.items("operations", owner_prefix="sub:" + sid + ":"),
            ]
            if any(
                r.get("state") in {"started", "unknown", "partial"}
                and not safe_read(r, value)
                for rows in scopes
                for r in rows.values()
            ):
                return False
        if any(
            r.get("state") in {"started", "unknown", "partial"}
            for r in runtime.items("run_transfers").values()
            if r.get("run_id") == value["id"]
        ):
            return False
        if any(
            r.get("status") == "running"
            or r.get("status") == "unknown"
            and not r.get("inspected")
            for r in runtime.items("background").values()
        ):
            return False
        for path in (self.store.root / ".harness/containers").glob("*.json"):
            import json

            try:
                with SafeFile(self.store.root, str(path)) as target:
                    raw, _, _ = target.snapshot(max_bytes=65536)
                record = json.loads(raw)
                if record.get("phase") != "removed":
                    return False
            except (ValueError, OSError):
                return False
        return True

    def wait_mcp_cleanup(self, value, seconds):
        """Give surviving stdio owners a bounded chance to publish closure."""
        until = time.monotonic() + seconds
        while any(
            row.get("run_id") == value["id"] and row["phase"] != "closed"
            for row in self.store.runtime.items("mcp_resources").values()
        ):
            if time.monotonic() >= until:
                return
            time.sleep(min(0.05, max(0, until - time.monotonic())))

    def reconcile_completed(self, value):
        if (
            value["status"] != "completed"
            or value.get("cleanup_complete")
            or self.worker_alive(value)
            or not self.recovery_safe(value)
        ):
            return False
        from homebrew_harness.runtime.runtime_state import settle_dead_usage

        with self.leases.use("session"), cleanup_scope():
            key = value.get("checkpoint")
            snapshot = self.store.runtime.get("checkpoints", key) if key else None
            meta = self.store.runtime.get("session_meta", value["session_id"])
            if (
                not snapshot
                or snapshot.get("phase") not in {"executed", "closed"}
                or snapshot.get("managed_run", {}).get("run_id") != value["id"]
                or not meta
                or meta.get("instance") != snapshot.get("host_instance")
            ):
                return False
            self.store.runtime.checkpoint(
                key,
                {
                    **snapshot,
                    "phase": "closed",
                    "usage": settle_dead_usage(snapshot.get("usage", {})),
                },
                expected_revision=self.store.runtime.checkpoint_revision(key),
            )
            self.store.runtime.finish_session(
                value["session_id"], meta["instance"], complete=True
            )
            header = {
                "run_id": value["id"],
                "owner": value["owner"],
                "generation": value["generation"],
            }
            self.store.ended(header, cleanup=True)
        return True

    def supervise(self, identity):
        started_clock = time.time()
        started_monotonic = time.monotonic()
        with self.leases.use("managed-supervisor"):
            self.services.bind(identity)
            while True:
                self._tick(identity)
                value = self.store.get(identity)
                spec = RunSpec.from_dict(value["spec"])
                if self.reconcile_completed(value):
                    self.store.finalize_terminal(identity, supervisor=self)
                    return self.store.status(identity)
                if value["status"] in TERMINAL:
                    self.store.finalize_terminal(identity, supervisor=self)
                if self.try_model_handoff(value):
                    continue
                if value["status"] in TERMINAL or value["status"] in {
                    "blocked",
                    "paused",
                }:
                    return self.store.status(identity)
                if not self.execution_free():
                    return {
                        **self.store.status(identity),
                        "supervisor_reason": "execution_lease_held",
                    }
                if self.worker_alive(value):
                    return {
                        **self.store.status(identity),
                        "supervisor_reason": "worker_process_alive",
                    }
                control = self.store.runtime.get(
                    "run_controls", identity, {"action": "run"}
                )
                if control["action"] in {"pause", "cancel"}:
                    header = {
                        "run_id": identity,
                        "owner": value["owner"],
                        "generation": value["generation"],
                    }
                    safe = self.recovery_safe(value)
                    status = "paused" if control["action"] == "pause" else "cancelled"
                    outcome = RunOutcome(
                        identity,
                        value["episode"],
                        status,
                        control["action"],
                        value.get("checkpoint") or "lead:" + value["session_id"],
                        cleanup_complete=safe,
                        required_host_actions=()
                        if safe
                        else ("Inspect saved operations and cleanup",),
                    )
                    self.store.update(
                        header,
                        {
                            "status": status,
                            "last_outcome": outcome.data(),
                            "cleanup_complete": safe,
                        },
                    )
                    return self.store.status(identity)
                if time.time() < value["last_clock"] - 1:
                    header = {
                        "run_id": identity,
                        "owner": value["owner"],
                        "generation": value["generation"],
                    }
                    self.store.update(
                        header,
                        {
                            "status": "blocked",
                            "last_outcome": RunOutcome(
                                identity,
                                value["episode"],
                                "blocked",
                                "clock_changed",
                                value.get("checkpoint")
                                or "lead:" + value["session_id"],
                            ).data(),
                        },
                    )
                    return self.store.status(identity)
                if time.time() >= value[
                    "deadline_at"
                ] or time.monotonic() - started_monotonic >= max(
                    0, value["deadline_at"] - started_clock
                ):
                    safe = self.recovery_safe(value)
                    header = {
                        "run_id": identity,
                        "owner": value["owner"],
                        "generation": value["generation"],
                    }
                    outcome = RunOutcome(
                        identity,
                        value["episode"],
                        "failed",
                        "deadline_reached",
                        value.get("checkpoint") or "lead:" + value["session_id"],
                        cleanup_complete=safe,
                        required_host_actions=()
                        if safe
                        else (
                            "Inspect saved operations and process/container cleanup",
                        ),
                    )
                    self.store.update(
                        header,
                        {
                            "status": "failed",
                            "last_outcome": outcome.data(),
                            "cleanup_complete": safe,
                        },
                    )
                    return self.store.status(identity)
                if value["status"] == "waiting":
                    time.sleep(0.1)
                    continue
                if value.get("next_retry_at") and value["next_retry_at"] > time.time():
                    control = self.store.runtime.get(
                        "run_controls", identity, {"action": "run"}
                    )
                    if control["action"] == "run":
                        time.sleep(min(0.1, value["next_retry_at"] - time.time()))
                        continue
                value = self.store.claim(identity)
                header = {
                    "run_id": identity,
                    "owner": value["owner"],
                    "generation": value["generation"],
                }
                reference = ".harness/runs/" + identity + "/" + value["owner"] + ".log"
                with (
                    cleanup_scope(),
                    SafeFile(self.store.root, reference, create_parents=True) as file,
                ):
                    file.publish(b"", None, 0o600)
                    fd = os.open(
                        file.parts[-1],
                        os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW,
                        dir_fd=file.fds[-1],
                    )
                process = None
                try:
                    with os.fdopen(fd, "ab", buffering=0) as log:
                        process = subprocess.Popen(
                            [
                                *self.worker_command,
                                "--cwd",
                                str(self.store.root),
                                "--run-id",
                                identity,
                                "--owner",
                                header["owner"],
                                "--generation",
                                str(header["generation"]),
                                *self.host_arguments,
                            ],
                            stdin=subprocess.DEVNULL,
                            stdout=log,
                            stderr=log,
                            start_new_session=True,
                        )
                    self.process = process
                    self.store.pid(header, process.pid)
                    started = time.monotonic()
                    stopping = None
                    watchdog_reason = None
                    while process.poll() is None:
                        self._tick(identity)
                        now = time.time()
                        control = self.store.runtime.get(
                            "run_controls", identity, {"action": "run"}
                        )
                        heartbeat = self.store.runtime.get(
                            "run_heartbeats", identity, {}
                        )
                        last = (
                            heartbeat.get("at", 0)
                            if heartbeat.get("owner") == header["owner"]
                            else 0
                        )
                        stale = (
                            now - last > spec.heartbeat_timeout
                            if last
                            else time.monotonic() - started > spec.heartbeat_timeout
                        )
                        expired = now >= value[
                            "deadline_at"
                        ] or time.monotonic() - started_monotonic >= max(
                            0, value["deadline_at"] - started_clock
                        )
                        if expired or stale or control["action"] in {"pause", "cancel"}:
                            if stopping is None:
                                stopping = time.monotonic()
                                watchdog_reason = (
                                    "deadline_reached"
                                    if expired
                                    else "heartbeat_stale"
                                    if stale
                                    else control["action"]
                                )
                            if time.monotonic() - stopping >= spec.shutdown_seconds:
                                self._close_process(process, spec.shutdown_seconds)
                                break
                        time.sleep(0.05)
                    current = self.store.get(identity)
                    if spec.mcp:
                        self.wait_mcp_cleanup(current, spec.shutdown_seconds)
                    if current["cleanup_complete"] and current["status"] == "waiting":
                        continue
                    if current["cleanup_complete"] and current["status"] in TERMINAL:
                        self._tick(identity)
                    if self.reconcile_completed(current):
                        self.store.finalize_terminal(identity, supervisor=self)
                        return self.store.status(identity)
                    if current["status"] in TERMINAL:
                        self.store.finalize_terminal(identity, supervisor=self)
                    if current.get("cleanup_complete") is not True:
                        if watchdog_reason in {
                            "pause",
                            "cancel",
                            "deadline_reached",
                        } and self.recovery_safe(current):
                            status = (
                                "paused"
                                if watchdog_reason == "pause"
                                else "cancelled"
                                if watchdog_reason == "cancel"
                                else "failed"
                            )
                            outcome = RunOutcome(
                                identity,
                                current["episode"],
                                status,
                                watchdog_reason,
                                current.get("checkpoint")
                                or "lead:" + current["session_id"],
                                cleanup_complete=True,
                            )
                            self.store.update(
                                header,
                                {"status": status, "last_outcome": outcome.data()},
                            )
                            self.store.ended(header, cleanup=True)
                            return self.store.status(identity)
                        if current["status"] == "blocked":
                            return self.store.status(identity)
                        if (
                            current["status"] in {"running", "retrying"}
                            and watchdog_reason is None
                            and self.recovery_safe(current)
                        ):
                            # Recovery inspects checkpoint operations before any model or tool starts.
                            count = current["recovery_count"] + 1
                            if count <= spec.retries:
                                self.store.update(
                                    header,
                                    {
                                        "status": "retrying",
                                        "recovery_count": count,
                                        "next_retry_at": time.time()
                                        + min(60, spec.retry_delay * 2 ** (count - 1)),
                                    },
                                )
                                continue
                        reason = watchdog_reason or "worker_exit_unknown"
                        outcome = RunOutcome(
                            identity,
                            current["episode"],
                            "blocked",
                            reason,
                            current.get("checkpoint")
                            or "lead:" + current["session_id"],
                            required_host_actions=(
                                "Inspect saved operations and process/container cleanup",
                            ),
                        )
                        self.store.update(
                            header,
                            {"status": "blocked", "last_outcome": outcome.data()},
                        )
                    elif current["status"] in {"retrying", "queued"}:
                        continue
                    if self.try_model_handoff(self.store.get(identity)):
                        continue
                    return self.store.status(identity)
                except BaseException:
                    if process is not None:
                        self._close_process(process, spec.shutdown_seconds)
                    self.store.update(
                        header,
                        {
                            "status": "blocked",
                            "last_outcome": RunOutcome(
                                identity,
                                value["episode"],
                                "blocked",
                                "worker_launch_failed",
                                "lead:" + value["session_id"],
                                required_host_actions=("Inspect worker startup",),
                            ).data(),
                        },
                    )
                    raise
                finally:
                    self.process = None

    def try_model_handoff(self, value):
        if (
            self.model_handoff is None
            or value["status"] != "blocked"
            or not value["cleanup_complete"]
        ):
            return False
        spec = RunSpec.from_dict(value["spec"])
        if (value.get("last_outcome") or {}).get(
            "reason_code"
        ) not in spec.fallback_errors:
            return False
        choices = [
            name
            for name in spec.fallback_models
            if name not in value.get("used_fallbacks", [])
            and name != value.get("selected_model")
        ]
        if not choices:
            return False
        try:
            self.model_handoff(self, value, choices[0])
        except (ValueError, OSError):
            # Failed preflight does not relax configuration or spin on another profile.
            return False
        return True

    def close(self):
        if self.process is not None:
            self._close_process(self.process, 1)
        try:
            self.services.close()
        finally:
            self.leases.close()
            self.store.close()
