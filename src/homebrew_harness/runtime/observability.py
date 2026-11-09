"""Small per-turn trace: identities and outcomes, never prompts or credentials."""

from __future__ import annotations

from homebrew_harness.runtime.output import print

import json
import os
import stat
import sys
import threading
import time
import weakref
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

_ACTIVE = ContextVar("harness_trace", default=None)
_LOCKS = weakref.WeakValueDictionary()
_LOCKS_GUARD = threading.Lock()


class Observations:
    """Best-effort host notifications never prove execution or shutdown."""

    def __init__(self):
        self._lock = threading.Lock()
        self._failures = {}

    def run(self, name, callback):
        try:
            return callback()
        except Exception as error:
            with self._lock:
                previous = self._failures.get(name, {}).get("dropped", 0)
                self._failures[name] = {
                    "dropped": previous + 1,
                    "error_type": type(error).__name__,
                }

    def snapshot(self):
        with self._lock:
            return {name: dict(value) for name, value in self._failures.items()}


class RuntimeTrace:
    def __init__(self, workdir: Path, *, max_bytes=1024 * 1024):
        if type(max_bytes) is not int or max_bytes < 256:
            raise ValueError("Trace file limit must be at least 256 bytes.")
        self.workdir = workdir.resolve()
        self.path = self.workdir / ".harness" / "runtime" / "events.jsonl"
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(str(self.path), threading.Lock())
        self.max_bytes = max_bytes
        self.dropped = 0
        self.error = None

    def status(self):
        return {
            "max_bytes_per_file": self.max_bytes,
            "retained_files": 2,
            "dropped": self.dropped,
            "error": self.error,
        }

    def emit(self, kind: str, **fields) -> None:
        try:
            from homebrew_harness.security.data_boundary import redact

            record = redact({"event": kind, "time": time.time(), **fields})
            data = (
                json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n"
            ).encode()
            if len(data) > self.max_bytes:
                raise ValueError("Trace event exceeds its limit.")
            from homebrew_harness.execution.execution_control import (
                acquired,
                operation_deadline,
            )

            with acquired(self._lock, operation_deadline(0.1), inherit=False):
                from homebrew_harness.execution.execution_control import cleanup_scope
                from homebrew_harness.security.safe_files import SafeFile

                with (
                    cleanup_scope(),
                    SafeFile(
                        self.workdir, str(self.path), create_parents=True
                    ) as target,
                ):
                    fd = os.open(
                        target.parts[-1],
                        os.O_WRONLY
                        | os.O_APPEND
                        | os.O_CREAT
                        | os.O_NOFOLLOW
                        | os.O_NONBLOCK,
                        0o600,
                        dir_fd=target.fds[-1],
                    )
                    try:
                        info = os.fstat(fd)
                        if not stat.S_ISREG(info.st_mode):
                            raise ValueError("Trace requires a regular file")
                        if info.st_size > self.max_bytes:
                            raise ValueError(
                                "Existing trace exceeds its limit; host rotation is required."
                            )
                        target.verify_binding()
                        if info.st_size + len(data) > self.max_bytes:
                            os.replace(
                                target.parts[-1],
                                "events.1.jsonl",
                                src_dir_fd=target.fds[-1],
                                dst_dir_fd=target.fds[-1],
                            )
                            os.close(fd)
                            fd = None
                            fd = os.open(
                                target.parts[-1],
                                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                0o600,
                                dir_fd=target.fds[-1],
                            )
                        with os.fdopen(fd, "wb", closefd=False) as handle:
                            handle.write(data)
                    finally:
                        if fd is not None:
                            os.close(fd)
        except Exception as error:
            self.dropped += 1
            self.error = type(error).__name__

    @contextmanager
    def scope(self, source: str, context_id: str):
        token = _ACTIVE.set((self, source, context_id))
        self.emit("turn_start", source=source, context_id=context_id)
        print(f"[runtime] {source}: {context_id}", file=sys.stderr)
        try:
            yield
        except BaseException as error:
            self.emit(
                "turn_error",
                source=source,
                context_id=context_id,
                error_type=type(error).__name__,
                cause_type=type(error.__cause__).__name__
                if error.__cause__ is not None
                else None,
            )
            raise
        else:
            self.emit("turn_end", source=source, context_id=context_id)
        finally:
            _ACTIVE.reset(token)


@contextmanager
def child_scope(source: str, context_id: str):
    current = _ACTIVE.get()
    if current is None:
        yield
    else:
        with current[0].scope(source, context_id):
            yield


def trace_tool(name: str, cwd: Path, call_id: str, output: str) -> str:
    current = _ACTIVE.get()
    if current is not None:
        trace, source, context_id = current
        try:
            value = json.loads(output)
            failed = isinstance(value, dict) and (
                "error" in value or value.get("isError", False)
            )
        except (TypeError, ValueError):
            failed = False
        try:
            location = str(cwd.resolve().relative_to(trace.workdir)) or "."
        except ValueError:
            location = "external"
        trace.emit(
            "tool_result",
            source=source,
            context_id=context_id,
            tool=name,
            call_id=call_id,
            cwd=location,
            output_chars=len(output),
            failed=bool(failed),
        )
    return output
