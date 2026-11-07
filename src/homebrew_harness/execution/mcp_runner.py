"""Container stdio process ownership, isolated input scope and shared cleanup."""

import hashlib
import json
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from homebrew_harness.execution.bash_policy import BashPolicy, bash_snapshot
from homebrew_harness.execution.execution_backend import (
    ContainerBackend,
    ContainerPolicy,
    ExecutionRequest,
)
from homebrew_harness.execution.execution_control import check, cleanup_scope
from homebrew_harness.execution.execution_leases import lease_scope
from homebrew_harness.security.safe_files import SafeFile, canonical_root
from homebrew_harness.storage.durable_runtime import settle_activity, track_activity


@dataclass(frozen=True)
class MCPWorkspace:
    cwd: Path
    task_id: str | None
    identity: tuple

    @property
    def key(self):
        return (str(self.cwd), self.task_id, *self.identity)

    @classmethod
    def select(cls, root, cwd, task_id=None):
        original_root = Path(root).absolute()
        root = canonical_root(original_root)
        candidate = Path(cwd).absolute()
        try:
            relative = candidate.relative_to(original_root)
        except ValueError:
            try:
                relative = candidate.relative_to(root)
            except ValueError:
                raise ValueError(
                    "MCP workspace must use a path inside its owning root."
                ) from None
        with SafeFile(root, (relative / ".mcp-scope-anchor").as_posix()) as anchor:
            cwd = canonical_root(candidate)
            info = os.fstat(anchor.fds[-1])
        if (
            not cwd.is_relative_to(root)
            or task_id is not None
            and (
                not isinstance(task_id, str)
                or not re.fullmatch(r"task_[0-9a-f]{8}", task_id)
            )
        ):
            raise ValueError("MCP workspace must belong to its host and assigned task.")
        return cls(cwd, task_id, (info.st_dev, info.st_ino))


def snapshot_policy(policy):
    return BashPolicy(
        mode="container",
        cli=policy.cli,
        image=policy.image,
        uid=policy.uid,
        cpus=policy.cpus,
        memory_mb=policy.memory_mb,
        pids=policy.pids,
        max_files=policy.max_files,
        max_input_bytes=policy.max_input_bytes,
    )


class MCPContainerBackend(ContainerBackend):
    def __init__(self, spec):
        execution = spec.execution
        argv = (spec.command, *spec.args)
        super().__init__(
            ContainerPolicy(
                execution.cli,
                execution.image,
                argv,
                argv,
                cpus=execution.cpus,
                memory_mb=execution.memory_mb,
                pids=execution.pids,
                uid=execution.uid,
            )
        )
        self.execution = execution
        self.inputs = {}

    def _snapshot(self, request):
        if self.execution.inputs == "none":
            self.inputs = {}
        else:
            self.inputs, _ = bash_snapshot(
                request.cwd,
                snapshot_policy(self.execution),
                request.deadline,
                request.cancel_events,
            )
        return self.inputs

    def _create_options(self, request):
        return ["--interactive"]

    def _verified(self, value, *args):
        return (
            super()._verified(value, *args)
            and value["Config"].get("OpenStdin") is True
            and not value["Config"].get("Tty")
        )

    def _start_process(self, name, staging, request):
        return subprocess.Popen(
            [self.policy.cli, "start", "--attach", "--interactive", name],
            cwd=staging,
            env={"PATH": os.defpath},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            start_new_session=True,
        )


class MCPContainerOwner:
    def __init__(self, spec, root, workspace, leases, deadline):
        self.spec = spec
        self.root = canonical_root(Path(root))
        self.workspace = workspace
        self.leases = leases
        self.deadline = deadline
        self.cancel = threading.Event()
        self.backend = MCPContainerBackend(spec)
        self.process = None
        self._leased = False
        self.activity = track_activity(self)

    def launch(self):
        self.leases.acquire("session")
        self._leased = True
        execution = self.spec.execution
        cwd = self.workspace.cwd if self.workspace else self.root
        try:
            with lease_scope(self.leases), self.leases.use("workspace:" + str(cwd)):
                if (
                    self.workspace is not None
                    and MCPWorkspace.select(self.root, cwd, self.workspace.task_id)
                    != self.workspace
                ):
                    raise ValueError("MCP workspace changed before container launch.")
                self.process = self.backend.launch(
                    ExecutionRequest(
                        self.backend.policy.argv,
                        cwd,
                        {},
                        self.deadline,
                        recipe_argv=self.backend.policy.native_argv,
                        cpu_seconds=execution.cpu_seconds,
                        file_bytes=execution.file_bytes,
                        open_files=execution.open_files,
                        cancel_events=(self.cancel,),
                        owner_root=self.root,
                    )
                )
            return self.process
        except BaseException:
            # The actor calls settle even if preparation ended with a pending container.
            raise

    def validate(self, root, cwd, task_id, deadline, *, check_inputs=True):
        if self.workspace is None:
            return
        if MCPWorkspace.select(root, cwd, task_id) != self.workspace:
            raise ValueError(
                "MCP binding belongs to another task or workspace; connect it in the current context."
            )
        if not check_inputs:
            check(deadline)
            return
        with (
            lease_scope(self.leases),
            self.leases.use("workspace:" + str(self.workspace.cwd)),
        ):
            current, _ = bash_snapshot(
                self.workspace.cwd, snapshot_policy(self.spec.execution), deadline
            )
        if current != self.backend.inputs:
            raise ValueError(
                "MCP input snapshot changed; reconnect explicitly before calling this service."
            )
        check(deadline)

    def settle(self):
        from homebrew_harness.execution.background import ShellRunner

        self.cancel.set()
        while True:
            try:
                with cleanup_scope():
                    if self.process is not None:
                        ShellRunner._stop(self.process)
                        self.backend.finish(self.process)
                        self.process.wait(timeout=0.2)
                        for pipe in (
                            self.process.stdin,
                            self.process.stdout,
                            self.process.stderr,
                        ):
                            pipe.close()
                if self.backend.wait_closed(1):
                    if self._leased:
                        self.leases.release("session")
                        self._leased = False
                    settle_activity(self.activity, self)
                    return
            except Exception:
                # Actor remains owned and leased until host resources really settle.
                pass
            threading.Event().wait(0.5)

    def metadata(self):
        return {
            "mode": "container",
            "configuration_verified": self.process is not None,
            "inputs": self.spec.execution.inputs,
            "writeback": False,
            "policy_fingerprint": self.spec.execution.fingerprint(),
            "input_fingerprint": hashlib.sha256(
                json.dumps(self.backend.inputs, sort_keys=True).encode()
            ).hexdigest(),
            "cleanup_complete": self.backend.status()["cleanup_complete"],
        }
