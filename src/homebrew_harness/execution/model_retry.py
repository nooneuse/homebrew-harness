"""Bounded transport retries, leaving tool execution and output judgment to callers."""

import asyncio
import time
from contextlib import contextmanager
from contextvars import ContextVar

from homebrew_harness.execution.model_gateway import ensure_gateway, ModelDeadlineExceeded
from homebrew_harness.execution.model_protocol import ProviderError
from homebrew_harness.execution.execution_control import operation_deadline, check
from homebrew_harness.runtime.errors import AgentError


_POLICY = ContextVar("model_transport_attempts", default=3)


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


def wait_retry(gateway, error, attempt, deadline):
    if deadline.remaining() is None and receipt_pending(gateway, error):
        raise error
    while receipt_pending(gateway, error):
        check(deadline)
        time.sleep(pause_seconds(deadline))
    check(deadline)
    delay_end = time.monotonic() + min(0.1 * 2 ** attempt, 1)
    while time.monotonic() < delay_end:
        check(deadline)
        time.sleep(pause_seconds(deadline, delay_end))


async def await_retry(gateway, error, attempt, deadline):
    if deadline.remaining() is None and receipt_pending(gateway, error):
        raise error
    while receipt_pending(gateway, error):
        check(deadline)
        await asyncio.sleep(pause_seconds(deadline))
    check(deadline)
    delay_end = time.monotonic() + min(0.1 * 2 ** attempt, 1)
    while time.monotonic() < delay_end:
        check(deadline)
        await asyncio.sleep(pause_seconds(deadline, delay_end))


def request(client, *, attempts=None, on_attempt=None, **parameters):
    attempts = _POLICY.get() if attempts is None else attempts
    if type(attempts) is not int or not 1 <= attempts <= 8:
        raise ValueError("Model retry attempts must be between 1 and 8.")
    gateway = ensure_gateway(client)
    deadline = operation_deadline().intersect(parameters.get("deadline") or operation_deadline())
    for attempt in range(attempts):
        check(deadline)
        if on_attempt:
            on_attempt(attempt)
        try:
            return gateway.request(**parameters)
        except (ProviderError, ModelDeadlineExceeded) as error:
            if not retryable(error) or attempt + 1 == attempts:
                raise
            wait_retry(gateway, error, attempt, deadline)


async def arequest(client, *, attempts=None, on_attempt=None, **parameters):
    attempts = _POLICY.get() if attempts is None else attempts
    if type(attempts) is not int or not 1 <= attempts <= 8:
        raise ValueError("Model retry attempts must be between 1 and 8.")
    gateway = ensure_gateway(client)
    deadline = operation_deadline().intersect(parameters.get("deadline") or operation_deadline())
    for attempt in range(attempts):
        check(deadline)
        if on_attempt:
            on_attempt(attempt)
        try:
            return await gateway.arequest(**parameters)
        except (ProviderError, ModelDeadlineExceeded) as error:
            if not retryable(error) or attempt + 1 == attempts:
                raise
            await await_retry(gateway, error, attempt, deadline)
