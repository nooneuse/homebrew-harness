"""Fixed detached supervisor entry, with a real lease and durable startup ACK."""

import argparse
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from homebrew_harness.execution.execution_leases import ExecutionLeases
from homebrew_harness.execution.model_connections import read_environment
from homebrew_harness.runtime.interaction_config import InteractionConfig
from homebrew_harness.security.safe_files import SafeFile
from homebrew_harness.storage.storage_gate import storage_gate
from homebrew_harness.runtime.runtime_control import Workers

_REAPERS = Workers()

from .spec import TERMINAL
from .store import RunStore
from .supervisor import RunSupervisor


def detached_status(store, identity):
    record = store.runtime.get("run_supervisors", identity)
    if not record:
        return {"live": False}
    leases = ExecutionLeases(store.root)
    try:
        try:
            leases.acquire("detached-supervisor:" + identity)
        except ValueError:
            live = True
        else:
            leases.release("detached-supervisor:" + identity)
            live = False
    finally:
        leases.close()
    return {
        "live": live and record["phase"] == "accepted",
        "phase": record["phase"],
        "token": record["token"],
    }


def launch_detached(store, identity, config, *, startup_seconds=5):
    run = store.get(identity)
    if run["status"] in TERMINAL:
        return {"state": "ended", "run_id": identity}
    config.worker_arguments(store.root)
    leases = ExecutionLeases(store.root)
    process = None
    try:
        with leases.use("detached-launch:" + identity):
            existing = detached_status(store, identity)
            if existing.get("live"):
                return {"state": "accepted", "run_id": identity, **existing}
            prior = store.runtime.get("run_supervisors", identity)
            if prior and prior["phase"] in {"pending", "accepted"}:
                # A missing ACK is an uncertain launch, not permission to spawn
                # again. Neither PID reuse nor a missing lease proves no delayed
                # child will start. An explicit recovery must inspect this case.
                return {"state": "startup_pending", "run_id": identity, **existing}
            token = uuid.uuid4().hex
            record = {
                "run_id": identity,
                "token": token,
                "phase": "pending",
                "config": config.identity(),
                "at": time.time(),
            }
            store.runtime.put("run_supervisors", identity, record)
            reference = ".harness/runs/" + identity + "/supervisor-" + token + ".log"
            with SafeFile(store.root, reference, create_parents=True) as file:
                file.publish(b"", None, 0o600)
                fd = os.open(
                    file.parts[-1],
                    os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW,
                    dir_fd=file.fds[-1],
                )
            try:
                with os.fdopen(fd, "ab", buffering=0) as log:
                    process = subprocess.Popen(
                        [
                            sys.executable,
                            "-I",
                            "-B",
                            "-m",
                            __name__,
                            "--cwd",
                            str(store.root),
                            "--run-id",
                            identity,
                            "--token",
                            token,
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=log,
                        start_new_session=True,
                    )
            except Exception:
                store.runtime.put(
                    "run_supervisors", identity, {**record, "phase": "failed"}
                )
                raise
            _REAPERS.start(
                process.wait, name="detached-process-reaper", track_operation=False
            )
            deadline = time.monotonic() + startup_seconds
            while time.monotonic() < deadline:
                current = store.runtime.get("run_supervisors", identity)
                if current["token"] == token and current["phase"] in {
                    "accepted",
                    "ended",
                    "failed",
                }:
                    return {
                        "state": current["phase"],
                        "run_id": identity,
                        "token": token,
                    }
                if process.poll() is not None:
                    store.runtime.put(
                        "run_supervisors", identity, {**record, "phase": "failed"}
                    )
                    return {"state": "failed", "run_id": identity}
                time.sleep(0.02)
            return {"state": "startup_pending", "run_id": identity, "token": token}
    finally:
        leases.close()


def recover_detached(store, identity, token):
    """Fence an uncertain old launch only after actual execution is free."""
    leases = ExecutionLeases(store.root)
    checker = RunSupervisor(store)
    try:
        with (
            leases.use("detached-launch:" + identity),
            leases.use("detached-supervisor:" + identity),
            leases.use("managed-supervisor"),
            leases.use("session"),
        ):
            run = store.get(identity)
            record = store.runtime.get("run_supervisors", identity)
            if (
                not record
                or record["token"] != token
                or record["phase"] not in {"pending", "accepted"}
            ):
                raise ValueError("Detached launch changed; inspect the current token.")
            if not checker.recovery_safe(run, execution_owned=True):
                raise ValueError(
                    "Inspect uncertain operations and resource cleanup first."
                )
            db = store.runtime.backend()
            with storage_gate(store.root), db.write() as conn:
                if db._get(conn, "run_supervisors", identity) != record:
                    raise ValueError("Detached launch changed before fencing.")
                db._put(
                    conn, "run_supervisors", identity, {**record, "phase": "recovered"}
                )
            return {"state": "recovered", "run_id": identity}
    finally:
        checker.close()
        leases.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Private detached Run supervisor")
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--token", required=True)
    args = parser.parse_args(argv)
    store = RunStore(args.cwd)
    leases = ExecutionLeases(store.root)
    supervisor = None
    accepted = False
    try:
        run = store.get(args.run_id)
        record = store.runtime.get("run_supervisors", args.run_id)
        if (
            not record
            or record["token"] != args.token
            or record["phase"] != "pending"
            or run["status"] in TERMINAL
        ):
            raise ValueError("Detached launch is stale or already accepted.")
        raw = dict(record["config"])
        for name in (
            "env_file",
            "models_config",
            "test_config",
            "bash_config",
            "mcp_config",
            "control_config",
        ):
            if raw.get(name) is not None:
                raw[name] = Path(raw[name])
        config = InteractionConfig(**raw)
        from .services import RunServices

        supervisor = RunSupervisor(
            store,
            host_arguments=config.worker_arguments(store.root),
            services=RunServices.load(
                store,
                config.control_config,
                environment=read_environment(config.env_file, store.root),
            ),
        )
        with (
            leases.use("detached-supervisor:" + run["id"]),
            supervisor.leases.use("managed-supervisor"),
        ):
            db = store.runtime.backend()
            with storage_gate(store.root), db.write() as conn:
                if db._get(conn, "run_supervisors", run["id"]) != record:
                    raise ValueError("Detached launch changed before startup ACK.")
                db._put(
                    conn,
                    "run_supervisors",
                    run["id"],
                    {
                        **record,
                        "phase": "accepted",
                        "instance": leases.instance,
                        "pid": os.getpid(),
                    },
                )
            accepted = True
            result = supervisor.supervise(run["id"])
            store.runtime.put(
                "run_supervisors",
                run["id"],
                {**record, "phase": "ended", "status": result["status"]},
            )
        return 0
    except Exception as error:
        current = store.runtime.get("run_supervisors", args.run_id)
        if (
            current
            and current["token"] == args.token
            and current["phase"] in {"pending", "accepted"}
        ):
            store.runtime.put(
                "run_supervisors",
                args.run_id,
                {
                    **current,
                    "phase": "failed",
                    "error_type": type(error).__name__,
                    "accepted": accepted,
                },
            )
        return 1
    finally:
        if supervisor:
            supervisor.close()
        leases.close()
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
