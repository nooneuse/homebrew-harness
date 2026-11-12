"""s11: explicit background commands, completion queues and process cleanup."""

from __future__ import annotations

import copy
import json
import os
import re
import secrets
import selectors
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from homebrew_harness.execution.execution_control import (
    check,
    cleanup_scope,
    operation_deadline,
)
from homebrew_harness.runtime.runtime_control import WorkAccepted, Workers
from homebrew_harness.security.data_boundary import redact
from homebrew_harness.storage.durable_runtime import DurableStore


def should_run_background(tool_name: str, arguments: dict) -> bool:
    return tool_name == "bash" and arguments.get("run_in_background") is True


class ShellRunner:
    """Track process groups; protect spawning against concurrent session close."""

    def __init__(self):
        self._lock = threading.RLock()
        self._processes: set[subprocess.Popen] = set()
        self._closed = False
        self._cancel = threading.Event()
        self._launching = 0
        self._cleanup_workers = Workers()
        self._deferred_cleanup = []

    @staticmethod
    def _stop(process: subprocess.Popen, deadline=None) -> None:
        if deadline is None:
            deadline = time.monotonic() + 0.25
        # Bash and its children share this new process group. Even a finished
        # shell may leave children, so do not stop at process.poll().
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                break
            except OSError:
                break
            if sig == signal.SIGTERM:
                time.sleep(
                    0.05
                    if deadline is None
                    else min(0.05, max(0, deadline - time.monotonic()))
                )
        try:
            process.wait(
                timeout=0.2
                if deadline is None
                else min(0.2, max(0, deadline - time.monotonic()))
            )
        except subprocess.TimeoutExpired:
            pass

    def run(
        self,
        command,
        workdir: Path,
        env: dict,
        timeout: float,
        *,
        output_bytes=8 * 1024 * 1024,
        cancel_events=(),
        on_started=None,
    ):
        deadline = operation_deadline(timeout)
        check(deadline, cancel_events)
        cancel_events = (*cancel_events, self._cancel)
        with self._lock:
            if self._closed:
                raise OSError("Shell runner is closed")
            self._launching += 1
        try:
            process = self._launch(command, workdir, env, deadline, cancel_events)
        except BaseException:
            with self._lock:
                self._launching -= 1
            raise
        with self._lock:
            self._launching -= 1
            self._processes.add(process)
            from homebrew_harness.storage.durable_runtime import (
                settle_process,
                track_process,
            )

            operation = track_process(process)
        result = None
        failure = None
        streams = {"stdout": bytearray(), "stderr": bytearray()}
        try:
            if on_started is not None:
                on_started(process)
            with selectors.DefaultSelector() as selector:
                for kind in streams:
                    pipe = getattr(process, kind)
                    os.set_blocking(pipe.fileno(), False)
                    selector.register(pipe, selectors.EVENT_READ, kind)
                while selector.get_map() or process.poll() is None:
                    if self._closed:
                        from homebrew_harness.execution.model_gateway import (
                            ModelCancelled,
                        )

                        raise ModelCancelled(
                            "Shell cancelled during session shutdown; inspect its effects."
                        )
                    # Local Shell timeout retains the established subprocess error contract.
                    if deadline.remaining() == 0:
                        raise subprocess.TimeoutExpired(process.args, timeout)
                    check(deadline, cancel_events)
                    remaining = deadline.remaining()
                    for key, _ in selector.select(
                        min(0.05, remaining) if remaining is not None else 0.05
                    ):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        else:
                            streams[key.data].extend(chunk)
                            if sum(map(len, streams.values())) > output_bytes:
                                result = subprocess.CompletedProcess(
                                    process.args,
                                    None,
                                    bytes(streams["stdout"][:output_bytes]).decode(
                                        "utf-8", "replace"
                                    ),
                                    bytes(
                                        streams["stderr"][
                                            : max(
                                                0,
                                                output_bytes
                                                - len(streams["stdout"][:output_bytes]),
                                            )
                                        ]
                                    ).decode("utf-8", "replace"),
                                )
                                result.output_limit = True
                                return result
            result = subprocess.CompletedProcess(
                process.args,
                process.returncode,
                bytes(streams["stdout"]).decode("utf-8", "replace"),
                bytes(streams["stderr"]).decode("utf-8", "replace"),
            )
            process.harness_capture_complete = True
            return result
        except BaseException as error:
            failure = error
            raise
        finally:
            cleanup_error = None
            try:
                self._stop(
                    process,
                    deadline.expires_at
                    if deadline.expires_at is not None
                    else time.monotonic() + 0.25,
                )
            except BaseException as error:
                cleanup_error = error
            for stream in (process.stdout, process.stderr):
                try:
                    stream.close()
                except BaseException as error:
                    cleanup_error = cleanup_error or error
            isolation = None
            try:
                isolation = self._finish(process)
            except BaseException as error:
                cleanup_error = cleanup_error or error
            if isolation is not None:
                if result is not None:
                    result.bash_execution = isolation
                if failure is not None:
                    failure.bash_execution = isolation

            def settle():
                if process.poll() is None:
                    self._stop(process)
                process.wait()
                self._wait_execution(process)
                with self._lock:
                    self._processes.discard(process)
                settle_process(operation, process)

            if process.poll() is None or not self._execution_ready(process):
                self._defer(settle)
            else:
                settle()
            if cleanup_error is not None:
                if failure is None:
                    raise cleanup_error
                failure.shell_cleanup_error = type(cleanup_error).__name__

    def _launch(self, command, workdir, env, deadline, cancel_events):
        with self._lock:
            if self._closed:
                raise OSError("Shell runner is closed")
            check(deadline, cancel_events)
            return subprocess.Popen(
                ["bash", "-c", command] if isinstance(command, str) else command,
                cwd=workdir,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )

    def _finish(self, process):
        return None

    def _execution_ready(self, process):
        return True

    def _wait_execution(self, process):
        return None

    def _extra_ready(self):
        return True

    def _defer(self, callback):
        try:
            self._cleanup_workers.start(callback, name="shell-cleanup")
        except WorkAccepted:
            pass
        except BaseException:
            with self._lock:
                self._deferred_cleanup.append(callback)

    def wait_closed(self, timeout=5):
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        while True:
            with self._lock:
                callbacks = self._deferred_cleanup
                self._deferred_cleanup = []
                ready = (
                    not self._processes and not self._launching and self._extra_ready()
                )
            for callback in callbacks:
                self._defer(callback)
            if ready and not callbacks and self._cleanup_workers.wait(0):
                return True
            remaining = 0.02 if deadline is None else deadline - time.monotonic()
            if remaining <= 0:
                return False
            threading.Event().wait(min(0.02, remaining))

    def close(self, timeout: float = 0.5) -> None:
        deadline = time.monotonic() + max(0, timeout)
        with self._lock:
            self._closed = True
            self._cancel.set()
            processes = list(self._processes)
        for process in processes:
            self._stop(process, deadline)


class BackgroundManager:
    """Session-local jobs. Completion collection never deletes queryable results."""

    def __init__(
        self,
        workdir: Path,
        run_command,
        *,
        max_running: int = 8,
        bash_policy=None,
        artifact_store=None,
    ):
        self.workdir = workdir.resolve()
        self.store = DurableStore(self.workdir)
        self.instance = __import__("uuid").uuid4().hex
        self._recovered = False
        self.run_command = run_command
        self.max_running = max_running
        from homebrew_harness.execution.bash_runner import make_shell_runner

        self.runner = make_shell_runner(bash_policy, artifact_store)
        self._lock = threading.Lock()
        self._tasks: dict[str, dict] = {}
        self._cancels = {}
        self._ready: list[str] = []
        self._workers = Workers()
        self._closed = False
        self.leases = None
        self.allow_async = True
        self.cleanup_errors = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def start(
        self,
        command: str,
        *,
        workdir: Path | None = None,
        assignment: dict | None = None,
        artifact_profile: str | None = None,
    ) -> dict:
        if not self.allow_async:
            raise ValueError(
                "This standalone loop has no event dispatcher; use synchronous Bash or HarnessSession for background work."
            )
        if not isinstance(command, str) or not command.strip():
            raise ValueError("Background command must be a nonempty string.")
        if artifact_profile is not None and not any(
            p.name == artifact_profile
            for p in getattr(getattr(self.runner, "bash_policy", None), "outputs", ())
        ):
            raise ValueError("Artifact output rule is not registered by the host.")
        cwd = (workdir or self.workdir).resolve()
        if not cwd.is_dir() or not cwd.is_relative_to(self.workdir):
            raise ValueError("Background cwd must remain inside its owning workspace.")
        with self._lock:
            if self._closed:
                raise ValueError("Background session is closed.")
            if (
                sum(task["status"] == "running" for task in self._tasks.values())
                >= self.max_running
            ):
                raise ValueError(
                    "Too many running background commands; collect results before starting more."
                )
            for _ in range(100):
                task_id = "bg_" + secrets.token_hex(4)
                if task_id not in self._tasks:
                    break
            else:
                raise ValueError("Could not allocate a unique background ID.")
            task = {
                "bg_id": task_id,
                "command": command,
                "status": "running",
                "result": None,
                "cwd": str(cwd),
                "assignment": copy.deepcopy(assignment),
                "artifact_profile": artifact_profile,
            }
            if self.leases is not None:
                self.leases.acquire("workspace:" + str(cwd))
            self._tasks[task_id] = task
            self._cancels[task_id] = threading.Event()
            try:
                self._persist(task_id, ready=False)
                self._workers.start(
                    self._run, task_id, command, cwd, name="background-" + task_id
                )
            except WorkAccepted:
                return {
                    "bg_id": task_id,
                    "status": "running",
                    "outcome": "unknown",
                    "accepted": True,
                    "error": "Launch was interrupted after acceptance. Inspect this background ID; do not start the command again.",
                }
            except BaseException:
                self._tasks.pop(task_id, None)
                self._cancels.pop(task_id, None)
                if self.leases is not None:
                    self.leases.release("workspace:" + str(cwd))
                # A publication may have succeeded before an fsync failed. Do
                # not leave a durable running record for work never launched.
                try:
                    self.store.remove("background", task_id)
                except Exception as error:
                    self.cleanup_errors.append(
                        {
                            "phase": "registration_rollback",
                            "error_type": type(error).__name__,
                        }
                    )
                raise
        return {
            "bg_id": task_id,
            "status": "running",
            "message": "Command accepted. Check by ID or collect its notification on a later turn.",
        }

    def _run(self, task_id: str, command: str, cwd: Path) -> None:
        try:
            from homebrew_harness.storage.artifact_store import artifact_context

            task = self._tasks[task_id]
            options = (
                {"artifact_profile": task["artifact_profile"]}
                if task.get("artifact_profile")
                else {}
            )
            from homebrew_harness.execution.execution_control import control_scope, operation_deadline
            with (artifact_context((task.get("assignment") or {}).get("task_id")),
                control_scope(operation_deadline(), cancel_events=(self._cancels[task_id],))):
                output = self.run_command(
                    command, workdir=cwd, runner=self.runner, **options
                )
            result = redact(json.loads(output))
            if not isinstance(result, dict):
                raise ValueError("Invalid command result")
            status = (
                "completed"
                if result.get("exit_code") == 0 and "error" not in result
                else "failed"
            )
        except Exception as error:
            # Exceptions may contain private environment or host details.
            result = {"error": f"Background command failed ({type(error).__name__})."}
            status = "failed"
        execution = result.get("execution", result.get("details", {}))
        identity = execution.get("id") if isinstance(execution, dict) else None
        if identity and hasattr(self.runner, "wait_execution"):
            self.runner.wait_execution(identity, None)
        if self.leases is not None:
            self.leases.release("workspace:" + str(cwd))
        with self._lock:
            task = self._tasks[task_id]
            if task["status"] == "cancelled":
                return
            if self._cancels.pop(task_id, threading.Event()).is_set():
                status = "cancelled"
            task.update(status=status, result=result)
            self._ready.append(task_id)
            try:
                with cleanup_scope():
                    self._persist(task_id, ready=True)
            except Exception as error:
                self.cleanup_errors.append(
                    {"phase": "result_persist", "error_type": type(error).__name__}
                )

    def _persist(self, task_id, *, ready):
        self._tasks[task_id]["revision"] = self._tasks[task_id].get("revision", 0) + 1
        record = copy.deepcopy(self._tasks[task_id])
        record.update(ready=ready, instance=self.instance)
        self.store.put("background", task_id, record)

    def recover(self):
        if self._recovered:
            return
        with self._lock:
            saved = self.store.items("background")
            for task_id, record in saved.items():
                if task_id in self._tasks:
                    continue
                task = {
                    k: v for k, v in record.items() if k not in {"instance", "ready"}
                }
                if task["status"] == "running":
                    task.update(
                        status="unknown",
                        result={
                            "error": "Previous host stopped during command execution; inspect its effects before continuing. No command was restarted."
                        },
                    )
                    record.update(task, ready=True)
                    self.store.put("background", task_id, record)
                self._tasks[task_id] = task
                if record.get("ready"):
                    self._ready.append(task_id)
            self._recovered = True

    def check(self, task_id: str, *, workdir: Path) -> str:
        if workdir.resolve() != self.workdir:
            raise ValueError(
                "Background manager belongs to a different working directory."
            )
        if not isinstance(task_id, str) or not re.fullmatch(r"bg_[0-9a-f]{8}", task_id):
            raise ValueError("Invalid background ID; use an ID returned by bash.")
        with self._lock:
            if task_id not in self._tasks:
                raise ValueError("Background task not found in this session.")
            snapshot = copy.deepcopy(self._tasks[task_id])
        return json.dumps(snapshot, ensure_ascii=False)

    def collect(self) -> list[dict]:
        with self._lock:
            results = [copy.deepcopy(self._tasks[task_id]) for task_id in self._ready]
            self._ready.clear()
        return results

    def inspect_unknown(self, identity, revision):
        with self._lock:
            task = self._tasks.get(identity)
            if (
                task is None
                or task["status"] != "unknown"
                or task.get("revision", 0) != revision
            ):
                raise ValueError(
                    "Background inspection is stale or work is still active."
                )
            task["inspected"] = True
            with cleanup_scope():
                self._persist(identity, ready=identity in self._ready)

    def acknowledge(self, results):
        for result in results:
            if self.store.get("background", result["bg_id"]) is not None:
                self.store.update_fields(
                    "background", result["bg_id"], {"ready": False}
                )

    def wait_closed(self, timeout=5):
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        if not self._workers.wait(
            None if deadline is None else max(0, deadline - time.monotonic())
        ):
            return False
        return self.runner.wait_closed(
            None if deadline is None else max(0, deadline - time.monotonic())
        )

    @property
    def _threads(self):
        with self._workers.condition:
            return tuple(self._workers.threads)

    def has_ready(self) -> bool:
        with self._lock:
            return bool(self._ready)

    def restore(self, results: list[dict]) -> None:
        with self._lock:
            for result in results:
                task_id = result["bg_id"]
                if task_id in self._tasks and task_id not in self._ready:
                    self._ready.append(task_id)

    def running_paths(self) -> list[Path]:
        with self._lock:
            return [
                Path(task["cwd"])
                for task in self._tasks.values()
                if task["status"] == "running"
            ]

    def task_running(self, task_id: str) -> bool:
        with self._lock:
            return any(
                task["status"] == "running"
                and (task["assignment"] or {}).get("task_id") == task_id
                for task in self._tasks.values()
            )

    def stop(self, task_id):
        with self._lock:
            task = self._tasks.get(task_id)
            event = self._cancels.get(task_id)
            if task is None or event is None or task["status"] != "running":
                raise ValueError("Background task is no longer active in this Session.")
            task["stop_requested"] = True
            with cleanup_scope():
                self._persist(task_id, ready=False)
            event.set()
            return {"id": task_id, "state": "stop_requested"}

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(list(self._tasks.values()))

    def close(self, timeout: float = 0.5) -> None:
        deadline = time.monotonic() + max(0, timeout)
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for task_id, task in self._tasks.items():
                if task["status"] == "running":
                    task.update(
                        status="cancelled",
                        result={"error": "Session ended before the command completed."},
                    )
                    self._ready.append(task_id)
                    try:
                        with cleanup_scope():
                            self._persist(task_id, ready=True)
                    except Exception as error:
                        self.cleanup_errors.append(
                            {
                                "phase": "close_persist",
                                "error_type": type(error).__name__,
                            }
                        )
            threads = list(self._threads)
        # Set the runner closed before killing; late workers cannot start new
        # processes. Joins share one short budget instead of waiting per job.
        self.runner.close(timeout=max(0, deadline - time.monotonic()))
        for thread in threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))


@contextmanager
def termination_guard():
    """CLI-only SIGTERM handling; importing the module changes no host signals."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.getsignal(signal.SIGTERM)

    def terminate(signum, _frame):
        signal.signal(signal.SIGTERM, previous)
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)
