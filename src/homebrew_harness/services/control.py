"""Authenticated loopback HTTP control server with bounded request ownership."""

import hmac
import http.client
import json
import os
import secrets
import socket
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .http_client import ControlClient as ControlClient
from .protocol import VERSION, MAX_FRAME, validate_envelope


class HeaderReader:
    def __init__(self, stream):
        self.stream = stream
        self.remaining = 16384

    def readline(self, limit=-1):
        raw = self.stream.readline(
            min(self.remaining + 1, limit) if limit >= 0 else self.remaining + 1
        )
        self.remaining -= len(raw)
        if self.remaining < 0:
            raise http.client.LineTooLong("control headers")
        return raw


class ControlServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    block_on_close = False

    def handle_error(self, request, client_address):
        # A disconnected observer is transport failure, not a new business action.
        self.transport_errors = getattr(self, "transport_errors", 0) + 1

    def __init__(self, root, dispatch, *, service_id, port=0):
        from pathlib import Path
        from homebrew_harness.security.safe_files import SafeFile
        from homebrew_harness.security.data_boundary import register_private_values

        self.root = Path(root)
        self.dispatch = dispatch
        self.service_id = service_id
        self.instance = uuid.uuid4().hex
        self.connections = set()
        self.connection_lock = threading.Condition()
        self.active_handlers = 0
        self.slots = threading.BoundedSemaphore(16)
        self.closing = False
        reference = ".harness/control/auth.json"
        with SafeFile(self.root, reference, create_parents=True) as target:
            raw, identity, _ = target.snapshot(max_bytes=65536)
            if raw is None:
                secret = {
                    "version": VERSION,
                    "service_id": service_id,
                    "token": secrets.token_urlsafe(32),
                }
                target.publish(json.dumps(secret).encode(), None, 0o600)
            else:
                with target.open_read() as stream:
                    info = os.fstat(stream.fileno())
                if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                    raise ValueError("Control credential must be owner-only.")
                secret = json.loads(raw)
                if (
                    type(secret.get("version")) is not int
                    or secret.get("version") != VERSION
                    or secret.get("service_id") != service_id
                ):
                    raise ValueError("Control credential belongs to another service.")
        self.token = secret["token"]
        register_private_values(self.token)
        if (
            not isinstance(self.token, str)
            or not 16 <= len(self.token) <= 512
            or not self.token.isascii()
            or any(c.isspace() for c in self.token)
        ):
            raise ValueError("Invalid control credential.")
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                super().setup()
                self.connection.settimeout(5)
                with server.connection_lock:
                    server.connections.add(self.connection)
                    closing = server.closing
                if closing:
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

            def finish(self):
                try:
                    super().finish()
                finally:
                    with server.connection_lock:
                        server.connections.discard(self.connection)

            def log_message(self, *_args):
                pass

            def parse_request(self):
                original = self.rfile
                self.rfile = HeaderReader(original)
                try:
                    return super().parse_request()
                finally:
                    self.rfile = original

            def reply(self, status, result=None, code=None, message=None):
                value = {
                    "version": VERSION,
                    "service_id": server.service_id,
                    "ok": code is None,
                }
                if code is None:
                    value["result"] = result
                else:
                    value.update(code=code, message=message or code)
                raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
                if len(raw) > MAX_FRAME:
                    status = 413
                    raw = json.dumps(
                        {
                            "version": VERSION,
                            "service_id": server.service_id,
                            "ok": False,
                            "code": "control_response",
                            "message": "Control response exceeds its limit; use pagination.",
                        }
                    ).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Connection", "close")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Harness-Instance", server.instance)
                self.end_headers()
                self.wfile.write(raw)
                self.close_connection = True

            def authorize(self):
                authorization = self.headers.get_all("Authorization", [])
                if len(authorization) != 1 or not hmac.compare_digest(
                    authorization[0].encode(),
                    "Bearer ".encode() + server.token.encode(),
                ):
                    self.reply(
                        401,
                        code="control_authentication",
                        message="Control authentication failed.",
                    )
                    return False
                hosts = self.headers.get_all("Host", [])
                if len(hosts) != 1:
                    self.reply(403, code="control_host")
                    return False
                try:
                    host = urlsplit("http://" + hosts[0])
                    port = host.port
                except ValueError:
                    self.reply(403, code="control_host")
                    return False
                if (
                    host.hostname not in {"127.0.0.1", "localhost", "::1"}
                    or host.username
                    or host.password
                    or host.path
                    or port == 0
                ):
                    self.reply(403, code="control_host")
                    return False
                origins = self.headers.get_all("Origin", [])
                if (
                    len(origins) > 1
                    or origins
                    and origins[0] not in {"http://" + hosts[0], "https://" + hosts[0]}
                ):
                    self.reply(403, code="control_origin")
                    return False
                instances = self.headers.get_all("X-Harness-Instance", [])
                if len(instances) > 1 or instances and instances[0] != server.instance:
                    self.reply(
                        409,
                        code="control_identity",
                        message="Control service instance changed.",
                    )
                    return False
                return True

            def do_GET(self):
                if not self.authorize():
                    return
                if self.path == "/v1/health":
                    self.reply(
                        200,
                        result={
                            "service_id": server.service_id,
                            "version": VERSION,
                            "instance": server.instance,
                            "ready": not server.closing,
                        },
                    )
                    return
                if self.path == "/v1/services/" + server.service_id:
                    self.invoke("snapshot", {}, None)
                    return
                self.reply(404, code="control_route")

            def do_POST(self):
                if not self.authorize():
                    return
                if self.path != "/v1/control":
                    self.reply(404, code="control_route")
                    return
                lengths = self.headers.get_all("Content-Length", [])
                if (
                    len(lengths) != 1
                    or not lengths[0].isdigit()
                    or self.headers.get_all("Transfer-Encoding", [])
                ):
                    self.reply(400, code="control_framing")
                    return
                length = int(lengths[0])
                if length > MAX_FRAME:
                    self.reply(413, code="control_size")
                    return
                types = self.headers.get_all("Content-Type", [])
                if (
                    len(types) != 1
                    or types[0].split(";")[0].strip() != "application/json"
                ):
                    self.reply(415, code="control_content_type")
                    return
                try:
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        raise ValueError("Incomplete control request.")

                    def invalid(_value):
                        raise ValueError("Control requires finite JSON.")

                    value = json.loads(raw.decode(), parse_constant=invalid)
                    validate_envelope(value)
                    if value["service_id"] != server.service_id:
                        self.reply(404, code="control_identity")
                        return
                except (ValueError, UnicodeError, RecursionError):
                    self.reply(
                        400, code="invalid_input", message="Invalid control request."
                    )
                    return
                self.invoke(value["method"], value["payload"], value["request_id"])

            def invoke(self, method, payload, request_id):
                try:
                    result = server.dispatch(
                        server.service_id, method, payload, request_id
                    )
                except Exception as error:
                    from homebrew_harness.security.data_boundary import redact

                    code = getattr(error, "code", "invalid_input")
                    self.reply(
                        409 if code == "stale_reference" else 400,
                        code=code,
                        message=redact(str(error)),
                    )
                    return
                self.reply(200, result=result)

            def do_OPTIONS(self):
                if not self.authorize():
                    return
                self.reply(405, code="control_method")

            do_PUT = do_OPTIONS
            do_PATCH = do_OPTIONS
            do_DELETE = do_OPTIONS

        super().__init__(("127.0.0.1", port), Handler)
        endpoint = "http://127.0.0.1:" + str(self.server_port)
        self.descriptor = {
            "version": VERSION,
            "service_id": service_id,
            "endpoint": endpoint,
            "instance": self.instance,
            "token_file": str(self.root / reference),
        }
        try:
            with SafeFile(
                self.root, ".harness/control/endpoint.json", create_parents=True
            ) as target:
                _, identity, _ = target.snapshot()
                target.publish(json.dumps(self.descriptor).encode(), identity, 0o600)
        except BaseException:
            self.server_close()
            raise

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.settimeout(1)
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
            except OSError:
                pass
            self.shutdown_request(request)
            return
        with self.connection_lock:
            self.active_handlers += 1
        try:
            super().process_request(request, client_address)
        except BaseException:
            with self.connection_lock:
                self.active_handlers -= 1
                self.connection_lock.notify_all()
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()
            with self.connection_lock:
                self.active_handlers -= 1
                self.connection_lock.notify_all()

    def remove_descriptor(self):
        from homebrew_harness.security.safe_files import SafeFile

        with SafeFile(self.root, ".harness/control/endpoint.json") as target:
            raw, identity, _ = target.snapshot(max_bytes=65536)
            if raw is not None and json.loads(raw).get("instance") == self.instance:
                target.unlink(identity)

    def close(self):
        self.closing = True
        self.shutdown()
        self.server_close()
        with self.connection_lock:
            connections = list(self.connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        with self.connection_lock:
            while self.active_handlers:
                self.connection_lock.wait(0.05)
        self.remove_descriptor()
