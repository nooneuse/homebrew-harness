"""Bounded persistence attempts; only proven uncommitted writes are retried."""

import time

from homebrew_harness.configuration import DEFAULT_PERSISTENCE_SECONDS
from homebrew_harness.execution.execution_control import (
    check, current_persistence, operation_deadline, persistence_scope,
)
from homebrew_harness.execution.model_gateway import ModelDeadlineExceeded
from homebrew_harness.runtime.errors import annotate_error
from homebrew_harness.runtime.runtime_state import RecoveryRequired


class StorageTimeout(RecoveryRequired):
    def __init__(self, phase, limit, started, deadline, *, rollback_confirmed=False):
        scope = current_persistence() or {}
        code = "persistence_timeout" if deadline.source == "persistence" else "storage_cleanup_timeout" if deadline.source == "cleanup" else "storage_" + phase + "_timeout"
        if deadline.source == "persistence" and scope:
            limit, started = scope["seconds"], scope["started"]
        if deadline.source == "cleanup":
            limit = max(0, deadline.expires_at - started)
        super().__init__("Local persistence deadline reached during " + phase + ". Inspect commit state before repeating work.")
        self.reason_code = code
        self.retryable = phase in {"open", "lock", "maintenance_lock"}
        annotate_error(self, code=code, source="storage", phase=phase,
                       operation=scope.get("operation", "state_commit"), context_id=scope.get("context_id"),
                       limit_name="persistence.seconds" if deadline.source == "persistence" else "effective_cleanup_seconds" if deadline.source == "cleanup" else "storage." + phase + "_seconds", limit_value=limit,
                       observed_value=max(0, time.monotonic()-started), unit="seconds",
                       elapsed_seconds=max(0, time.monotonic()-started), deadline_source=deadline.source,
                       effective_timeout_seconds=max(0, deadline.expires_at-started) if deadline.expires_at is not None else None,
                       persistence_timeout_seconds=scope.get("seconds"),
                       rollback_confirmed=rollback_confirmed, commit_state="not_started" if self.retryable else "uncommitted")

    def rolled_back(self):
        self.retryable = True
        annotate_error(self, rollback_confirmed=True, commit_state="rolled_back")


def storage_check(deadline, *, phase, limit, started):
    try:
        check(deadline, gateway=False)
    except ModelDeadlineExceeded as error:
        if error.source not in {"tool", "persistence", "cleanup"}:
            raise
        raise StorageTimeout(phase, limit, started, deadline) from error


def persist(operation, callback, *, context_id=None, seconds=DEFAULT_PERSISTENCE_SECONDS, attempts=3):
    """Callback must be an idempotent checkpoint/result write, never a tool body."""
    if type(attempts) is not int or not 1 <= attempts <= 3:
        raise ValueError("Persistence allows one to three attempts.")
    with persistence_scope(seconds, operation=operation, context_id=context_id) as deadline:
        started = time.monotonic()
        for attempt in range(1, attempts+1):
            try:
                storage_check(deadline, phase="persistence", limit=seconds, started=started)
                return callback()
            except StorageTimeout as error:
                exhausted = deadline.remaining() == 0
                annotate_error(error, attempts=attempt, attempt_limit=attempts,
                               retry_stop="deadline_reached" if exhausted else "not_proven_uncommitted" if not error.retryable else "attempt_limit" if attempt == attempts else "retrying")
                if exhausted or not error.retryable or attempt == attempts:
                    raise
                time.sleep(min(0.05 * attempt, deadline.remaining()))
