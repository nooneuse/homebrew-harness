from homebrew_harness.runtime.limits import current_limits
"""Bounded external JSON transport; no execution or repository state."""

import json
import asyncio
import ssl
import time
import sys
from urllib.parse import urlsplit

import httpx
from homebrew_harness.execution.execution_control import check
from homebrew_harness.execution.model_gateway import ModelDeadlineExceeded
from homebrew_harness.security.data_boundary import register_private_values
from .contracts import digest


class RemoteFailure(ValueError):
    def __init__(self, status, headers=None, detail=None):
        self.status = status
        self.headers = dict(headers or {})
        from homebrew_harness.runtime.errors import annotate_error
        from homebrew_harness.security.data_boundary import redact
        self.status_code = status
        detail = detail if isinstance(detail, dict) else {}
        message = detail.get("message")
        import re
        message = re.sub(r"[\x00-\x1f\x7f]", " ", redact(message))[:1000] if isinstance(message, str) else None
        super().__init__("Remote service returned status " + str(status) + (": " + message if message else ""))
        lowered = {k.lower(): v for k, v in self.headers.items()}
        annotate_error(self, status_code=status,
            platform_code=detail.get("code") if isinstance(detail.get("code"), (str, int)) else None,
            platform_request_id=lowered.get("x-request-id") or lowered.get("x-github-request-id"),
            retry_after=lowered.get("retry-after"))


def validated_url(value):
    if (
        not isinstance(value, str)
        or len(value) > 8192
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ValueError("Invalid external service address.")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.query
    ):
        raise ValueError(
            "External service URL must identify one host without embedded credentials."
        )
    if parsed.scheme == "http" and parsed.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise ValueError(
            "Authenticated external services require HTTPS; HTTP is for loopback fixtures."
        )
    return value


class JsonTransport:
    def __init__(self, endpoint, token, *, verify=True):
        if verify is not True and not isinstance(verify, ssl.SSLContext):
            raise ValueError("External TLS verification requires a trust context.")
        self.endpoint = validated_url(endpoint)
        if not isinstance(token, str) or any(
            ord(c) < 32 or ord(c) == 127 for c in token
        ):
            raise ValueError("Invalid external service credential.")
        register_private_values(endpoint, token)
        self.credential_identity = digest(token)
        self.loop = asyncio.new_event_loop()
        headers = {"Accept": "application/json", "User-Agent": "homebrew-harness"}
        if token:
            headers["Authorization"] = "Bearer " + token
        self.client = httpx.AsyncClient(
            base_url=self.endpoint.rstrip("/") + "/",
            follow_redirects=False,
            trust_env=False,
            headers=headers,
            verify=verify,
        )
        self.identity = digest({"endpoint": self.endpoint})

    def request(
        self,
        method,
        path,
        deadline,
        *,
        params=None,
        body=None,
        headers=None,
        parse_json=True,
    ):
        from homebrew_harness.execution.execution_control import operation_deadline
        from homebrew_harness.runtime.limits import backoff
        from homebrew_harness.runtime.errors import annotate_error
        limits = current_limits()
        deadline = deadline.intersect(operation_deadline(limits.platform_request_seconds))
        started = time.monotonic()
        attempts = limits.platform_read_attempts if method in {"GET", "HEAD"} else 1
        for attempt in range(attempts):
            check(deadline)
            try:
                return self.loop.run_until_complete(asyncio.wait_for(
                    self._controlled_request(method, path, deadline, params=params, body=body, headers=headers, parse_json=parse_json),
                    timeout=deadline.remaining()))
            except (httpx.RequestError, TimeoutError, asyncio.TimeoutError) as error:
                phase = next((name for kind, name in ((httpx.ConnectTimeout, "connect"), (httpx.ReadTimeout, "read"),
                    (httpx.WriteTimeout, "write"), (httpx.PoolTimeout, "pool")) if isinstance(error, kind)), "request")
                field = "platform_" + phase + "_seconds"
                annotate_error(error, code="platform_request_failed", phase=phase,
                    limit_name=field, limit_value=getattr(limits, field), unit="seconds",
                    elapsed_seconds=time.monotonic()-started, attempt=attempt+1, method=method)
                if attempt + 1 == attempts or deadline.remaining() == 0:
                    raise
                pause = backoff(attempt, initial=limits.transport_delay_seconds, maximum=limits.transport_delay_cap_seconds)
                until = time.monotonic() + pause
                while time.monotonic() < until:
                    check(deadline)
                    time.sleep(min(limits.observation_poll_seconds, max(0, until-time.monotonic())))

    async def _controlled_request(self, method, path, deadline, **kwargs):
        # A failure status is already conclusive even when its optional body stalls.
        observed = {}
        task = asyncio.create_task(self._request(method, path, deadline, observed=observed, **kwargs))
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        try:
            while not task.done():
                check(deadline)
                await asyncio.wait({task}, timeout=current_limits().observation_poll_seconds)
            return await task
        except (TimeoutError, asyncio.TimeoutError, asyncio.CancelledError, ModelDeadlineExceeded):
            if deadline.remaining() == 0 and observed.get("failure") is not None:
                raise observed["failure"] from None
            raise
        finally:
            primary = sys.exc_info()[1]
            if not task.done():
                task.cancel()
            try:
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), current_limits().platform_close_seconds)
            except (TimeoutError, asyncio.TimeoutError, asyncio.CancelledError) as cleanup:
                if primary is None:
                    raise
                from homebrew_harness.runtime.errors import annotate_error
                annotate_error(primary, cleanup_error_type=type(cleanup).__name__)

    async def _request(
        self, method, path, deadline, *, params, body, headers, parse_json, observed
    ):
        remaining = deadline.remaining()
        target = self.endpoint if method == "POST" and not path else path
        async with self.client.stream(
            method,
            target,
            params=params,
            json=body,
            headers=headers,
            timeout=httpx.Timeout(**{name: min(getattr(current_limits(), "platform_"+name+"_seconds"), remaining)
                if remaining is not None else getattr(current_limits(), "platform_"+name+"_seconds")
                for name in ("connect", "read", "write", "pool")}),
        ) as response:
            if response.status_code < 200 or response.status_code >= 300:
                observed["failure"] = RemoteFailure(response.status_code, response.headers)
                detail = None
                async def read_error():
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > current_limits().platform_error_bytes:
                            break
                        chunks.append(chunk)
                    else:
                        body = json.loads(b"".join(chunks))
                        if isinstance(body, dict):
                            return body.get("error", body)
                try:
                    left = deadline.remaining()
                    detail = await asyncio.wait_for(read_error(), current_limits().platform_error_seconds if left is None else min(current_limits().platform_error_seconds, max(0, left)))
                except asyncio.CancelledError:
                    raise
                except (ValueError, httpx.HTTPError, asyncio.TimeoutError):
                    pass
                observed["failure"] = RemoteFailure(response.status_code, response.headers, detail)
                raise observed["failure"]
            if not parse_json:
                return None, dict(response.headers)
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                check(deadline)
                size += len(chunk)
                if size > current_limits().platform_response_bytes:
                    raise ValueError("External JSON response exceeds its size limit.")
                chunks.append(chunk)
            raw = b"".join(chunks)
            return (json.loads(raw) if raw else None), dict(response.headers)

    def close(self):
        if self.loop.is_closed():
            return
        try:
            pending = asyncio.all_tasks(self.loop)
            for task in pending:
                task.cancel()
            if pending:
                self.loop.run_until_complete(
                    asyncio.wait_for(
                        asyncio.gather(*pending, return_exceptions=True), current_limits().platform_close_seconds
                    )
                )
            self.loop.run_until_complete(asyncio.wait_for(self.client.aclose(), current_limits().platform_close_seconds))
        finally:
            self.loop.close()
