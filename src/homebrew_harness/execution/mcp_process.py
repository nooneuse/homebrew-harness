"""An owned stdio process survives only while its worker lifeline is open."""

import argparse
import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from homebrew_harness.execution.execution_control import check, cleanup_scope
from homebrew_harness.execution.execution_leases import ExecutionLeases
from homebrew_harness.storage.durable_runtime import (
    DurableStore,
    settle_activity,
    track_activity,
)


def update_resource(store, identity, token, **fields):
    db = store.backend()
    with cleanup_scope(), db.write() as conn:
        current = db._get(conn, "mcp_resources", identity)
        if not current or current["token"] != token:
            raise ValueError("MCP process ownership changed.")
        value = {**current, **fields, "revision": current["revision"] + 1}
        db._put(conn, "mcp_resources", identity, value)
    return value


def inspect_resource(store, run_id, identity, revision, token):
    leases = ExecutionLeases(store.workdir)
    try:
        with leases.use("mcp-resource:" + identity), leases.use("session"):
            db = store.backend()
            with db.write() as conn:
                row = db._get(conn, "mcp_resources", identity)
                if (
                    not row
                    or row.get("run_id") != run_id
                    or row["token"] != token
                    or row["revision"] != revision
                    or row["phase"] == "closed"
                ):
                    raise ValueError(
                        "MCP resource inspection is stale or has another owner."
                    )
                db._put(
                    conn,
                    "mcp_resources",
                    identity,
                    {
                        **row,
                        "phase": "closed",
                        "inspected": True,
                        "revision": revision + 1,
                    },
                )
            return {
                "resource_id": identity,
                "state": "closed",
                "host_attestation": True,
            }
    finally:
        leases.close()


class MCPProcessOwner:
    def __init__(self, spec, root, leases, scope, deadline, environment):
        self.spec, self.root, self.leases = spec, root, leases
        self.scope, self.deadline = scope, deadline
        self.environment = environment
        self.cancel = threading.Event()
        self.process = None
        self.life = None
        self.id, self.token = "mcp_" + uuid.uuid4().hex, uuid.uuid4().hex
        self.store = DurableStore(root)
        self._leased = False
        self.activity = track_activity(self)

    def launch(self):
        self.leases.acquire("session")
        self._leased = True
        record = {
            "id": self.id,
            "token": self.token,
            "phase": "prepared",
            "revision": 0,
            "server": self.spec.name,
            "checkpoint": "lead:" + self.scope["session_id"],
            "mode": self.spec.execution.mode,
            **self.scope,
        }
        try:
            self.store.put("mcp_resources", self.id, record)
        except Exception:
            if self.store.get("mcp_resources", self.id) != record:
                raise
        payload = {
            "argv": [self.spec.command, *self.spec.args],
            "cwd": str(self.spec.cwd) if self.spec.cwd is not None else None,
            "env": self.environment,
        }
        payload.update(getattr(self, "container_config", {}))
        raw = json.dumps(payload).encode()
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError("MCP process configuration exceeds its limit.")
        life_read, self.life = os.pipe()
        config_read, config_write = os.pipe()
        try:
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-m",
                    __name__,
                    "--root",
                    str(self.root),
                    "--resource",
                    self.id,
                    "--token",
                    self.token,
                    "--lifeline",
                    str(life_read),
                    "--config",
                    str(config_read),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=(life_read, config_read),
                start_new_session=True,
            )
            os.close(life_read)
            life_read = None
            os.close(config_read)
            config_read = None
            os.set_blocking(config_write, False)
            while raw:
                if self.cancel.is_set():
                    raise ValueError("MCP process launch cancelled.")
                check(self.deadline)
                if select.select(
                    [], [config_write], [], min(0.05, self.deadline.remaining())
                )[1]:
                    try:
                        sent = os.write(config_write, raw)
                    except BlockingIOError:
                        continue
                    raw = raw[sent:]
            return self.process
        finally:
            for fd in (life_read, config_read, config_write):
                if fd is not None:
                    os.close(fd)

    def settle(self):
        self.cancel.set()
        if self.life is not None:
            os.close(self.life)
            self.life = None
        if self.process is not None:
            self.process.wait()
            for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
                pipe.close()
            row = self.store.get("mcp_resources", self.id)
            if row["phase"] != "closed":
                raise ValueError(
                    "MCP process cleanup has not been confirmed by its owner."
                )
        elif self.store.get("mcp_resources", self.id):
            update_resource(self.store, self.id, self.token, phase="closed")
        if self._leased:
            self.leases.release("session")
            self._leased = False
        settle_activity(self.activity, self)

    def metadata(self):
        row = self.store.get("mcp_resources", self.id, {})
        return {
            "mode": self.spec.execution.mode,
            "resource_id": self.id,
            "cleanup_complete": row.get("phase") == "closed",
        }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Private MCP process owner")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--resource", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--lifeline", type=int, required=True)
    parser.add_argument("--config", type=int, required=True)
    args = parser.parse_args(argv)
    store, leases = DurableStore(args.root), ExecutionLeases(args.root)
    child = backend = None
    interrupted = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: interrupted.set())
    signal.signal(signal.SIGINT, lambda *_: interrupted.set())
    try:
        leases.acquire("mcp-resource:" + args.resource)
        with os.fdopen(args.config, "rb") as config:
            raw = config.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError("MCP configuration exceeds its limit.")
        value = json.loads(raw)
        row = store.get("mcp_resources", args.resource)
        if not row or row["token"] != args.token or row["phase"] != "prepared":
            raise ValueError("MCP process acceptance is stale.")
        if "execution" in value:
            from homebrew_harness.execution.mcp_runner import MCPContainerBackend
            from homebrew_harness.execution.mcp_policy import MCPExecutionPolicy
            from homebrew_harness.execution.execution_backend import ExecutionRequest
            from homebrew_harness.execution.model_gateway import Deadline
            from homebrew_harness.tools.mcp.runtime import ServerSpec

            policy = MCPExecutionPolicy(**value["execution"])
            spec = ServerSpec(
                "resource",
                "stdio",
                command=value["argv"][0],
                args=tuple(value["argv"][1:]),
                execution=policy,
            )
            backend = MCPContainerBackend(spec)
            child = backend.launch(
                ExecutionRequest(
                    backend.policy.argv,
                    Path(value["cwd"]),
                    {},
                    Deadline(value["expires_at"]),
                    recipe_argv=backend.policy.native_argv,
                    cpu_seconds=policy.cpu_seconds,
                    file_bytes=policy.file_bytes,
                    open_files=policy.open_files,
                    owner_root=args.root,
                    source_snapshot=value["inputs"],
                    cancel_events=(interrupted,),
                )
            )

            def relay(pipe, destination):
                try:
                    while data := os.read(pipe.fileno(), 65536):
                        if destination is not None:
                            destination.write(data)
                            destination.flush()
                except (OSError, ValueError):
                    pass

            threading.Thread(
                target=relay, args=(child.stdout, sys.stdout.buffer), daemon=True
            ).start()
            threading.Thread(
                target=relay, args=(child.stderr, None), daemon=True
            ).start()
        else:
            child = subprocess.Popen(
                value["argv"],
                cwd=value["cwd"],
                env=value["env"],
                stdin=subprocess.PIPE,
                stdout=sys.stdout.buffer,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        update_resource(
            store,
            args.resource,
            args.token,
            phase="started",
            pid=os.getpid(),
            child_pid=child.pid,
        )

        def forward():
            try:
                while True:
                    data = os.read(sys.stdin.fileno(), 65536)
                    if not data:
                        break
                    child.stdin.write(data)
                    child.stdin.flush()
            except (OSError, ValueError):
                pass
            interrupted.set()

        threading.Thread(target=forward, name="mcp-stdin", daemon=True).start()
        while child.poll() is None and not interrupted.is_set():
            if select.select([args.lifeline], [], [], 0.05)[0] and not os.read(
                args.lifeline, 1
            ):
                break
    finally:
        # Only this surviving owner signals the group it actually launched.
        if backend is not None:
            from homebrew_harness.execution.background import ShellRunner

            if child is not None:
                ShellRunner._stop(child)
                backend.finish(child)
            while not backend.wait_closed(1):
                time.sleep(0.05)
            if child is not None:
                child.wait()
                for pipe in (child.stdin, child.stdout, child.stderr):
                    pipe.close()
        elif child is not None:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(child.pid, sig)
                except ProcessLookupError:
                    break
                except PermissionError:
                    # Darwin can report EPERM for a group containing only the
                    # exited child. Reap our handle and require the group gone;
                    # never signal an unverified historical PID on recovery.
                    if child.poll() is None:
                        raise
                    try:
                        os.killpg(child.pid, 0)
                    except ProcessLookupError:
                        break
                    raise
                if sig == signal.SIGTERM:
                    time.sleep(0.05)
            child.wait()
            child.stdin.close()
        try:
            update_resource(store, args.resource, args.token, phase="closed")
        finally:
            leases.close()
            os.close(args.lifeline)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
