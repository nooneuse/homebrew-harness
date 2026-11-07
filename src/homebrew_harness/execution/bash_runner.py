"""Restricted Bash uses the shared process capture and Docker lifecycle."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import uuid
from contextvars import ContextVar

from homebrew_harness.execution.background import ShellRunner
from homebrew_harness.execution.bash_policy import (
    HOST_POLICY,
    BashPolicy,
    bash_snapshot,
    current_bash_policy,
)
from homebrew_harness.execution.execution_backend import (
    BackendUnavailable,
    ContainerBackend,
    ContainerPolicy,
    ExecutionRequest,
)
from homebrew_harness.tools.tool_outcome import ToolExecutionError

_OUTPUT = ContextVar("bash_output_profile", default=None)


class BashContainerBackend(ContainerBackend):
    def __init__(self, policy: BashPolicy, command, profile=None):
        argv = (
            policy.bash_path,
            "--noprofile",
            "--norc",
            "-c",
            'cp -a --no-preserve=ownership /inputs/. /workspace/ && cd /workspace && exec "$0" --noprofile --norc -c "$1"',
            policy.bash_path,
            command,
        )
        self.command_argv = argv
        self.profile = profile
        if profile is not None:
            argv = (
                policy.bash_path,
                "--noprofile",
                "--norc",
                "-c",
                "while :; do sleep 3600; done",
            )
        super().__init__(
            ContainerPolicy(
                policy.cli,
                policy.image,
                argv,
                argv,
                cpus=policy.cpus,
                memory_mb=policy.memory_mb,
                pids=policy.pids,
                uid=policy.uid,
                workspace_writable=True,
                workspace_bytes=policy.workspace_bytes,
            )
        )
        self.bash_policy = policy
        self.inputs = {}

    def _snapshot(self, request):
        values, self.inputs = bash_snapshot(
            request.cwd, self.bash_policy, request.deadline, request.cancel_events
        )
        return values

    def _start_process(self, name, staging, request):
        if self.profile is None:
            return super()._start_process(name, staging, request)
        from homebrew_harness.execution.execution_control import check

        start = self._control(
            [self.policy.cli, "start", name],
            request.deadline.remaining() or 0.001,
            request.cancel_events,
        )
        if start.returncode != 0:
            raise BackendUnavailable("Artifact container keeper did not start.")
        check(request.deadline, request.cancel_events)
        return subprocess.Popen(
            [
                self.policy.cli,
                "exec",
                "--workdir",
                "/workspace",
                name,
                *self.command_argv,
            ],
            cwd=staging,
            env={"PATH": os.defpath},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

    def export(self, process):
        from homebrew_harness.execution.artifact_export import COLLECTOR, decode_export

        with self.lock:
            name, _, _ = self.active[process.pid]
            request, _ = self.requests[process.pid]
        runner = ShellRunner()
        self.retain_control(runner)
        try:
            result = runner.run(
                [
                    self.policy.cli,
                    "exec",
                    name,
                    self.bash_policy.export_python,
                    "-I",
                    "-S",
                    "-c",
                    COLLECTOR,
                    json.dumps(self.profile.data()),
                ],
                request.cwd,
                {"PATH": os.defpath},
                request.deadline.remaining() or 0.001,
                output_bytes=self.profile.max_bytes * 2 + 65536,
                cancel_events=request.cancel_events,
            )
            if result.returncode != 0 or getattr(result, "output_limit", False):
                from homebrew_harness.execution.artifact_policy import ArtifactError

                raise ArtifactError(
                    "Artifact export was incomplete; no workspace files were published."
                )
            return decode_export(result.stdout, self.profile)
        finally:
            runner.close(0.2)


class ContainerShellRunner(ShellRunner):
    def __init__(self, policy, artifact_store=None):
        super().__init__()
        self.bash_policy = policy
        self._executions = {}
        self._by_pid = {}
        self.artifact_store = artifact_store
        self._artifact_results = {}

    def _metadata(self, identity, backend, isolation=None):
        return {
            "id": identity,
            "mode": "container",
            "workspace": "isolated_copy",
            "writeback": False,
            "policy_fingerprint": self.bash_policy.fingerprint(),
            "inputs": dict(backend.inputs),
            "isolation": isolation or backend.status(),
            **(
                {"artifacts": self._artifact_results[identity]}
                if identity in self._artifact_results
                else {}
            ),
        }

    def run(
        self,
        command,
        workdir,
        env,
        timeout,
        *,
        output_bytes=8 * 1024 * 1024,
        cancel_events=(),
        on_started=None,
        artifact_profile=None,
    ):
        if not isinstance(command, str):
            raise BackendUnavailable(
                "Container Bash accepts shell text only; host management uses its own execution chain."
            )
        profile = None
        if artifact_profile is not None:
            from homebrew_harness.execution.artifact_policy import ArtifactError

            profile = next(
                (p for p in self.bash_policy.outputs if p.name == artifact_profile),
                None,
            )
            if profile is None:
                raise ArtifactError(
                    "Artifact output rule is not registered by the host."
                )
        token = _OUTPUT.set(profile)
        try:
            return super().run(
                command,
                workdir,
                {},
                min(timeout, self.bash_policy.timeout),
                output_bytes=min(output_bytes, self.bash_policy.output_bytes),
                cancel_events=cancel_events,
                on_started=on_started,
            )
        finally:
            _OUTPUT.reset(token)

    def _launch(self, command, workdir, env, deadline, cancel_events):
        policy = self.bash_policy
        profile = _OUTPUT.get()
        backend = (
            BashContainerBackend(policy, command)
            if profile is None
            else BashContainerBackend(policy, command, profile)
        )
        identity = uuid.uuid4().hex
        done = threading.Event()
        from homebrew_harness.storage.durable_runtime import track_activity

        activity = track_activity(backend)
        with self._lock:
            self._executions[identity] = (backend, done, activity)
        request = ExecutionRequest(
            backend.policy.argv,
            workdir,
            {"LANG": "C.UTF-8"},
            deadline,
            recipe_argv=backend.policy.native_argv,
            cpu_seconds=policy.cpu_seconds,
            file_bytes=policy.file_bytes,
            open_files=policy.open_files,
            cancel_events=cancel_events,
        )
        try:
            process = backend.launch(request)
        except BaseException as error:
            if backend.status()["cleanup_complete"]:
                with self._lock:
                    self._executions.pop(identity, None)
                done.set()
                from homebrew_harness.storage.durable_runtime import settle_activity

                settle_activity(activity, backend)
            else:
                self._defer(lambda: self._settle_backend(identity))
                raise ToolExecutionError(
                    "Container Bash preparation was interrupted; cleanup remains owned. No host fallback occurred.",
                    outcome="unknown",
                    details={
                        "stage": "bash_container_prepare",
                        **self._metadata(identity, backend),
                    },
                ) from error
            error.bash_execution = self._metadata(identity, backend)
            raise
        with self._lock:
            self._by_pid[process.pid] = identity
        return process

    def _finish(self, process):
        with self._lock:
            identity = self._by_pid[process.pid]
            backend, _, _ = self._executions[identity]
        try:
            self._collect_finish(process, identity, backend)
        finally:
            try:
                isolation = backend.finish(process)
            except Exception:
                isolation = backend.status()  # Shared owner retains incomplete cleanup.
        return self._metadata(identity, backend, isolation)

    def _collect_finish(self, process, identity, backend):
        if (
            getattr(backend, "profile", None) is not None
            and getattr(process, "harness_capture_complete", False)
            and process.returncode is not None
            and process.returncode >= 0
        ):
            try:
                from homebrew_harness.storage.artifact_store import (
                    ArtifactStore,
                    current_artifact_task,
                )

                files, missing = backend.export(process)
                with backend.lock:
                    cwd = backend.active[process.pid][2]
                store = self.artifact_store or ArtifactStore(cwd, self.bash_policy)
                self._artifact_results[identity] = store.collect(
                    identity,
                    cwd,
                    backend.profile,
                    files,
                    missing,
                    current_artifact_task(),
                )
            except Exception as error:
                self._artifact_results[identity] = {
                    "state": "collection_unknown"
                    if isinstance(error, ToolExecutionError)
                    else "export_failed",
                    **(error.details if isinstance(error, ToolExecutionError) else {}),
                    "error_type": type(error).__name__,
                    "error": "Declared artifacts could not be collected; no workspace files were published.",
                }

    def _execution_ready(self, process):
        with self._lock:
            identity = self._by_pid.get(process.pid)
            if identity is None:
                return True
            backend, _, _ = self._executions[identity]
        return backend.wait_closed(0)

    def _settle_backend(self, identity):
        with self._lock:
            record = self._executions.get(identity)
        if record is None:
            return
        backend, done, activity = record
        backend.wait_closed(None)
        done.set()
        from homebrew_harness.storage.durable_runtime import settle_activity

        settle_activity(activity, backend)
        with self._lock:
            self._executions.pop(identity, None)

    def _wait_execution(self, process):
        with self._lock:
            identity = self._by_pid.get(process.pid)
        if identity is not None:
            self._settle_backend(identity)
        with self._lock:
            self._by_pid.pop(process.pid, None)
            self._artifact_results.pop(identity, None)

    def _extra_ready(self):
        return not self._executions

    def after_executions(self, callback):
        from homebrew_harness.storage.durable_runtime import (
            settle_activity,
            track_activity,
        )

        token = object()
        activity = track_activity(token)

        def finish():
            try:
                while True:
                    with self._lock:
                        ready = (
                            not self._processes
                            and not self._launching
                            and not self._executions
                        )
                    if ready:
                        break
                    threading.Event().wait(0.02)
                callback()
            finally:
                settle_activity(activity, token)

        self._defer(finish)

    def wait_execution(self, identity, timeout=None):
        with self._lock:
            record = self._executions.get(identity)
        return True if record is None else record[1].wait(timeout)


def make_shell_runner(policy=None, artifact_store=None):
    policy = policy or current_bash_policy()
    if policy.mode == "container":
        return ContainerShellRunner(policy, artifact_store)
    runner = ShellRunner()
    runner.bash_policy = HOST_POLICY
    return runner


def validate_bash_resources(policy, runner, background=None):
    from functools import partial

    from homebrew_harness.execution.bash_policy import BashPolicyError
    from homebrew_harness.tools.shell import run_bash

    if (
        getattr(runner, "bash_policy", HOST_POLICY).fingerprint()
        != policy.fingerprint()
    ):
        raise BashPolicyError(
            "The Bash runner does not match the selected host policy."
        )
    if background is not None:
        if (
            getattr(background.runner, "bash_policy", HOST_POLICY).fingerprint()
            != policy.fingerprint()
        ):
            raise BashPolicyError(
                "Background Bash must use the same host execution policy."
            )
        command = background.run_command
        if (
            policy.mode == "container"
            and command is not run_bash
            and not (isinstance(command, partial) and command.func is run_bash)
        ):
            raise BashPolicyError(
                "Container Bash cannot use a host extension which bypasses its runner."
            )
