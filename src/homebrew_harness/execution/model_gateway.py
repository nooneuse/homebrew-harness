"""Shared model usage, concurrency control and absolute caller deadlines."""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import hashlib
import math
import os
import queue
import re
import stat
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path

import httpx

from homebrew_harness.execution.model_errors import quota_exhausted
from homebrew_harness.runtime.runtime_control import WorkAccepted, Workers
from homebrew_harness.security.data_boundary import redact, register_private_values
from homebrew_harness.security.safe_files import SafeFile


class ModelGatewayError(RuntimeError):
    retryable = False


class ModelDeadlineExceeded(ModelGatewayError):
    def __init__(
        self,
        message,
        *,
        source="caller",
        phase=None,
        request_id=None,
        sent=False,
        settled=None,
    ):
        super().__init__(message)
        self.source, self.phase = source, phase
        self.request_id, self.sent, self.settled = request_id, sent, settled
        self.reason_code = source + "_timeout"
        self.kind = "timeout" if source == "request" else self.reason_code
        self.transient = source == "request"


class ModelCancelled(ModelGatewayError):
    pass


@dataclass(frozen=True)
class GatewayLimits:
    concurrency: int = 4
    request_timeout: float = 60
    session_timeout: float | None = None
    max_failures: int = 3

    def __post_init__(self):
        for name in ("concurrency", "max_failures"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError("Gateway " + name + " must be a positive integer.")
        for value in (self.request_timeout, self.session_timeout):
            if value is not None and (
                isinstance(value, bool) or not math.isfinite(value) or value <= 0
            ):
                raise ValueError("Gateway time limits must be finite positive seconds.")


@dataclass(frozen=True)
class Deadline:
    expires_at: float | None = None
    source: str = field(default="caller", compare=False)

    def __post_init__(self):
        if self.source not in {
            "caller",
            "request",
            "episode",
            "run",
            "session",
            "tool",
            "cleanup",
        }:
            raise ValueError("Invalid deadline source.")
        if self.expires_at is not None and (
            isinstance(self.expires_at, bool) or not math.isfinite(self.expires_at)
        ):
            raise ValueError("Deadline must use a finite monotonic timestamp.")

    @classmethod
    def after(cls, seconds: float | None, *, source="caller"):
        if seconds is not None and (
            isinstance(seconds, bool) or not math.isfinite(seconds) or seconds < 0
        ):
            raise ValueError("Deadline duration must be finite and nonnegative.")
        return cls(None if seconds is None else time.monotonic() + seconds, source)

    def remaining(self):
        return (
            None
            if self.expires_at is None
            else max(0, self.expires_at - time.monotonic())
        )

    def intersect(self, other):
        values = [value for value in (self, other) if value.expires_at is not None]
        priorities = {
            name: n
            for n, name in enumerate(
                ("run", "session", "episode", "caller", "request", "tool", "cleanup")
            )
        }
        return (
            min(values, key=lambda value: (value.expires_at, priorities[value.source]))
            if values
            else Deadline()
        )


@dataclass(frozen=True)
class RequestScope:
    gateway: object = None
    role: str = "lead"
    context_id: str = ""
    parent_id: str = ""
    task_id: str = ""
    deadline: Deadline = field(default_factory=Deadline)
    cancel_events: tuple = ()


_SCOPE = ContextVar("model_request_scope", default=RequestScope())
_CLIENT_GUARD = threading.RLock()


def _number(value):
    return value if type(value) is int and value >= 0 else None


def _field(value, name):
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def normalize_usage(response):
    from homebrew_harness.execution.model_protocol import ModelResponse
    from homebrew_harness.execution.providers.chat_completions import usage_from_chat

    return (
        response.usage.data()
        if isinstance(response, ModelResponse)
        else usage_from_chat(response).data()
    )


class UsageLedger:
    """Optional bounded observations; never a prerequisite for model execution."""

    def __init__(
        self, workdir=None, session_id=None, *, enabled=True, max_bytes=8 * 1024 * 1024
    ):
        self.workdir = Path(workdir).resolve() if workdir is not None else None
        self.session_id = session_id or uuid.uuid4().hex
        self.reference = ".harness/usage/" + self.session_id + ".jsonl"
        self.enabled, self.max_bytes = enabled, max_bytes
        self.records = deque(maxlen=128)
        self.lock = threading.RLock()
        self.queue = queue.Queue(maxsize=256)
        self.workers = Workers()
        self.started = False
        self.closed = False
        self.error = None
        self.dropped = 0

    def append(self, record):
        safe = redact({"session_id": self.session_id, "time": time.time(), **record})
        if not self.enabled or self.workdir is None:
            return
        with self.lock:
            if self.closed:
                self.dropped += 1
                return
            if not self.started:
                self.started = True
                try:
                    self.workers.start(
                        self._write_loop, name="usage-observer", track_operation=False
                    )
                except WorkAccepted:
                    self.dropped += 1
                    return
                except BaseException as error:
                    self.started = False
                    self.error = type(error).__name__
                    self.dropped += 1
                    return
            try:
                self.queue.put_nowait(safe)
            except queue.Full:
                self.dropped += 1
                self.error = "observation_queue_full"

    def _write_loop(self):
        while not self.closed or not self.queue.empty():
            try:
                record = self.queue.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                from homebrew_harness.execution.execution_control import cleanup_scope

                with (
                    cleanup_scope(),
                    SafeFile(
                        self.workdir, self.reference, create_parents=True
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
                        if not stat.S_ISREG(os.fstat(fd).st_mode):
                            raise ValueError("Observation requires a regular file")
                        data = (
                            json.dumps(record, ensure_ascii=False, allow_nan=False)
                            + "\n"
                        ).encode()
                        if os.fstat(fd).st_size + len(data) > self.max_bytes:
                            raise ValueError("Observation file limit reached")
                        target.verify_binding()
                        os.fchmod(fd, 0o600)
                        with os.fdopen(fd, "wb", closefd=False) as stream:
                            stream.write(data)
                            stream.flush()
                        self.records.append(record)
                    finally:
                        os.close(fd)
            except Exception as error:
                self.error = type(error).__name__
                self.dropped += 1
            finally:
                self.queue.task_done()

    def flush(self, timeout=1):
        end = time.monotonic() + max(0, timeout)
        with self.queue.all_tasks_done:
            while self.queue.unfinished_tasks:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return False
                self.queue.all_tasks_done.wait(remaining)
        return True

    def close(self, timeout=0):
        self.closed = True
        return self.workers.wait(timeout)


@dataclass
class Receipt:
    id: str
    metadata: dict
    started: float
    deadline: Deadline
    cancel_events: tuple
    cancel: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    response: object = None
    usage: object = None
    error: BaseException | None = None
    abandoned: str = ""
    sent: bool = False
    registered: bool = False
    failure_counted: bool = False


class GatewayState:
    def __init__(self, limits, ledger):
        self.limits, self.ledger = limits, ledger
        self.deadline = Deadline.after(limits.session_timeout, source="session")
        self.session_deadline_at = (
            None
            if limits.session_timeout is None
            else time.time() + limits.session_timeout
        )
        self.condition = threading.Condition(threading.RLock())
        self.inflight = {}
        self.attempts = self.calls = 0
        self.failures = 0
        self.closed = False
        self.blocked = ""
        self.connection_faults = {}
        self.finished = deque(maxlen=128)
        self.sequence = 0
        self.new_attempts = 0
        self.known_tokens = self.unknown_calls = self.late_calls = 0
        self.purposes, self.roles = {}, {}
        self.observation_incomplete = False
        self.observation_error = None
        self.worker_output_tokens = int(
            os.getenv("HARNESS_WORKER_OUTPUT_TOKENS", "").strip() or "4096"
        )
        if not 128 <= self.worker_output_tokens <= 32768:
            raise ValueError(
                "HARNESS_WORKER_OUTPUT_TOKENS must be between 128 and 32768."
            )

    def check(self, deadline=Deadline(), cancel_events=(), connection=None):
        if self.closed or any(event.is_set() for event in cancel_events):
            raise ModelCancelled(
                "Model operation cancelled; no further model actions are accepted."
            )
        if deadline.remaining() == 0 or self.deadline.remaining() == 0:
            raise ModelDeadlineExceeded(
                "Model operation deadline reached; unfinished work is retained.",
                source=deadline.intersect(self.deadline).source,
            )
        blocked = self.blocked or self.connection_faults.get(connection, {}).get(
            "blocked", ""
        )
        if blocked:
            raise ModelGatewayError(
                "Model gateway stopped: "
                + blocked
                + ". Resume explicitly after resolving it."
            )

    def acquire(self, metadata, deadline, cancel_events, *, wait=True):
        with self.condition:
            while True:
                self.check(deadline, cancel_events, metadata.get("connection"))
                if len(self.inflight) < self.limits.concurrency:
                    receipt = Receipt(
                        uuid.uuid4().hex,
                        metadata,
                        time.monotonic(),
                        deadline,
                        cancel_events,
                    )
                    receipt.cancel_events = (*receipt.cancel_events, receipt.cancel)
                    self.inflight[receipt.id] = receipt
                    return receipt
                if not wait:
                    return None
                remaining = deadline.remaining()
                self.condition.wait(
                    min(0.05, remaining) if remaining is not None else 0.05
                )

    def record(self, event):
        try:
            self.ledger.append(event)
        except Exception as error:
            self.observation_incomplete = True
            self.observation_error = type(error).__name__

    @staticmethod
    def add_group(table, key, tokens, unknown, call=True):
        if key not in table and len(table) >= 128:
            key = "other"
        entry = table.setdefault(
            key, {"calls": 0, "known_tokens": 0, "unknown_usage_calls": 0}
        )
        entry["calls"] += int(call)
        entry["known_tokens"] += tokens or 0
        entry["unknown_usage_calls"] += int(unknown)

    def observation(self):
        return {
            "format": 2,
            "session_id": self.ledger.session_id,
            "session_deadline_at": self.session_deadline_at,
            "sequence": self.sequence,
            "attempted_calls": self.attempts,
            "calls": self.calls,
            "known_tokens": self.known_tokens,
            "unknown_usage_calls": self.unknown_calls,
            "late_calls": self.late_calls,
            "by_purpose": copy.deepcopy(self.purposes),
            "by_role": copy.deepcopy(self.roles),
            "pending_requests": [
                {
                    "request_id": r.id,
                    "sent": r.sent,
                    "role": r.metadata["role"],
                    "purpose": r.metadata["purpose"],
                }
                for r in self.inflight.values()
            ],
            "blocked": self.blocked,
            "consecutive_failures": self.failures,
            "connection_faults": copy.deepcopy(self.connection_faults),
        }

    def publish_observation(self):
        with self.condition:
            record = self.observation()
        self.record({"event": "usage_snapshot", "observation": record})

    def finish(self, receipt):
        try:
            usage = (
                receipt.usage.data()
                if receipt.usage is not None
                else normalize_usage(None)
            )
        except Exception:
            usage = normalize_usage(None)
        known = usage["total_tokens"] is not None
        with self.condition:
            fault = self.connection_faults.setdefault(
                receipt.metadata.get("connection"), {"failures": 0, "blocked": ""}
            )
            if receipt.error is not None:
                status = _number(getattr(receipt.error, "status_code", None)) or 0
                transient = getattr(receipt.error, "transient", False)
                if (
                    not self.closed
                    and receipt.abandoned != "ModelCancelled"
                    and not receipt.failure_counted
                ):
                    if transient and receipt.sent:
                        fault["failures"] += 1
                        receipt.failure_counted = True
                    self.failures = fault["failures"]
                if quota_exhausted(receipt.error):
                    fault["blocked"] = "provider quota unavailable"
                elif status in {401, 403}:
                    fault["blocked"] = "provider authentication or access denied"
                elif fault["failures"] >= self.limits.max_failures:
                    fault["blocked"] = "consecutive request failures"
            elif not receipt.abandoned and not self.closed:
                self.failures = 0
                fault["failures"] = 0
            event = {
                "event": "request_finished",
                "request_id": receipt.id,
                **receipt.metadata,
                "status": "failed" if receipt.error else "completed",
                "sent": receipt.sent,
                "late": bool(receipt.abandoned),
                "abandoned": receipt.abandoned,
                "elapsed_seconds": max(0, time.monotonic() - receipt.started),
                "usage": usage,
                "unknown_usage": receipt.sent and not known,
                "blocked": self.blocked,
                "consecutive_failures": self.failures,
                "error_type": type(receipt.error).__name__ if receipt.error else None,
            }
            if receipt.registered:
                self.finished.append(event)
            if receipt.sent:
                self.known_tokens += usage["total_tokens"] or 0
                self.unknown_calls += int(not known)
                self.late_calls += int(bool(receipt.abandoned))
                self.add_group(
                    self.purposes,
                    receipt.metadata["purpose"],
                    usage["total_tokens"],
                    not known,
                )
                self.add_group(
                    self.roles,
                    receipt.metadata["role"],
                    usage["total_tokens"],
                    not known,
                )
            self.inflight.pop(receipt.id, None)
            self.sequence += 1
            receipt.done.set()
            self.condition.notify_all()
        self.publish_observation()


class ModelGateway:
    def __init__(
        self,
        client,
        limits=None,
        *,
        workdir=None,
        ledger=None,
        _state=None,
        _timeout=None,
    ):
        self._client = client._client if isinstance(client, ModelGateway) else client
        self._timeout = (
            _timeout
            if _timeout is not None
            else client._timeout
            if isinstance(client, ModelGateway)
            else None
        )
        self._state = _state or GatewayState(
            limits or GatewayLimits(), ledger or UsageLedger(workdir, enabled=False)
        )
        from homebrew_harness.execution.providers import adapter_for

        self._adapter = adapter_for(self._client)
        self._transport_client = None
        self._transport_lock = threading.Lock()
        self._prepared_lock = threading.Lock()
        self._prepared = None
        self._threads = Workers()

    def __getattr__(self, name):
        if name in {"api_key", "base_url", "timeout"}:
            return getattr(self._client, name, None)
        raise AttributeError(name)

    def _connection_key(self):
        raw = getattr(self._adapter, "client", self._client)
        identity = self._adapter.base_url or getattr(
            raw, "_harness_model_identity", id(raw)
        )
        return (
            self._adapter.connection_id
            or hashlib.sha256(
                (self._adapter.protocol + str(identity)).encode()
            ).hexdigest()
        )

    def bind(self, client):
        if isinstance(client, ModelGateway) and client._state is self._state:
            return client
        return ModelGateway(
            client._adapter if isinstance(client, ModelGateway) else client,
            _state=self._state,
        )

    def with_options(self, **options):
        if options.get("max_retries", 0) != 0:
            raise ValueError(
                "SDK retries are disabled; every gateway attempt must be accounted for."
            )
        options["max_retries"] = 0
        raw = self._adapter.with_options(**options)
        return ModelGateway(
            raw, _state=self._state, _timeout=options.get("timeout", self._timeout)
        )

    def _transport(self):
        with self._transport_lock:
            if self._transport_client is None:
                self._transport_client = self._adapter.with_options(max_retries=0)
            return self._transport_client

    @contextmanager
    def scope(
        self,
        *,
        role=None,
        context_id=None,
        task_id=None,
        deadline=None,
        cancel_event=None,
        inherit_deadline=True,
    ):
        parent = _SCOPE.get()
        scope = RequestScope(
            self,
            role or parent.role,
            context_id or parent.context_id,
            parent.context_id
            if context_id and context_id != parent.context_id
            else parent.parent_id,
            task_id or parent.task_id,
            (parent.deadline if inherit_deadline else Deadline()).intersect(
                deadline or Deadline()
            ),
            parent.cancel_events
            + ((cancel_event,) if cancel_event is not None else ()),
        )
        token = _SCOPE.set(scope)
        try:
            yield scope
        finally:
            _SCOPE.reset(token)

    def operation_deadline(self):
        return self._state.deadline.intersect(_SCOPE.get().deadline)

    def checkpoint(self):
        scope = _SCOPE.get()
        self._state.check(scope.deadline, scope.cancel_events, self._connection_key())

    @property
    def worker_output_tokens(self):
        return self._state.worker_output_tokens

    def configure_worker_output(self, value):
        if type(value) is not int or not 128 <= value <= 32768:
            raise ValueError("Worker output tokens must be between 128 and 32768.")
        if self._state.attempts or self._state.inflight:
            raise ValueError("Configure worker output before starting model work.")
        self._state.worker_output_tokens = value

    def inflight_for(self, task_id):
        with self._state.condition:
            return any(
                receipt.metadata.get("task_id") == task_id
                for receipt in self._state.inflight.values()
            )

    def inflight_context(self, context_id):
        with self._state.condition:
            return sum(
                r.metadata.get("context_id") == context_id
                for r in self._state.inflight.values()
            )

    @property
    def inflight(self):
        with self._state.condition:
            return len(self._state.inflight)

    def active_requests(self):
        """Public operation metadata only, with no request payload or connection values."""
        with self._state.condition:
            return [{"id": "model_" + r.id, "kind": "model", "state": "cleanup_pending" if r.abandoned else "stopping" if r.cancel.is_set() else "running",
                "elapsed": max(0, time.monotonic() - r.started), "stoppable": not r.abandoned,
                **{key: r.metadata.get(key) for key in ("role", "purpose", "context_id", "task_id", "model")}}
                for r in self._state.inflight.values()]

    def stop_request(self, identity):
        with self._state.condition:
            receipt = self._state.inflight.get(identity.removeprefix("model_"))
            if receipt is None or not identity.startswith("model_"):
                raise ValueError("Model request is no longer active.")
            receipt.cancel.set()
            self._state.condition.notify_all()
            return {"id": identity, "state": "stop_requested"}

    def inflight_cancel(self, cancel_event):
        """Observe requests belonging to a particular live cancellation scope."""
        with self._state.condition:
            return cancel_event is not None and any(
                cancel_event in receipt.cancel_events
                for receipt in self._state.inflight.values()
            )

    @property
    def blocked(self):
        return (
            self._state.blocked
            or self._state.connection_faults.get(self._connection_key(), {}).get(
                "blocked", ""
            )
            or (
                "session deadline reached"
                if self._state.deadline.remaining() == 0
                else ""
            )
        )

    def acknowledge_faults(self, expected):
        """Host resume acknowledges saved connection errors without resetting usage."""
        with self._state.condition:
            if self._state.inflight:
                raise ModelGatewayError(
                    "Wait for outstanding model execution before resume."
                )
            for key, fault in expected.items():
                if self._state.connection_faults.get(key) != fault:
                    raise ModelGatewayError(
                        "Connection fault changed before resume confirmation."
                    )
            if expected:
                for key in expected:
                    self._state.connection_faults.pop(key, None)
                self._state.failures = max(
                    (
                        f.get("failures", 0)
                        for f in self._state.connection_faults.values()
                    ),
                    default=0,
                )
                self._state.sequence += 1
                self._state.condition.notify_all()
        self._state.publish_observation()

    def restore_acknowledged_faults(self, faults):
        with self._state.condition:
            if self._state.inflight:
                raise ModelGatewayError(
                    "Cannot restore faults during active execution."
                )
            self._state.connection_faults.update(copy.deepcopy(faults))
            self._state.failures = max(
                (f.get("failures", 0) for f in self._state.connection_faults.values()),
                default=0,
            )
            self._state.sequence += 1
        self._state.publish_observation()

    def reset_failures(self):
        with self._state.condition:
            self._state.connection_faults.clear()
            if self._state.failures or self._state.blocked in {
                "provider quota unavailable",
                "provider authentication or access denied",
                "consecutive request failures",
            }:
                self._state.sequence += 1
            self._state.failures = 0
            if self._state.blocked in {
                "provider quota unavailable",
                "provider authentication or access denied",
                "consecutive request failures",
            }:
                self._state.blocked = ""
            self._state.condition.notify_all()
        self._state.publish_observation()

    def close(self):
        with self._state.condition:
            self._state.closed = True
            self._state.condition.notify_all()
        self._state.ledger.close(0)

    def wait_idle(self, timeout=None):
        deadline = Deadline.after(timeout)
        with self._state.condition:
            while self._state.inflight:
                remaining = deadline.remaining()
                if remaining == 0:
                    return False
                self._state.condition.wait(remaining)
        return True

    def restore_usage(self, usage):
        """Core counters survive observation loss; no model requests are replayed."""
        with self._state.condition:
            if self._state.new_attempts or self._state.inflight:
                raise ModelGatewayError("Usage restore requires an unused gateway.")
            if not isinstance(usage, dict) or usage and usage.get("format") != 2:
                raise ModelGatewayError(
                    "Unsupported prerelease usage snapshot; start a new session."
                )
            latest = copy.deepcopy(usage) if usage else self._state.observation()
            reference = usage.get("ledger")
            incomplete = usage.get("observation_incomplete", False)
            if reference and self._state.ledger.workdir is not None:
                if not isinstance(reference, str) or not re.fullmatch(
                    r"\.harness/usage/[0-9a-f]{32}\.jsonl", reference
                ):
                    raise ModelGatewayError("Invalid saved observation reference.")
                try:
                    with (
                        SafeFile(self._state.ledger.workdir, reference) as target,
                        target.open_read() as stream,
                    ):
                        raw = stream.read(8 * 1024 * 1024 + 1)
                        target.verify_binding()
                    if len(raw) > 8 * 1024 * 1024:
                        raise ValueError("Observation exceeds read limit")
                    for line in raw.splitlines():
                        try:
                            event = json.loads(line)
                            candidate = event.get("observation", {})
                            if (
                                event.get("event") == "usage_snapshot"
                                and candidate.get("format") == 2
                                and candidate.get("session_id")
                                == usage.get("session_id")
                                and type(candidate.get("sequence")) is int
                                and candidate["sequence"] > latest.get("sequence", 0)
                            ):
                                numeric = (
                                    "sequence",
                                    "attempted_calls",
                                    "calls",
                                    "known_tokens",
                                    "unknown_usage_calls",
                                    "late_calls",
                                    "consecutive_failures",
                                )
                                if any(
                                    _number(candidate.get(key)) is None
                                    for key in numeric
                                ):
                                    raise ValueError("Invalid observed counters")
                                if not isinstance(
                                    candidate.get("blocked"), str
                                ) or not isinstance(
                                    candidate.get("pending_requests"), list
                                ):
                                    raise ValueError("Invalid observed state")
                                for key in ("by_purpose", "by_role"):
                                    if (
                                        not isinstance(candidate.get(key), dict)
                                        or len(candidate[key]) > 129
                                    ):
                                        raise ValueError("Invalid observed groups")
                                    for group in candidate[key].values():
                                        if not isinstance(group, dict) or any(
                                            _number(group.get(k)) is None
                                            for k in (
                                                "calls",
                                                "known_tokens",
                                                "unknown_usage_calls",
                                            )
                                        ):
                                            raise ValueError("Invalid observed group")
                                for pending in candidate["pending_requests"]:
                                    if (
                                        not isinstance(pending, dict)
                                        or type(pending.get("sent")) is not bool
                                        or any(
                                            not isinstance(pending.get(k), str)
                                            for k in ("request_id", "purpose", "role")
                                        )
                                    ):
                                        raise ValueError("Invalid pending observation")
                                latest = candidate
                        except (ValueError, TypeError, AttributeError):
                            incomplete = True
                except (OSError, ValueError):
                    incomplete = True
            for field in (
                "sequence",
                "attempted_calls",
                "calls",
                "known_tokens",
                "unknown_usage_calls",
                "late_calls",
                "consecutive_failures",
            ):
                if _number(latest.get(field, 0)) is None:
                    raise ModelGatewayError("Invalid core usage counters.")
            for field in ("by_purpose", "by_role"):
                if (
                    not isinstance(latest.get(field, {}), dict)
                    or len(latest.get(field, {})) > 129
                ):
                    raise ModelGatewayError("Invalid usage groups.")
                for key, item in latest.get(field, {}).items():
                    if (
                        not isinstance(key, str)
                        or not isinstance(item, dict)
                        or any(
                            _number(item.get(k)) is None
                            for k in ("calls", "known_tokens", "unknown_usage_calls")
                        )
                    ):
                        raise ModelGatewayError("Invalid usage group counters.")
            state = self._state
            wall = latest.get("session_deadline_at")
            if wall is not None:
                if type(wall) not in {int, float} or not math.isfinite(wall):
                    raise ModelGatewayError("Invalid saved session deadline.")
                state.session_deadline_at = (
                    min(wall, state.session_deadline_at)
                    if state.session_deadline_at is not None
                    else wall
                )
                state.deadline = state.deadline.intersect(
                    Deadline.after(max(0, wall - time.time()), source="session")
                )
            state.sequence = latest.get("sequence", 0)
            state.attempts = latest.get("attempted_calls", 0)
            state.calls = latest.get("calls", 0)
            state.known_tokens = latest.get("known_tokens", 0)
            state.unknown_calls = latest.get("unknown_usage_calls", 0)
            state.late_calls = latest.get("late_calls", 0)
            state.purposes = copy.deepcopy(latest.get("by_purpose", {}))
            state.roles = copy.deepcopy(latest.get("by_role", {}))
            state.connection_faults = copy.deepcopy(latest.get("connection_faults", {}))
            state.blocked = "" if state.connection_faults else latest.get("blocked", "")
            state.failures = latest.get("consecutive_failures", 0)
            if not isinstance(state.blocked, str):
                raise ModelGatewayError("Invalid provider state.")
            for pending in latest.get("pending_requests", []):
                if (
                    not isinstance(pending, dict)
                    or type(pending.get("sent")) is not bool
                ):
                    raise ModelGatewayError("Invalid pending observation.")
                if pending["sent"]:
                    state.unknown_calls += 1
                    state.add_group(state.purposes, pending["purpose"], None, True)
                    state.add_group(state.roles, pending["role"], None, True)
            if latest.get("pending_requests"):
                state.sequence += 1
                incomplete = True
            state.observation_incomplete = incomplete
            if reference and incomplete:
                state.observation_error = "saved_observation_incomplete"
            if usage.get("wall_deadline") is not None:
                state.deadline = state.deadline.intersect(
                    Deadline.after(max(0, usage["wall_deadline"] - time.time()))
                )
        self._state.publish_observation()

    def mark_observation_incomplete(self):
        with self._state.condition:
            self._state.observation_incomplete = True

    def summary(self):
        with self._state.condition:
            state = self._state
            return {
                **state.observation(),
                "blocked": self.blocked,
                "inflight": len(state.inflight),
                "pending_usage_calls": sum(
                    int(r.sent) for r in state.inflight.values()
                ),
                "observation_incomplete": state.observation_incomplete
                or bool(state.ledger.error or state.ledger.dropped),
                "observation_error": state.observation_error or state.ledger.error,
                "details_enabled": state.ledger.enabled,
                "details_dropped": state.ledger.dropped,
                "limits": {
                    "concurrency": state.limits.concurrency,
                    "request_timeout": state.limits.request_timeout,
                    "session_timeout": state.limits.session_timeout,
                },
                "ledger": state.ledger.reference
                if state.ledger.enabled and state.ledger.workdir is not None
                else None,
                "wall_deadline": None
                if state.deadline.expires_at is None
                else time.time() + state.deadline.remaining(),
            }

    def _prepare_request(self, purpose, role, task_id, deadline, cancel_event, kwargs):
        request_started = time.monotonic()
        if set(kwargs) - {
            "model",
            "messages",
            "tools",
            "max_tokens",
            "max_completion_tokens",
            "timeout",
            "protocol_root",
            "output_contract",
            "response_format",
            "temperature",
            "top_p",
            "stream",
            "reasoning_effort",
            "extra_body",
        }:
            raise ValueError("Unsupported model request parameters.")
        register_private_values(
            getattr(self._client, "api_key", None),
            str(getattr(self._client, "base_url", "")),
        )
        register_private_values(
            *(
                os.getenv(name)
                for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "DASHSCOPE_API_KEY")
            )
        )
        params = copy.deepcopy(kwargs)
        for name in ("messages", "tools", "response_format"):
            if name in params:
                params[name] = redact(params[name])
        output_limit = params.get(
            "max_completion_tokens", params.get("max_tokens", 4096)
        )
        if type(output_limit) is not int or output_limit < 1:
            raise ValueError("Model output limit must be a positive integer.")
        if "max_tokens" not in params and "max_completion_tokens" not in params:
            params["max_tokens"] = output_limit
        scope = _SCOPE.get()
        configured_timeout = params.pop("timeout", self._timeout)
        if configured_timeout is None:
            configured_timeout = getattr(self._client, "timeout", None)
        timeout = min(
            self._state.limits.request_timeout,
            _timeout_seconds(configured_timeout, self._client),
        )
        params["timeout"] = configured_timeout
        expires = (
            Deadline(request_started + timeout, source="request")
            .intersect(self._state.deadline)
            .intersect(scope.deadline)
            .intersect(deadline or Deadline())
        )
        cancels = scope.cancel_events + (
            (cancel_event,) if cancel_event is not None else ()
        )
        metadata = {
            "role": role or scope.role,
            "purpose": purpose,
            "context_id": scope.context_id,
            "parent_id": scope.parent_id,
            "task_id": task_id or scope.task_id,
            "model": str(params.get("model", "")),
            "output_limit": output_limit,
        }
        from homebrew_harness.execution.model_protocol import ModelRequest
        from homebrew_harness.storage.durable_runtime import active_writer

        writer = active_writer()
        context_key = (
            writer.key
            if writer
            else self._state.ledger.session_id + ":" + scope.context_id
        )
        contract = params.pop("output_contract", None)
        response_format = params.pop("response_format", None)
        if contract is None and response_format:
            contract = response_format
        options = {}
        for key in ("temperature", "top_p", "stream", "reasoning_effort", "extra_body"):
            if key in params:
                options[key] = params.pop(key)
        request = ModelRequest(
            str(params["model"]),
            tuple(params.get("messages", [])),
            tuple(params.get("tools", [])),
            output_limit,
            contract,
            options,
            context_key,
            writer.store.workdir
            if writer
            else params.pop("protocol_root", self._state.ledger.workdir),
        )
        transport = self._transport()
        identity = json.dumps(
            {
                "model": request.model,
                "messages": request.messages,
                "tools": request.tools,
                "output": request.max_output_tokens,
                "contract": request.output_contract,
                "options": request.options,
                "owner": request.context_key,
                "root": str(request.state_root),
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        with self._prepared_lock:
            if self._prepared is not None and self._prepared[0] == identity:
                from homebrew_harness.execution.providers.common import (
                    messages as validate_replay,
                )

                if any(m.get("_protocol_state") for m in request.messages):
                    validate_replay(request, transport)
                prepared = self._prepared[1]
            else:
                prepared = transport.prepare(request)
                self._prepared = (identity, prepared)
        metadata["connection"] = self._connection_key()
        metadata["protocol"] = self._adapter.protocol
        if expires.remaining() == 0:
            raise ModelDeadlineExceeded(
                "Model preparation exceeded its deadline.",
                source=expires.source,
                phase="prepare",
                settled=True,
            )
        return (request, prepared, configured_timeout), metadata, expires, cancels

    def input_size(
        self,
        model,
        messages,
        tools,
        root,
        output_tokens=None,
        *,
        output_contract=None,
        settings=None,
    ):
        params, _, _, _ = self._prepare_request(
            "context_prepare",
            None,
            None,
            None,
            None,
            {
                **(settings or {}),
                "model": model,
                "messages": messages,
                "tools": tools or [],
                "max_tokens": output_tokens or self.worker_output_tokens,
                "protocol_root": root,
                **(
                    {"output_contract": output_contract}
                    if output_contract is not None
                    else {}
                ),
            },
        )
        return len(json.dumps(params[1], ensure_ascii=False))

    def _launch_request(self, receipt, params):
        try:
            self._threads.start(
                self._invoke, receipt, params, name="model-" + receipt.id[:8]
            )
        except WorkAccepted as error:
            receipt.abandoned = "CallerInterrupted"
            raise ModelGatewayError(
                "Model launch interrupted after acceptance; its request remains pending."
            ) from error
        except BaseException as error:
            receipt.error = error
            self._state.finish(receipt)
            raise

    def _response(self, receipt):
        if self._state.closed or any(event.is_set() for event in receipt.cancel_events):
            raise ModelCancelled("Model response discarded after cancellation.")
        if receipt.deadline.remaining() == 0:
            timeout = isinstance(receipt.error, (TimeoutError, httpx.TimeoutException, ModelDeadlineExceeded)) or getattr(receipt.error, "kind", None) == "timeout"
            phase = (getattr(receipt.error, "phase", None) or "transport") if timeout else "response"
            raise ModelDeadlineExceeded(
                "Model execution exceeded its deadline; late output was discarded.",
                source=receipt.deadline.source,
                phase=phase,
            )
        if receipt.error is not None:
            raise receipt.error
        return receipt.response

    def _abandon(self, receipt, error):
        with self._state.condition:
            receipt.abandoned = type(error).__name__
            if (
                isinstance(error, ModelDeadlineExceeded)
                and error.source == "request"
                and receipt.sent
                and not receipt.done.is_set()
            ):
                fault = self._state.connection_faults.setdefault(
                    receipt.metadata["connection"], {"failures": 0, "blocked": ""}
                )
                fault["failures"] += 1
                self._state.failures = fault["failures"]
                receipt.failure_counted = True
                if fault["failures"] >= self._state.limits.max_failures:
                    fault["blocked"] = "consecutive request failures"

    def request(
        self,
        *,
        purpose="model",
        role=None,
        task_id=None,
        deadline=None,
        cancel_event=None,
        **kwargs,
    ):
        active = _SCOPE.get().gateway
        if active is not None and active._state is not self._state:
            return active.bind(self).request(
                purpose=purpose,
                role=role,
                task_id=task_id,
                deadline=deadline,
                cancel_event=cancel_event,
                **kwargs,
            )
        params, metadata, expires, cancels = self._prepare_request(
            purpose, role, task_id, deadline, cancel_event, kwargs
        )
        try:
            receipt = self._state.acquire(metadata, expires, cancels)
        except ModelDeadlineExceeded as error:
            error.phase = "queue"
            error.settled = True
            raise
        self._launch_request(receipt, params)
        try:
            while not receipt.done.is_set():
                with self._state.condition:
                    if receipt.done.is_set():
                        break
                    self._state.check(expires, receipt.cancel_events, self._connection_key())
                receipt.done.wait(min(0.05, expires.remaining()))
            return self._response(receipt)
        except (ModelCancelled, ModelDeadlineExceeded) as error:
            if isinstance(error, ModelDeadlineExceeded):
                error.request_id, error.sent, error.settled = (
                    receipt.id,
                    receipt.sent,
                    receipt.done.is_set(),
                )
                error.phase = error.phase or "transport"
            self._abandon(receipt, error)
            raise

    async def arequest(
        self,
        *,
        purpose="model",
        role=None,
        task_id=None,
        deadline=None,
        cancel_event=None,
        **kwargs,
    ):
        """Await the same owned transport without a second waiting thread."""
        active = _SCOPE.get().gateway
        if active is not None and active._state is not self._state:
            return await active.bind(self).arequest(
                purpose=purpose,
                role=role,
                task_id=task_id,
                deadline=deadline,
                cancel_event=cancel_event,
                **kwargs,
            )
        params, metadata, expires, cancels = self._prepare_request(
            purpose, role, task_id, deadline, cancel_event, kwargs
        )
        caller_cancel = threading.Event()
        cancels = (*cancels, caller_cancel)
        receipt = None
        try:
            while receipt is None:
                receipt = self._state.acquire(metadata, expires, cancels, wait=False)
                if receipt is None:
                    await asyncio.sleep(min(0.01, expires.remaining()))
            self._launch_request(receipt, params)
            while not receipt.done.is_set():
                with self._state.condition:
                    self._state.check(expires, receipt.cancel_events, self._connection_key())
                await asyncio.sleep(min(0.01, expires.remaining()))
            return self._response(receipt)
        except (asyncio.CancelledError, ModelCancelled, ModelDeadlineExceeded) as error:
            caller_cancel.set()
            if isinstance(error, ModelDeadlineExceeded):
                error.phase = error.phase or ("transport" if receipt else "queue")
                error.request_id = receipt.id if receipt else None
                error.sent = bool(receipt and receipt.sent)
                error.settled = receipt.done.is_set() if receipt else True
            if receipt is not None:
                self._abandon(receipt, error)
            raise

    def _invoke(self, receipt, params):
        try:
            with self._state.condition:
                receipt.registered = True
                self._state.attempts += 1
                self._state.new_attempts += 1
                self._state.sequence += 1
            with self._state.condition:
                self._state.check(receipt.deadline, receipt.cancel_events)
            client = self._transport()
            # SDK platform headers can be cold/slow; prepare them before the
            # final deadline check so expired work does not start an HTTP call.
            _ = getattr(client, "default_headers", None)
            with self._state.condition:
                self._state.check(
                    receipt.deadline,
                    receipt.cancel_events,
                    receipt.metadata["connection"],
                )
            request, prepared, timeout = params
            timeout = _clamp_timeout(timeout, max(0.001, receipt.deadline.remaining()))
            with self._state.condition:
                self._state.check(
                    receipt.deadline,
                    receipt.cancel_events,
                    receipt.metadata["connection"],
                )
                receipt.sent = True
                self._state.calls += 1
                self._state.sequence += 1
            self._state.publish_observation()

            def check_transport():
                with self._state.condition:
                    self._state.check(
                        receipt.deadline,
                        receipt.cancel_events,
                        receipt.metadata["connection"],
                    )

            response = client.execute(request, prepared, timeout, check_transport)
            receipt.usage = response.usage
            response.validate()
            if response.replay and request.state_root is not None:
                from homebrew_harness.execution.model_protocol import save_replay
                from homebrew_harness.execution.providers.common import binding_key
                from dataclasses import replace

                state = save_replay(
                    request.state_root,
                    request.context_key,
                    binding_key(client, request.model),
                    response.replay,
                )
                response = replace(response, replay=state)
            receipt.response = response
        except BaseException as error:
            receipt.error = error
            if getattr(error, "model_usage", None) is not None:
                receipt.usage = error.model_usage
        finally:
            self._state.finish(receipt)


def _timeout_seconds(explicit, client):
    value = explicit if explicit is not None else getattr(client, "timeout", None)
    if not isinstance(value, (int, float)):
        value = getattr(value, "read", None)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value) or value < 0:
            raise ValueError("Model timeout must be finite and nonnegative.")
        return float(value)
    return math.inf


def _clamp_timeout(configured, remaining):
    if isinstance(configured, httpx.Timeout):

        def limit(value):
            return remaining if value is None else min(value, remaining)

        return httpx.Timeout(
            connect=limit(configured.connect),
            read=limit(configured.read),
            write=limit(configured.write),
            pool=limit(configured.pool),
        )
    if isinstance(configured, (int, float)) and not isinstance(configured, bool):
        return min(float(configured), remaining)
    return remaining


def checkpoint():
    gateway = _SCOPE.get().gateway
    if gateway is not None:
        gateway.checkpoint()


def ensure_gateway(client, *, limits=None, workdir=None):
    active = _SCOPE.get().gateway
    if active is not None:
        return active.bind(client)
    if isinstance(client, ModelGateway):
        return client
    with _CLIENT_GUARD:
        attributes = getattr(client, "__dict__", {})
        cached = attributes.get("_harness_gateway")
        if isinstance(cached, ModelGateway):
            return cached
        gateway = ModelGateway(client, limits, workdir=workdir)
        if isinstance(attributes, dict):
            attributes["_harness_gateway"] = gateway
        return gateway


def request(client, **kwargs):
    return ensure_gateway(client).request(**kwargs)


def gateway_error(error):
    seen = set()
    while error is not None and id(error) not in seen:
        if isinstance(error, ModelGatewayError):
            return error
        seen.add(id(error))
        error = error.__cause__
    return None


_GATEWAY_OPTIONS = (
    ("model_concurrency", "HARNESS_MODEL_CONCURRENCY", int),
    ("model_request_timeout", "HARNESS_MODEL_REQUEST_TIMEOUT", float),
    ("model_session_timeout", "HARNESS_MODEL_SESSION_TIMEOUT", float),
)


def gateway_limits_from_args(args):
    values = []
    defaults = GatewayLimits()
    fields = ("concurrency", "request_timeout", "session_timeout")
    for (name, variable, converter), field_name in zip(_GATEWAY_OPTIONS, fields):
        supplied = getattr(args, name, None)
        if supplied is None:
            raw = os.getenv(variable, "").strip()
            try:
                supplied = converter(raw) if raw else getattr(defaults, field_name)
            except ValueError:
                raise ValueError(
                    "Invalid external gateway setting: " + variable
                ) from None
        values.append(supplied)
    return GatewayLimits(*values)


def model_operation(function):
    signature = inspect.signature(function)

    @wraps(function)
    def run(*args, **kwargs):
        values = signature.bind_partial(*args, **kwargs).arguments
        gateway = ensure_gateway(values["client"])
        parent = _SCOPE.get()
        cancel_event = values.get("stop_event") or getattr(
            values.get("components"), "stop_event", None
        )
        with gateway.scope(
            context_id=parent.context_id or function.__name__, cancel_event=cancel_event
        ):
            return function(*args, **kwargs)

    return run


def current_scope():
    return _SCOPE.get()
