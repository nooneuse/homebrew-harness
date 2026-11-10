"""HTTP authentication, remote isolation, replay and final resource boundaries."""

import io
import json
import socket
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

import httpx

from homebrew_harness import RemoteService, ControlConnection
from homebrew_harness.contracts import SDKError
from homebrew_harness.services.http_client import ControlClient, descriptor_path
from homebrew_harness.services.cli import attach, execute
from homebrew_harness.cli import parser
from tests.services import test_runtime as fixture
from tests.support.clients import reply, call
from tests.support.paths import SRC_ROOT


class HTTPControl(unittest.TestCase):
    setUp = fixture.ServiceRuntimeTests.setUp
    host = fixture.ServiceRuntimeTests.host
    spec = fixture.ServiceRuntimeTests.spec

    def service(self, replies=()):
        host, model = self.host(list(replies))
        service = host.services.start(self.spec())
        self.addCleanup(service.close)
        credentials = ControlClient(self.control).credentials()
        return host, model, service, credentials

    def connection_file(self, credentials):
        secret = self.base / "client-secret.json"
        secret.write_text(
            json.dumps(
                {
                    "version": 1,
                    "service_id": credentials.service_id,
                    "token": credentials.token,
                }
            )
        )
        secret.chmod(0o600)
        config = self.base / "client.json"
        config.write_text(
            json.dumps(
                {
                    "version": 1,
                    "service_id": credentials.service_id,
                    "endpoint": credentials.endpoint,
                    "token_file": str(secret),
                }
            )
        )
        return config

    def test_authentication_is_required_for_queries_and_commands(self):
        _, _, service, c = self.service()
        body = {
            "version": 1,
            "service_id": service.id,
            "method": "snapshot",
            "payload": {},
            "request_id": None,
        }
        with httpx.Client(trust_env=False) as client:
            for headers in ({}, {"Authorization": "Bearer wrong"}):
                self.assertEqual(
                    client.post(
                        c.endpoint + "/v1/control", json=body, headers=headers
                    ).status_code,
                    401,
                )
                self.assertEqual(
                    client.get(c.endpoint + "/v1/health", headers=headers).status_code,
                    401,
                )
            headers = {"Authorization": "Bearer " + c.token}
            result = client.get(
                c.endpoint + "/v1/services/" + service.id, headers=headers
            )
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["result"]["id"], service.id)
            self.assertEqual(
                client.get(c.endpoint + "/v1/health", headers=headers).json()["result"][
                    "version"
                ],
                1,
            )

    def test_host_origin_and_unknown_methods_do_not_mutate_work(self):
        _, _, service, c = self.service()
        body = {
            "version": 1,
            "service_id": service.id,
            "method": "submit",
            "payload": {"text": "Never execute"},
            "request_id": "one",
        }
        with httpx.Client(trust_env=False) as client:
            base = {"Authorization": "Bearer " + c.token}
            self.assertEqual(
                client.post(
                    c.endpoint + "/v1/control",
                    json=body,
                    headers={**base, "Host": "attacker.invalid"},
                ).status_code,
                403,
            )
            self.assertEqual(
                client.post(
                    c.endpoint + "/v1/control",
                    json=body,
                    headers={**base, "Origin": "https://attacker.invalid"},
                ).status_code,
                403,
            )
            self.assertEqual(
                client.post(
                    c.endpoint + "/v1/control",
                    json={**body, "method": "exec"},
                    headers=base,
                ).status_code,
                400,
            )
            self.assertEqual(
                client.options(c.endpoint + "/v1/control", headers=base).status_code,
                405,
            )
        self.assertEqual(service.work.list()["items"], [])

    def test_instance_and_service_identity_are_checked(self):
        _, _, service, c = self.service()
        wrong = ControlClient(
            connection=ControlConnection(
                c.endpoint, c.service_id, c.token, "old-instance"
            )
        )
        self.addCleanup(wrong.close)
        with self.assertRaises(SDKError) as error:
            wrong.call(service.id, "snapshot")
        self.assertEqual(error.exception.code, "control_identity")
        other = ControlClient(
            connection=ControlConnection(c.endpoint, "svc_" + "f" * 32, c.token)
        )
        self.addCleanup(other.close)
        with self.assertRaises(SDKError):
            other.call("svc_" + "f" * 32, "snapshot")

    def test_restart_keeps_credential_and_changes_instance(self):
        host, _, owner, old = self.service()
        owner.stop()
        self.assertTrue(owner.wait_closed(10))
        restarted = host.services.start(identity=owner.id)
        self.addCleanup(restarted.close)
        current = ControlClient(self.control).credentials()
        self.assertEqual(current.service_id, old.service_id)
        self.assertEqual(current.token, old.token)
        self.assertNotEqual(current.instance, old.instance)
        with RemoteService.from_root(self.control) as view:
            self.assertEqual(view.snapshot().state, "running")

    def test_invalid_client_payload_is_rejected_before_dispatch(self):
        _, _, owner, c = self.service()
        client = ControlClient(connection=c)
        self.addCleanup(client.close)
        for payload in ([], {"actor": "owner"}):
            with self.assertRaises(SDKError) as error:
                client.call(owner.id, "snapshot", payload)
            self.assertEqual(error.exception.code, "invalid_input")
        self.assertEqual(owner.work.list()["items"], [])

    def test_remote_client_uses_no_service_store_or_control_directory(self):
        _, model, owner, c = self.service([reply("Done")])
        config = self.connection_file(c)
        with patch(
            "homebrew_harness.services.store.ServiceStore",
            side_effect=AssertionError("Client used local state"),
        ):
            with RemoteService.from_file(config, control=True) as client:
                self.assertEqual(client.snapshot().state, "running")
                work = client.work.submit("Work", request_id="one")
                self.assertEqual(work.wait(20).snapshot.state, "review")
                self.assertEqual(work.result().text, "Done")
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(owner.snapshot().state, "running")

    def test_cli_connection_and_text_ui_share_remote_api(self):
        _, _, owner, c = self.service()
        config = self.connection_file(c)
        output = io.StringIO()
        args = parser().parse_args(
            [
                "service",
                "--connection",
                str(config),
                "status",
                owner.id,
                "--cwd",
                str(self.base / "absent"),
            ]
        )
        with (
            patch(
                "homebrew_harness.api.Harness",
                side_effect=AssertionError("Remote CLI constructed a local host"),
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(execute(args, None), 0)
        self.assertFalse((self.base / "absent").exists())
        self.assertIn(owner.id, output.getvalue())
        with RemoteService.from_file(config, control=True) as client:
            lines = iter(["status", "exit"])
            with redirect_stdout(io.StringIO()):
                attach(client, input_fn=lambda _prompt: next(lines))
        self.assertEqual(owner.snapshot().state, "running")

    def test_mutation_replay_and_lost_response_preserve_the_original_work(self):
        _, model, owner, c = self.service([reply("Done")])
        client = ControlClient(connection=c)
        self.addCleanup(client.close)
        payload = {"text": "Work", "goal": None, "priority": 0}
        first = client.call(owner.id, "submit", payload, "stable")
        self.assertEqual(first, client.call(owner.id, "submit", payload, "stable"))
        with self.assertRaises(SDKError):
            client.call(owner.id, "submit", {**payload, "text": "Other"}, "stable")
        self.assertIsNotNone(client.call(owner.id, "receipt", {"request_id": "stable"}))
        self.assertEqual(owner.work.get(first["id"]).wait(20).snapshot.state, "review")
        self.assertEqual(len(model.requests), 1)

    def test_connection_aborted_after_work_acceptance_is_recoverable_by_receipt(self):
        _, model, owner, c = self.service([reply("Done")])
        client = ControlClient(connection=c)
        self.addCleanup(client.close)
        handler = owner.runtime.server.RequestHandlerClass
        original = handler.reply

        def lose_response(request, status, result=None, code=None, message=None):
            if (
                result
                and result.get("accepted")
                and str(result.get("id", "")).startswith("work_")
            ):
                request.connection.shutdown(socket.SHUT_RDWR)
                request.close_connection = True
                return
            return original(request, status, result, code, message)

        payload = {"text": "Work", "goal": None, "priority": 0}
        with (
            patch.object(handler, "reply", lose_response),
            self.assertRaises(SDKError) as error,
        ):
            client.call(owner.id, "submit", payload, "lost")
        self.assertEqual(error.exception.code, "control_unknown")
        self.assertEqual(error.exception.details["request_id"], "lost")
        receipt = client.call(owner.id, "receipt", {"request_id": "lost"})
        replay = client.call(owner.id, "submit", payload, "lost")
        self.assertEqual(receipt["result"], replay)
        self.assertEqual(owner.work.get(replay["id"]).wait(20).snapshot.state, "review")
        self.assertEqual(len(model.requests), 1)

    def test_http_body_framing_and_duplicate_authorization_are_rejected(self):
        import http.client
        from urllib.parse import urlsplit

        _, _, owner, c = self.service()
        port = urlsplit(c.endpoint).port
        variants = [
            ("Content-Length: 2\r\nContent-Length: 3\r\n", 400),
            ("Content-Length: 2\r\nTransfer-Encoding: chunked\r\n", 400),
        ]
        for extra, expected in variants:
            with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
                raw = (
                    "POST /v1/control HTTP/1.1\r\nHost: localhost:"
                    + str(port)
                    + "\r\nAuthorization: Bearer "
                    + c.token
                    + "\r\nContent-Type: application/json\r\n"
                    + extra
                    + "\r\n{}"
                )
                connection.sendall(raw.encode())
                response = http.client.HTTPResponse(connection)
                response.begin()
                self.assertEqual(response.status, expected)
                response.read()
        with httpx.Client(trust_env=False) as client:
            result = client.post(
                c.endpoint + "/v1/control",
                content=b"{}",
                headers=[
                    ("Authorization", "Bearer " + c.token),
                    ("Authorization", "Bearer " + c.token),
                    ("Content-Type", "application/json"),
                ],
            )
            self.assertEqual(result.status_code, 401)
        self.assertEqual(owner.work.list()["items"], [])

    def test_private_local_descriptor_and_token_file_permissions_are_checked(self):
        _, _, owner, c = self.service()
        path = descriptor_path(self.control)
        value = json.loads(path.read_text())
        secret = Path(value["token_file"])
        secret.chmod(0o644)
        try:
            with self.assertRaises(ValueError):
                ControlClient(self.control).credentials()
        finally:
            secret.chmod(0o600)

    def test_external_token_file_requires_private_permissions(self):
        _, _, _, c = self.service()
        config = self.connection_file(c)
        secret = self.base / "client-secret.json"
        secret.chmod(0o644)
        try:
            with self.assertRaises(ValueError):
                RemoteService.from_file(config)
        finally:
            secret.chmod(0o600)

    def test_cli_lost_response_displays_the_replay_reference(self):
        _, _, owner, c = self.service()
        config = self.connection_file(c)
        args = parser().parse_args(
            [
                "service",
                "--connection",
                str(config),
                "work",
                owner.id,
                "submit",
                "Work",
                "--request-id",
                "cli-lost",
            ]
        )
        original = ControlClient.call

        def unavailable(client, identity, method, payload=None, request_id=None, **kw):
            if method == "submit":
                raise SDKError(
                    "No acknowledgment",
                    code="control_unknown",
                    details={"request_id": request_id, "method": method},
                )
            return original(client, identity, method, payload, request_id, **kw)

        output = io.StringIO()
        with patch.object(ControlClient, "call", unavailable), redirect_stderr(output):
            self.assertEqual(execute(args, None), 1)
        self.assertIn("cli-lost", output.getvalue())
        self.assertIn("control_unknown", output.getvalue())
        with RemoteService(c, control=True) as view:
            lines = iter(["Work", "exit"])
            with (
                patch.object(ControlClient, "call", unavailable),
                redirect_stdout(output),
            ):
                attach(view, input_fn=lambda _prompt: next(lines))
        self.assertIn("control_", output.getvalue())

    def test_cli_submission_returns_acceptance_without_a_second_query(self):
        _, _, owner, c = self.service()
        owner.pause()
        config = self.connection_file(c)
        args = parser().parse_args(
            [
                "service",
                "--connection",
                str(config),
                "work",
                owner.id,
                "submit",
                "Work",
                "--request-id",
                "accepted-cli",
            ]
        )
        output = io.StringIO()
        original = ControlClient.call

        def no_followup(client, identity, method, *args, **kw):
            self.assertEqual(method, "submit")
            return original(client, identity, method, *args, **kw)

        with patch.object(ControlClient, "call", no_followup), redirect_stdout(output):
            self.assertEqual(execute(args, None), 0)
        result = json.loads(output.getvalue())
        self.assertTrue(result["accepted"])
        self.assertEqual(result["request_id"], "accepted-cli")
        self.assertEqual(owner.work.list()["items"][0]["id"], result["id"])

    def test_connection_loss_is_not_a_stopped_state_or_local_fallback(self):
        _, _, owner, c = self.service()
        client = RemoteService(c, control=True)
        self.addCleanup(client.close)
        owner.stop()
        self.assertTrue(owner.wait_closed(10))
        with self.assertRaises(SDKError) as error:
            client.snapshot()
        self.assertEqual(error.exception.code, "control_unknown")
        self.assertEqual(owner.snapshot().state, "stopped")

    def test_observer_host_closes_connections_without_stopping_the_owner(self):
        from homebrew_harness import Harness

        _, _, owner, _ = self.service()
        with Harness(workdir=self.control) as observer:
            readonly = observer.services.get(owner.id)
            writable = observer.services.get(owner.id, control=True)
            self.assertIs(readonly, observer.services.get(owner.id))
            self.assertIsNot(readonly, writable)
            self.assertFalse(readonly.control)
            readonly.query("snapshot")
            writable.query("snapshot")
        self.assertTrue(readonly.client.closed)
        self.assertTrue(writable.client.closed)
        self.assertTrue(readonly.client._client.is_closed)
        self.assertEqual(owner.snapshot().state, "running")

    def test_remote_view_does_not_close_a_borrowed_client(self):
        _, _, owner, c = self.service()
        shared = ControlClient(connection=c)
        self.addCleanup(shared.close)
        with RemoteService(shared) as view:
            self.assertEqual(view.snapshot().state, "running")
        self.assertFalse(shared.closed)
        self.assertEqual(shared.call(owner.id, "snapshot")["state"], "running")
        with RemoteService(c) as view:
            self.assertEqual(view.snapshot().state, "running")
        self.assertTrue(view.client.closed)

    def test_native_attention_and_stale_decisions_use_http(self):
        from tests.runtime.test_interaction import eventually

        _, _, _, c = self.service(
            [
                reply(
                    calls=[
                        call(
                            "ask",
                            name="request_attention",
                            arguments='{"question":"What behavior?","reason":"Clarify","impact":"Result"}',
                        )
                    ]
                ),
                reply("Done"),
            ]
        )
        with RemoteService(c, control=True) as service:
            work = service.work.submit("Ask before work", request_id="attention")
            request = eventually(lambda: work.requests() and work.requests()[0], 15)
            self.assertEqual(request.kind, "attention")
            self.assertEqual(request.ref.target.owner_id, service.id)
            with self.assertRaises(SDKError):
                service.decide(request, "approve", decision_id="wrong-kind")
            with self.assertRaises(SDKError):
                service.decide(
                    replace(request.ref, revision=request.ref.revision + 1),
                    "answer",
                    text="Changed",
                    decision_id="stale",
                )
            service.decide(
                request, "answer", text="Keep behavior", decision_id="answer"
            )
            eventually(lambda: work.snapshot().state == "review", 20)

    def test_exact_native_bash_approval_is_replayed_through_http(self):
        from tests.runtime.test_interaction import eventually

        host, model = self.host(
            [
                reply(
                    calls=[
                        call(
                            "shell",
                            name="bash",
                            arguments='{"command":"printf approved"}',
                        )
                    ]
                ),
                reply("Done"),
            ],
            managed_bash=True,
        )
        owner = host.services.start(self.spec(run_defaults={"tools": ["bash"]}))
        self.addCleanup(owner.close)
        with RemoteService.from_root(self.control, control=True) as service:
            work = service.work.submit("Execute only the approved command")
            request = eventually(lambda: work.requests() and work.requests()[0], 15)
            self.assertEqual(request.kind, "tool")
            model.responses.appendleft(
                reply(
                    calls=[
                        call(
                            "execute",
                            name="execute_approved",
                            arguments='{"request_id":"' + request.ref.id + '"}',
                        )
                    ]
                ).model_dump(exclude_none=True)
            )
            accepted = service.decide(request, "approve", decision_id="exact")
            self.assertEqual(
                accepted, service.decide(request, "approve", decision_id="exact")
            )
            eventually(lambda: work.snapshot().state == "review", 20)

    def test_connection_config_has_no_inline_secret_and_rejects_remote_plain_http(self):
        with self.assertRaises(ValueError):
            ControlConnection(
                "http://example.invalid", "svc_" + "a" * 32, "secret-value-long-enough"
            )
        with self.assertRaises(ValueError):
            ControlConnection(
                "https://user:password@example.invalid",
                "svc_" + "a" * 32,
                "secret-value-long-enough",
            )
        with self.assertRaises(ValueError):
            ControlConnection(
                "http://localhost", "svc_" + "a" * 32, "bad\r\nheader-value"
            )

    def test_idle_http_connection_is_closed_before_service_releases_ownership(self):
        _, _, owner, c = self.service()
        from urllib.parse import urlsplit

        connection = socket.create_connection(
            ("127.0.0.1", urlsplit(c.endpoint).port), timeout=2
        )
        try:
            from tests.runtime.test_interaction import eventually

            eventually(lambda: owner.runtime.server.active_handlers > 0)
            owner.stop()
            self.assertTrue(owner.wait_closed(10))
            self.assertEqual(owner.runtime.server.active_handlers, 0)
        finally:
            connection.close()

    def test_handler_starting_during_shutdown_closes_its_socket(self):
        from urllib.parse import urlsplit
        from tests.runtime.test_interaction import eventually

        _, _, owner, c = self.service()
        handler = owner.runtime.server.RequestHandlerClass
        original = handler.setup
        entered, release = threading.Event(), threading.Event()

        def delayed(request):
            entered.set()
            release.wait(10)
            original(request)

        with patch.object(handler, "setup", delayed):
            connection = socket.create_connection(
                ("127.0.0.1", urlsplit(c.endpoint).port), timeout=2
            )
            try:
                self.assertTrue(entered.wait(2))
                owner.stop()
                eventually(lambda: owner.runtime.server.closing)
                release.set()
                self.assertEqual(connection.recv(1), b"")
                self.assertTrue(owner.wait_closed(10))
                self.assertEqual(owner.runtime.server.active_handlers, 0)
            finally:
                release.set()
                connection.close()

    def test_client_import_is_independent_of_posix_execution(self):
        code = """import builtins,sys
sys.path.insert(0,SOURCE)
original=builtins.__import__
def guarded(name,*args,**kw):
 if name=='fcntl':raise AssertionError('POSIX backend imported')
 return original(name,*args,**kw)
builtins.__import__=guarded
from homebrew_harness import RemoteService,ControlConnection
assert not any(name.startswith(('homebrew_harness.api.core','homebrew_harness.storage','homebrew_harness.execution')) for name in sys.modules)
print('portable client imports')
""".replace("SOURCE", repr(str(SRC_ROOT)))
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", code],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
