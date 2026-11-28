"""Service-wide inbox digests over a host-declared JSON webhook."""

import logging
import math
import os
import re
import threading
import time
import uuid
from contextvars import ContextVar
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from homebrew_harness.execution.execution_control import acquired, control_scope
from homebrew_harness.execution.model_connections import read_environment
from homebrew_harness.execution.model_gateway import Deadline, ModelCancelled, ModelDeadlineExceeded
from homebrew_harness.platforms.http import JsonTransport, RemoteFailure, validated_url
from homebrew_harness.platforms.registry import load_ca_bundle, read_credentials_file
from homebrew_harness.runtime.limits import backoff
from homebrew_harness.runtime.runtime_control import WorkAccepted
from homebrew_harness.security.data_boundary import redact, register_private_values
from .inbox import build_inbox
from .protocol import fingerprint, request_key

_SECTION = "service_notifications"
_METHOD = "notification_send"
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
_DURATIONS = {
    "every_seconds": (43200, 0.1, 31536000),
    "cooldown_seconds": (300, 0, 86400),
    "request_seconds": (10, 0.1, 60),
}
_DEFAULTS = {
    "enabled": True,
    "mode": "remote",
    "url_env": None,
    "token_env": None,
    "ca_file_env": None,
    "credentials_file": None,
    "min_pending": None,
    "include_expired": True,
    **{key: bounds[0] for key, bounds in _DURATIONS.items()},
}
_INITIAL_STATE = dict(
    last_sent_at=None, last_digest=None, last_trigger=None, last_delivery_id=None,
    last_error=None, attempts=0, next_retry_at=None, sent_count=0,
)


def validate_notifications(raw):
    if not isinstance(raw, dict) or raw.keys() - _DEFAULTS.keys():
        raise ValueError("Invalid service notifications fields.")
    if not raw:
        return {}
    spec = {**_DEFAULTS, **raw}
    for key in ("enabled", "include_expired"):
        if type(spec[key]) is not bool:
            raise ValueError(f"notifications.{key} must be Boolean.")
    if spec["mode"] not in ("remote", "local"):
        raise ValueError("notifications.mode must be remote or local.")
    for key in ("url_env", "token_env", "ca_file_env"):
        name = spec[key]
        if name is None and key != "url_env":
            continue
        if not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,100}", name):
            raise ValueError(f"notifications.{key} must name a host environment variable.")
    path = spec["credentials_file"]
    if path is not None and (
        not isinstance(path, str) or "\0" in path or not Path(path).is_absolute()
    ):
        raise ValueError("Notification credentials require an absolute external path.")
    pending = spec["min_pending"]
    if pending is not None and (type(pending) is not int or pending < 1):
        raise ValueError("notifications.min_pending must be a positive integer.")
    for key, (_, low, high) in _DURATIONS.items():
        value = spec[key]
        if type(value) not in (int, float) or not low <= value <= high:
            raise ValueError(f"Invalid notifications.{key} duration.")
        spec[key] = float(value)
    return spec


def _register_credentials(spec, values):
    url = values.get(spec["url_env"])
    token = values.get(spec["token_env"]) if spec["token_env"] else ""
    register_private_values(url, token)
    return url, token


def read_notification_credentials(spec, roots):
    values = read_credentials_file(spec["credentials_file"], roots, label="Notification credentials")
    _register_credentials(spec, values)
    return values


_WEBHOOK_IO = ContextVar("service_webhook_io", default=False)


class _PrivateWebhookLogs(logging.Filter):
    def filter(self, record):
        # httpx logs the entire URL; httpcore's traces can contain response headers.
        # These destinations are private even when the host enables debug logging.
        return not _WEBHOOK_IO.get()


_PRIVATE_WEBHOOK_LOGS = _PrivateWebhookLogs()


def open_notification_transport(spec, environment, roots):
    values = read_notification_credentials(spec, roots) if spec["credentials_file"] else environment
    url, token = _register_credentials(spec, values)
    if not url or spec["token_env"] and not token:
        raise ValueError("Notification host credentials are missing.")
    if not isinstance(token, str) or any(not 33 <= ord(c) <= 126 for c in token):
        raise ValueError("Invalid notification Bearer token.")
    try:
        parsed = urlsplit(validated_url(url))
        if (
            parsed.port == 0 or parsed.username is not None or parsed.password is not None
            or any(c.isspace() for c in url) or any(c in url for c in "\\?#")
        ):
            raise ValueError()
        httpx.URL(url)  # Check the actual transport parser before allocating a client.
    except (ValueError, httpx.InvalidURL):
        raise ValueError(
            "Notification URL must use HTTPS or loopback HTTP, without embedded credentials, query or fragment."
        ) from None
    if spec["mode"] == "local" and parsed.hostname not in _LOOPBACK:
        raise ValueError("Local notification mode requires a loopback URL.")
    verify = True
    if spec["ca_file_env"]:
        ca_file = values.get(spec["ca_file_env"])
        if not ca_file:
            raise ValueError("Notification CA configuration is missing.")
        try:
            verify, _ = load_ca_bundle(ca_file, roots=roots)
        except (ValueError, OSError):
            raise ValueError("Notification CA must be a valid external host certificate file.") from None
    for logger in ("httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2"):
        logging.getLogger(logger).addFilter(_PRIVATE_WEBHOOK_LOGS)
    try:
        transport = JsonTransport(url, token, verify=verify)
    except (ValueError, httpx.InvalidURL):
        raise ValueError("Invalid notification URL or credential.") from None
    transport._webhook_short_token = token if len(token) < 8 else ""
    return transport


def _retry_seconds(error, attempts, now):
    status = error.status if isinstance(error, RemoteFailure) else None
    non_transient = status is not None and 400 <= status < 500 and status not in {408, 429}
    delay = backoff(attempts - 1, initial=60 if non_transient else 1, maximum=3600)
    if status in {429, 503}:
        retry = next((value for key, value in error.headers.items() if key.lower() == "retry-after"), None)
        if retry is not None:
            try:
                try:
                    seconds = float(retry)
                except ValueError:
                    seconds = parsedate_to_datetime(retry).timestamp() - now
                if math.isfinite(seconds):
                    delay = max(delay, min(seconds, 86400))
            except (ValueError, OverflowError, OSError):
                pass
    return delay


def _error_message(error):
    # Never copy URL/token, arbitrary response bodies, headers or exception text
    # into persistent state or control receipts.
    if isinstance(error, RemoteFailure):
        return f"Webhook returned HTTP {error.status}."
    if isinstance(error, (TimeoutError, httpx.TimeoutException, ModelDeadlineExceeded)):
        return "Webhook request timed out."
    if isinstance(error, httpx.RequestError):
        return "Webhook connection failed."
    return "Webhook request failed."


class ServiceNotifications:
    def __init__(self, runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.id = runtime.id
        self.spec = runtime.spec.notifications
        self.enabled = self.spec.get("enabled", False)
        self.stop = threading.Event()
        self.parked = threading.Event()
        if runtime.header.get("operations_phase", "active") != "active":
            self.parked.set()
        self.send_lock = threading.Lock()
        self.thread = None
        self.transport = None
        # Protected by send_lock. Keep the exact in-flight body process-local;
        # only delivery metadata and receipts go into the durable store.
        self._auto_retry = None
        self._unpersisted_outcome = None
        self._next_retry_at = 0
        if self.enabled and not self.spec["credentials_file"]:
            try:
                _register_credentials(self.spec, self._host_environment())
            except (ValueError, OSError):
                pass  # An unreadable environment will be reported on a send attempt.

    def start(self):
        if not self.enabled or self.thread is not None:
            return
        try:
            self.thread = self.runtime.workers.start(
                self.loop, name="service-notifications", track_operation=False
            )
        except WorkAccepted as started:
            self.thread = started.thread

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join()
        with self.send_lock:
            if self.transport is not None:
                token = _WEBHOOK_IO.set(True)
                try:
                    self.transport.close()
                finally:
                    self.transport = None
                    _WEBHOOK_IO.reset(token)
            self._auto_retry = None
            self._flush_outcome()

    def park(self, *, cancel_events=()):
        self.parked.set()
        # Drain both the POST and its durable outcome without stopping the worker.
        with acquired(self.send_lock, cancel_events=(self.stop, *cancel_events)):
            self._flush_outcome()

    def unpark(self):
        self.parked.clear()

    def _state(self):
        saved = self.store.runtime.get(_SECTION, self.id, {})
        return {key: saved.get(key, value) for key, value in _INITIAL_STATE.items()}

    def status(self):
        config = {
            key: self.spec[key]
            for key in ("mode", "every_seconds", "min_pending", "cooldown_seconds", "request_seconds", "include_expired")
        } if self.spec else {}
        if config:
            config.update(
                credential_source="file" if self.spec["credentials_file"] else "environment",
                bearer_token=bool(self.spec["token_env"]), custom_ca=bool(self.spec["ca_file_env"]),
            )
        return {"enabled": self.enabled, "config": config, **self._state()}

    def _active(self, meta):
        return (
            not self.stop.is_set()
            and not self.parked.is_set()
            and meta.get("operations_phase", "active") == "active"
            and meta["state"] not in {"stopping", "stopped"}
        )

    def _require_active(self, meta):
        if not self._active(meta):
            raise ValueError("Service is stopping or parked.")

    def _host_environment(self):
        # The child uses the saved service config, unlike the attaching Harness.
        host = getattr(self.runtime, "child", None) or self.runtime.core
        env_file = host.config.settings.env_file
        if not env_file:
            return os.environ
        path = Path(env_file).expanduser()
        if path.is_symlink() or any(
            path.resolve().is_relative_to(root)
            for root in (self.store.root, self.runtime.spec.workspace)
        ):
            raise ValueError("Notification environment must remain outside the service directories.")
        return read_environment(path, self.runtime.spec.workspace)

    def _connect(self):
        roots = (self.store.root, self.runtime.spec.workspace)
        environment = {} if self.spec["credentials_file"] else self._host_environment()
        self.transport = open_notification_transport(self.spec, environment, roots)

    def _redact_preview(self, text):
        text = redact(text)
        # Globally registering a short token would redact common substrings in
        # unrelated records. Apply it only to webhook display text, before clipping.
        short_token = getattr(self.transport, "_webhook_short_token", "")
        return text.replace(short_token, "[redacted]") if isinstance(short_token, str) and short_token else text

    def _inbox(self):
        return build_inbox(
            self.runtime, include_expired=self.spec["include_expired"], redact_text=self._redact_preview,
        )

    def _auto_candidate(self, state):
        with acquired(self.runtime.lock, cancel_events=(self.stop,)):
            meta = self.store.get(self.id)
            if not self._active(meta) or meta["state"] != "running":
                return None, None
            now = time.time()
            if now < max(self._next_retry_at, state["next_retry_at"] or 0):
                return None, None
            last = state["last_sent_at"]
            if last is not None and now - last < self.spec["cooldown_seconds"]:
                return None, None
            if self._auto_retry is not None:
                # Retry the admitted snapshot even if the live inbox has changed.
                payload, _ = self._auto_retry
                return payload, payload["trigger"]
            due = last is None or now - last >= self.spec["every_seconds"]
            minimum = self.spec["min_pending"]
            if not due and minimum is None:
                return None, None
            inbox = self._inbox()
            pending = inbox["counts"]["actionable"]
            if not pending:
                return None, None
            if minimum is not None and pending >= minimum and inbox["digest"] != state["last_digest"]:
                return inbox, "threshold"
            return (inbox, "schedule") if due else (None, None)

    def tick(self):
        if (
            not self.enabled or self.parked.is_set()
            or not self.send_lock.acquire(blocking=False)
        ):
            return
        try:
            if not self._flush_outcome():
                return
            state = self._state()
            inbox, trigger = self._auto_candidate(state)
            if trigger is None:
                return
            if self.transport is None:
                try:
                    self._connect()
                except Exception:
                    self._attempt(inbox, trigger, state)
                    return
                # Resolve credentials before rendering the actual outbound inbox,
                # so registered values are removed even before previews are clipped.
                inbox, trigger = self._auto_candidate(state)
                if trigger is None:
                    return
            self._attempt(inbox, trigger, state)
        finally:
            self.send_lock.release()

    def loop(self):
        interval = min(1, max(0.1, self.runtime.spec.poll_seconds))
        fault = True  # Clear a persisted worker diagnostic on first successful observation.
        while not self.stop.wait(interval):
            try:
                self.tick()
                if fault:
                    self.store.runtime_failure(self.runtime.header, "notifications")
                    fault = False
            except Exception:
                if self.stop.is_set():
                    return
                # Storage and inbox observation errors must not terminate this
                # worker or the service coordinator. Retry at a bounded rate.
                self.runtime.report_failure(
                    "notifications", RuntimeError("Notification worker could not observe or persist its state.")
                )
                fault = True
                if self.stop.wait(1):
                    return

    def send(self, force, request_id):
        if type(force) is not bool:
            raise ValueError("force must be Boolean.")
        arguments = {"force": force}
        key = request_key("command", self.id, request_id)
        signature = fingerprint([_METHOD, arguments])
        # New commands should not wait behind a delivery being drained. A
        # replay can still wait for that delivery's final, durable receipt.
        meta = self.store.get(self.id)
        if (
            not self._active(meta)
            and self.store.runtime.get("service_commands", key) is None
        ):
            self._require_active(meta)
        with acquired(self.send_lock, cancel_events=(self.stop,)):
            prior = self.store.runtime.get("service_commands", key)
            if prior:
                if prior["payload"] != signature:
                    raise ValueError("Control request_id has another command payload.")
                return prior["result"]
            self._require_active(self.store.get(self.id))
            if not self._flush_outcome():
                raise ValueError("Service is stopping or parked.")
            inbox = None
            if self.enabled:
                with acquired(self.runtime.lock, cancel_events=(self.stop,)):
                    inbox = self._inbox()
                if (force or inbox["counts"]["actionable"]) and self.transport is None:
                    try:
                        self._connect()
                    except Exception:
                        pass  # Recorded as a configuration failure if we accept the send.
                    else:
                        with acquired(self.runtime.lock, cancel_events=(self.stop,)):
                            inbox = self._inbox()
            accepted = False

            def reserve(conn):
                nonlocal accepted
                self._require_active(self.store.guard(conn, self.runtime.header))
                if not self.enabled:
                    return {"sent": False, "reason": "notifications_disabled"}
                if not force and not inbox["counts"]["actionable"]:
                    return {"sent": False, "reason": "no_actionable_items"}
                accepted = True
                return {"sent": False, "reason": "delivery_unknown", "trigger": "manual",
                        "delivery_id": "notify_" + key, "counts": inbox["counts"]}

            # Reserve a non-null receipt before the POST. A replay after a crash
            # observes the uncertain outcome instead of posting again blindly.
            receipt = self.store.command(self.runtime.header, _METHOD, arguments, request_id, reserve)
            if not accepted:
                return receipt
            return self._attempt(inbox, "manual", self._state(), key=key, receipt=receipt)

    def _attempt(self, inbox, trigger, state, *, key=None, receipt=None):
        meta = self.store.get(self.id)
        if key is None:
            if not self._active(meta) or meta["state"] != "running":
                return receipt
        elif self.stop.is_set() or meta["state"] in {"stopping", "stopped"}:
            return receipt
        # A manual send was admitted when its receipt was reserved. Quiesce
        # waits for it (and any automatic send admitted above) to commit.
        if key is None and self._auto_retry is not None:
            payload, receipt = self._auto_retry
        else:
            payload = {
                "event": "service.inbox.digest", "service_id": self.id,
                "trigger": trigger, "generated_at": time.time(), **inbox,
            } if self.transport is not None else None
            if receipt is None:
                receipt = {
                    "sent": False, "delivery_id": "notify_" + uuid.uuid4().hex,
                    "trigger": trigger, "counts": inbox["counts"],
                }
                if payload is not None:
                    self._auto_retry = payload, receipt
        delivery_id = receipt["delivery_id"]
        error = None
        message = None
        if payload is None:
            message = "Webhook host configuration is missing or invalid."
        else:
            deadline = Deadline.after(self.spec["request_seconds"])
            scope = _WEBHOOK_IO.set(True)
            try:
                with control_scope(deadline, cancel_events=(self.stop,)):
                    self.transport.request(
                        "POST", "", deadline, body=payload,
                        headers={"Idempotency-Key": delivery_id}, parse_json=False,
                    )
            except ModelCancelled:
                return receipt
            except Exception as exc:
                error = exc
                message = _error_message(exc)
            finally:
                _WEBHOOK_IO.reset(scope)

        now = time.time()
        if message is None:
            result = {k: v for k, v in receipt.items() if k != "reason"} | {"sent": True}
            state.update(last_sent_at=now, last_digest=inbox["digest"], last_trigger=trigger,
                         last_delivery_id=delivery_id, last_error=None, attempts=0,
                         next_retry_at=None, sent_count=state["sent_count"] + 1)
            if key is None:
                self._auto_retry = None
                self._next_retry_at = 0
        else:
            result = {**receipt, "reason": "delivery_failed", "last_error": message}
            attempts = state["attempts"] + 1
            state.update(last_error=message, attempts=attempts,
                         next_retry_at=now + _retry_seconds(error, attempts, now))
            self._next_retry_at = max(self._next_retry_at, state["next_retry_at"])
        # A failed commit must not turn a successful POST into a new send or
        # discard the backoff from a failed one. Retry the write, not the POST.
        self._unpersisted_outcome = state, key, result
        return result if self._flush_outcome() else receipt

    def _flush_outcome(self):
        if self._unpersisted_outcome is None:
            return True
        state, key, result = self._unpersisted_outcome
        with self.store.db.write() as conn:
            meta = self.store.guard(conn, self.runtime.header)
            # Quiesce waits on send_lock before changing draining to parked.
            if meta.get("operations_phase", "active") not in {"active", "draining"}:
                return False
            self.store.db._put(conn, _SECTION, self.id, {
                **state, "service_id": self.id, "checkpoint": "service:" + self.id,
            })
            if key is not None:
                command = self.store.db._get(conn, "service_commands", key)
                self.store.db._put(conn, "service_commands", key, {**command, "result": result})
        self._unpersisted_outcome = None
        return True
