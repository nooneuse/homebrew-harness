"""Bounded transport retries, leaving tool execution and output judgment to callers."""

import asyncio
import time
from contextlib import contextmanager
from contextvars import ContextVar

from homebrew_harness.execution.model_gateway import ensure_gateway, ModelDeadlineExceeded
from homebrew_harness.execution.model_protocol import ProviderError
from homebrew_harness.execution.execution_control import operation_deadline, check
from homebrew_harness.runtime.errors import AgentError
from homebrew_harness.runtime.limits import current_limits, positive, backoff


_POLICY = ContextVar("model_transport_attempts", default=None)


@contextmanager
def retry_policy(attempts):
    token = _POLICY.set(attempts)
    try:
        yield
    finally:
        _POLICY.reset(token)


class RetryBoundary(AgentError):
    def __init__(self, message, decision=None):
        super().__init__(message)
        self.decision = decision


def retryable(error):
    return (isinstance(error, ProviderError) and error.kind != "context_overflow") or (
        isinstance(error, ModelDeadlineExceeded) and error.source == "request")


def receipt_pending(gateway, error):
    identity = getattr(error, "request_id", None)
    if not identity:
        return False
    with gateway._state.condition:
        receipt = gateway._state.inflight.get(identity)
        return receipt is not None and not receipt.done.is_set()


def pause_seconds(deadline, end=None):
    seconds = 0.05 if end is None else min(0.05, max(0, end - time.monotonic()))
    remaining = deadline.remaining()
    return seconds if remaining is None else min(seconds, remaining)


def wait_retry(gateway, error, attempt, deadline, *, on_phase=None):
    if deadline.remaining() is None and receipt_pending(gateway, error):
        raise error
    while receipt_pending(gateway, error):
        if on_phase:
            on_phase("waiting_for_previous_request")
        check(deadline)
        time.sleep(pause_seconds(deadline))
    if on_phase:
        on_phase("retry_delay")
    check(deadline)
    delay_end = time.monotonic() + backoff(attempt, initial=current_limits().transport_delay_seconds, maximum=current_limits().transport_delay_cap_seconds)
    while time.monotonic() < delay_end:
        check(deadline)
        time.sleep(pause_seconds(deadline, delay_end))


async def await_retry(gateway, error, attempt, deadline, *, on_phase=None):
    if deadline.remaining() is None and receipt_pending(gateway, error):
        raise error
    while receipt_pending(gateway, error):
        if on_phase:
            on_phase("waiting_for_previous_request")
        check(deadline)
        await asyncio.sleep(pause_seconds(deadline))
    if on_phase:
        on_phase("retry_delay")
    check(deadline)
    delay_end = time.monotonic() + backoff(attempt, initial=current_limits().transport_delay_seconds, maximum=current_limits().transport_delay_cap_seconds)
    while time.monotonic() < delay_end:
        check(deadline)
        await asyncio.sleep(pause_seconds(deadline, delay_end))


def retry_diagnostic(error, used, limit, stage, previous=None):
    from homebrew_harness.runtime.errors import annotate_error, error_info
    reason = "attempts_exhausted" if used == limit and stage == "request" else (
        "not_retryable" if stage == "request" else stage)
    annotate_error(error, attempts=used, attempt_limit=limit, retry_stop=reason,
                   previous_error=error_info(previous)["message"] if previous is not None and previous is not error else None)


def request(client, *, attempts=None, on_attempt=None, **parameters):
    attempts = (_POLICY.get() or current_limits().transport_attempts) if attempts is None else attempts
    positive(attempts, "transport_attempts", integer=True)
    gateway = ensure_gateway(client)
    deadline = operation_deadline().intersect(parameters.get("deadline") or operation_deadline())
    used, previous, stage = 0, None, "before_attempt"
    def observe_phase(value):
        nonlocal stage
        stage = value
    try:
        for attempt in range(attempts):
            stage = "before_attempt"
            check(deadline)
            if on_attempt:
                on_attempt(attempt)
            used += 1
            stage = "request"
            try:
                return gateway.request(**parameters)
            except (ProviderError, ModelDeadlineExceeded) as error:
                if not retryable(error) or attempt + 1 == attempts:
                    raise
                previous = error
                stage = "waiting_for_previous_request" if receipt_pending(gateway, error) else "retry_delay"
                wait_retry(gateway, error, attempt, deadline, on_phase=observe_phase)
    except Exception as error:
        retry_diagnostic(error, used, attempts, stage, previous)
        raise


async def arequest(client, *, attempts=None, on_attempt=None, **parameters):
    attempts = (_POLICY.get() or current_limits().transport_attempts) if attempts is None else attempts
    positive(attempts, "transport_attempts", integer=True)
    gateway = ensure_gateway(client)
    deadline = operation_deadline().intersect(parameters.get("deadline") or operation_deadline())
    used, previous, stage = 0, None, "before_attempt"
    def observe_phase(value):
        nonlocal stage
        stage = value
    try:
        for attempt in range(attempts):
            stage = "before_attempt"
            check(deadline)
            if on_attempt:
                on_attempt(attempt)
            used += 1
            stage = "request"
            try:
                return await gateway.arequest(**parameters)
            except (ProviderError, ModelDeadlineExceeded) as error:
                if not retryable(error) or attempt + 1 == attempts:
                    raise
                previous = error
                stage = "waiting_for_previous_request" if receipt_pending(gateway, error) else "retry_delay"
                await await_retry(gateway, error, attempt, deadline, on_phase=observe_phase)
    except Exception as error:
        retry_diagnostic(error, used, attempts, stage, previous)
        raise
