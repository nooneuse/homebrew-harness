"""Detached service startup uses a persistent token and real ownership locks."""

import argparse
import os
import subprocess
import sys
import time
import uuid
import signal
import threading
from pathlib import Path

from homebrew_harness.execution.execution_leases import ExecutionLeases
from homebrew_harness.runtime.runtime_control import Workers
from homebrew_harness.security.safe_files import SafeFile
from .store import ServiceStore

REAPERS = Workers()


def live(root):
    leases = ExecutionLeases(root)
    try:
        try:
            leases.acquire("service")
        except ValueError:
            return True
        else:
            leases.release("service")
            return False
    finally:
        leases.close()


def gone(pid):
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def launch(store, identity, seconds=5):
    leases = ExecutionLeases(store.root)
    try:
        with leases.use("service-launch"):
            meta = store.get(identity)
            if live(store.root):
                return {"state": "accepted", "id": identity}
            prior = meta.get("launch") or {}
            if prior.get("phase") == "accepted" and gone(meta.get("pid")):
                prior = {**prior, "phase": "recovered"}
            if prior.get("phase") in {"pending", "accepted"}:
                return {
                    "state": "startup_pending",
                    "id": identity,
                    "token": prior.get("token"),
                }
            token = uuid.uuid4().hex
            with store.db.write() as conn:
                current = store.db._get(conn, "service_meta", identity)
                store.db._put(
                    conn,
                    "service_meta",
                    identity,
                    {**current, "launch": {"token": token, "phase": "pending"}},
                )
            reference = ".harness/service-supervisor-" + token + ".log"
            with SafeFile(store.root, reference, create_parents=True) as target:
                target.publish(b"", None, 0o600)
                fd = os.open(
                    target.parts[-1],
                    os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW,
                    dir_fd=target.fds[-1],
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
                                "--control",
                                str(store.root),
                                "--service-id",
                                identity,
                                "--token",
                                token,
                            ],
                            stdin=subprocess.DEVNULL,
                            stdout=log,
                            stderr=log,
                            start_new_session=True,
                        )
                except BaseException:
                    with store.db.write() as conn:
                        current = store.db._get(conn, "service_meta", identity)
                        store.db._put(
                            conn,
                            "service_meta",
                            identity,
                            {**current, "launch": {"token": token, "phase": "failed"}},
                        )
                    raise
            from homebrew_harness.runtime.runtime_control import WorkAccepted

            try:
                REAPERS.start(
                    process.wait, name="service-process-reaper", track_operation=False
                )
            except WorkAccepted:
                pass
            expires = time.monotonic() + seconds
            while time.monotonic() < expires:
                current = store.get(identity)
                record = current.get("launch") or {}
                if record.get("token") == token and record.get("phase") in {
                    "accepted",
                    "ended",
                    "failed",
                }:
                    return {"state": record["phase"], "id": identity, "token": token}
                if process.poll() is not None:
                    with store.db.write() as conn:
                        current = store.db._get(conn, "service_meta", identity)
                        store.db._put(
                            conn,
                            "service_meta",
                            identity,
                            {
                                **current,
                                "launch": {"token": token, "phase": "failed"},
                                "error": "startup_failed",
                            },
                        )
                    return {"state": "failed", "id": identity, "token": token}
                time.sleep(0.02)
            return {"state": "startup_pending", "id": identity, "token": token}
    finally:
        leases.close()


def recover_launch(store, identity, token):
    leases = ExecutionLeases(store.root)
    try:
        with leases.use("service-launch"), leases.use("service"):
            with store.db.write() as conn:
                value = store.db._get(conn, "service_meta", identity)
                record = value.get("launch") or {}
                if record.get("token") != token or record.get("phase") not in {
                    "pending",
                    "accepted",
                }:
                    raise ValueError("Startup token changed; refresh its saved state.")
                # Old delayed children must validate this token after acquiring the same lease.
                store.db._put(
                    conn,
                    "service_meta",
                    identity,
                    {**value, "launch": {"token": None, "phase": "recovered"}},
                )
                return {"id": identity, "state": "recovered"}
    finally:
        leases.close()


def main(argv=None):
    from homebrew_harness.api.core import Harness
    from .runtime import ServiceRuntime, saved_config

    parser = argparse.ArgumentParser(description="Persistent service supervisor")
    parser.add_argument("--control", type=Path, required=True)
    parser.add_argument("--service-id", required=True)
    parser.add_argument("--token", required=True)
    args = parser.parse_args(argv)
    store = ServiceStore(args.control)
    meta = store.get(args.service_id)
    core = Harness(workdir=args.control, config=saved_config(meta["config"]))
    runtime = None
    stop = threading.Event()
    previous = signal.signal(signal.SIGTERM, lambda *_args: stop.set())
    try:
        runtime = ServiceRuntime(core, store, args.service_id, token=args.token)
        runtime.start()
        while not runtime.wait_closed(0.25):
            if stop.is_set():
                runtime.close()
    except KeyboardInterrupt:
        if runtime is not None:
            runtime.close()
    finally:
        if runtime is not None:
            runtime.close()
            runtime.wait_closed(None)
        core.close()
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    main()
