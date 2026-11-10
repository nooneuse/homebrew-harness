"""Bounded external JSON transport; no execution or repository state."""

import json
import asyncio
import ssl
from urllib.parse import urlsplit

import httpx
from homebrew_harness.execution.execution_control import check
from homebrew_harness.security.data_boundary import register_private_values
from .contracts import digest


class RemoteFailure(ValueError):
    def __init__(self, status, headers=None):
        self.status = status
        self.headers = dict(headers or {})
        super().__init__("Remote service returned status " + str(status))


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
        check(deadline)
        remaining = deadline.remaining()
        return self.loop.run_until_complete(
            asyncio.wait_for(
                self._request(
                    method,
                    path,
                    deadline,
                    params=params,
                    body=body,
                    headers=headers,
                    parse_json=parse_json,
                ),
                timeout=remaining,
            )
        )

    async def _request(
        self, method, path, deadline, *, params, body, headers, parse_json
    ):
        remaining = deadline.remaining()
        target = self.endpoint if method == "POST" and not path else path
        async with self.client.stream(
            method,
            target,
            params=params,
            json=body,
            headers=headers,
            timeout=min(5, remaining) if remaining is not None else 5,
        ) as response:
            if response.status_code < 200 or response.status_code >= 300:
                raise RemoteFailure(response.status_code, response.headers)
            if not parse_json:
                return None, dict(response.headers)
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                check(deadline)
                size += len(chunk)
                if size > 2_000_000:
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
                        asyncio.gather(*pending, return_exceptions=True), 1
                    )
                )
            self.loop.run_until_complete(asyncio.wait_for(self.client.aclose(), 1))
        finally:
            self.loop.close()
