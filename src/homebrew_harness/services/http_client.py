"""Portable authenticated control client; no execution or database imports."""

import ipaddress
import json
import math
import re
import os
import stat
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from homebrew_harness.contracts import SDKError, ExecutionRef
from homebrew_harness.security.data_boundary import register_private_values
from .protocol import VERSION, MAX_FRAME, COMMAND_METHODS, SERVICE_ID, validate_envelope


def read_json(path, *, private=False):
    path = Path(path).expanduser()
    fd = None
    try:
        if path.is_symlink():
            raise ValueError("Control configuration cannot be a symlink.")
        fd = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
            raise ValueError("Control configuration must be a bounded regular file.")
        if (
            private
            and os.name == "posix"
            and (info.st_uid != os.geteuid() or info.st_mode & 0o077)
        ):
            raise ValueError("Control configuration must be owner-only.")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("Control configuration exceeds 64 KiB.")
        return json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SDKError(
            "Control configuration is unavailable or invalid.",
            code="control_configuration",
        ) from error
    finally:
        if fd is not None:
            os.close(fd)


def validate_endpoint(endpoint):
    if (
        not isinstance(endpoint, str)
        or len(endpoint) > 8192
        or any(ord(c) < 33 for c in endpoint)
    ):
        raise ValueError("Invalid control endpoint.")
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Control endpoints require HTTP(S), without embedded credentials or query."
        )
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("Invalid control endpoint port.") from error
    if port == 0:
        raise ValueError("Invalid control endpoint port.")
    if parsed.scheme == "http":
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname == "localhost"
        if not loopback:
            raise ValueError(
                "Plain HTTP control is limited to loopback; use HTTPS or an SSH tunnel."
            )
    return endpoint.rstrip("/")


@dataclass(frozen=True)
class ControlConnection:
    endpoint: str
    service_id: str
    token: str = field(repr=False)
    instance: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "endpoint", validate_endpoint(self.endpoint))
        if not isinstance(self.service_id, str) or not SERVICE_ID.fullmatch(
            self.service_id
        ):
            raise ValueError("Invalid service reference.")
        if (
            not isinstance(self.token, str)
            or not 16 <= len(self.token) <= 512
            or not self.token.isascii()
            or any(c.isspace() or ord(c) < 33 for c in self.token)
        ):
            raise ValueError("Invalid control credential.")
        if self.instance is not None and (
            not isinstance(self.instance, str)
            or not self.instance
            or len(self.instance) > 128
        ):
            raise ValueError("Invalid control instance.")
        register_private_values(self.token)

    @classmethod
    def from_file(cls, path, *, private=False):
        path = Path(path).expanduser()
        value = read_json(path, private=private)
        if (
            not isinstance(value, dict)
            or set(value)
            - {
                "version",
                "endpoint",
                "service_id",
                "token_file",
                "token_env",
                "instance",
            }
            or type(value.get("version")) is not int
            or value["version"] != VERSION
        ):
            raise ValueError("Invalid control connection configuration.")
        if bool(value.get("token_file")) == bool(value.get("token_env")):
            raise ValueError("Choose token_file or token_env, exclusively.")
        if value.get("token_file"):
            token_path = Path(value["token_file"]).expanduser()
            if not token_path.is_absolute():
                token_path = path.parent / token_path
            secret = read_json(token_path, private=True)
            if (
                not isinstance(secret, dict)
                or secret.get("service_id") != value["service_id"]
                or type(secret.get("version")) is not int
                or secret["version"] != VERSION
            ):
                raise ValueError("Control credential belongs to another service.")
            token = secret.get("token")
        else:
            name = value["token_env"]
            if not isinstance(name, str) or not re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]{0,99}", name
            ):
                raise ValueError("Invalid control credential environment reference.")
            token = os.environ.get(name)
        return cls(value["endpoint"], value["service_id"], token, value.get("instance"))


def descriptor_path(root):
    return Path(root) / ".harness/control/endpoint.json"


class ControlClient:
    def __init__(self, root=None, *, connection=None):
        if (root is None) == (connection is None):
            raise ValueError("Choose a local descriptor or ControlConnection.")
        self.root = Path(root) if root is not None else None
        self.connection = connection
        self._client = None
        self._lock = threading.Lock()
        self.closed = False

    @classmethod
    def from_file(cls, path):
        return cls(connection=ControlConnection.from_file(path))

    def credentials(self):
        return (
            self.connection
            if self.connection is not None
            else ControlConnection.from_file(descriptor_path(self.root), private=True)
        )

    def _http(self):
        with self._lock:
            if self.closed:
                raise SDKError("Control client is closed.", code="client_closed")
            if self._client is None:
                self._client = httpx.Client(trust_env=False, follow_redirects=False)
            return self._client

    def call(self, service_id, method, payload=None, request_id=None, timeout=10):
        if (
            type(timeout) not in {int, float}
            or not math.isfinite(timeout)
            or not 0 < timeout <= 60
        ):
            raise ValueError("Invalid control timeout.")
        if method in COMMAND_METHODS and request_id is None:
            request_id = "control_" + uuid.uuid4().hex
        value = {
            "version": VERSION,
            "service_id": service_id,
            "method": method,
            "payload": payload if payload is not None else {},
            "request_id": request_id,
        }
        details = {"request_id": request_id, "method": method}
        try:
            validate_envelope(value)
        except ValueError as error:
            raise SDKError(
                "Invalid control method, fields or request reference.",
                code="invalid_input",
                ref=ExecutionRef("service", service_id),
                details=details,
            ) from error
        try:
            connection = self.credentials()
        except SDKError as error:
            error.details.update(details)
            raise
        if connection.service_id != service_id:
            raise SDKError(
                "Control connection targets another service.", code="control_identity"
            )
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        if len(raw) > MAX_FRAME:
            raise ValueError("Control request exceeds its body limit.")
        headers = {
            "Authorization": "Bearer " + connection.token,
            "Content-Type": "application/json",
        }
        if connection.instance is not None:
            headers["X-Harness-Instance"] = connection.instance
        try:
            with self._http().stream(
                "POST",
                connection.endpoint + "/v1/control",
                content=raw,
                headers=headers,
                timeout=timeout,
            ) as response:
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_FRAME:
                        raise SDKError(
                            "Control response exceeds its limit.",
                            code="control_response",
                            details=details,
                        )
                status = response.status_code
                if (
                    connection.instance is not None
                    and response.headers.get("X-Harness-Instance")
                    != connection.instance
                ):
                    raise SDKError(
                        "Control service instance changed.",
                        code="control_identity",
                        details=details,
                    )
        except (httpx.HTTPError, KeyboardInterrupt) as error:
            raise SDKError(
                "Control acknowledgment is unavailable; inspect or replay the same request_id.",
                code="control_unknown",
                ref=ExecutionRef("service", service_id),
                details=details,
            ) from error
        if status == 503 and not body:
            raise SDKError(
                "Control service is busy.", code="control_busy", details=details
            )
        try:
            result = json.loads(body)
        except (ValueError, UnicodeError) as error:
            raise SDKError(
                "Control acknowledgment is invalid; inspect the same request_id.",
                code="control_unknown",
                details=details,
            ) from error
        if (
            not isinstance(result, dict)
            or result.get("service_id") != service_id
            or type(result.get("version")) is not int
            or result.get("version") != VERSION
            or type(result.get("ok")) is not bool
        ):
            raise SDKError(
                "Control response belongs to another service or protocol.",
                code="control_identity",
                details=details,
            )
        if not result.get("ok"):
            raise SDKError(
                result.get("message", "Control command failed."),
                code=result.get("code", "control_error"),
                ref=ExecutionRef("service", service_id),
                details={**details, "status": status},
            )
        if status != 200 or "result" not in result:
            raise SDKError(
                "Control acknowledgment is incomplete.",
                code="control_unknown",
                details=details,
            )
        return result["result"]

    def close(self):
        with self._lock:
            self.closed = True
            if self._client is not None:
                self._client.close()
