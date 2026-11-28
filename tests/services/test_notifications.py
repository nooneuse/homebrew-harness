"""Webhook payloads, delivery backoff and the authenticated inbox controls."""

import io
import json
import logging
import socket
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

from homebrew_harness import Harness, HostConfig, RemoteService, ServiceSpec
from homebrew_harness.cli import main
from homebrew_harness.contracts import SDKError
from homebrew_harness.security.data_boundary import redact
from homebrew_harness.services.cli import attach
from homebrew_harness.services.notifications import open_notification_transport
from homebrew_harness.services.protocol import fingerprint
from tests.runtime.test_interaction import eventually
from tests.services import test_maintenance as fixture


class Webhook:
    def __init__(self, port=0):
        self.calls = []
        self.bodies = []
        self.status = 204
        self.retry_after = None
        self.wait = threading.Event()
        self.wait.set()
        webhook = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                webhook.bodies.append(raw)
                webhook.calls.append((self.path, dict(self.headers), json.loads(raw)))
                webhook.wait.wait(5)
                detail = json.dumps({"message": f"private: {webhook.url} {self.headers.get('Authorization')}"}).encode()
                status = webhook.status
                self.send_response(status)
                if webhook.retry_after is not None:
                    self.send_header("Retry-After", str(webhook.retry_after))
                self.end_headers()
                if status != 204:
                    try:
                        self.wfile.write(detail)
                    except OSError:
                        pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = self.server.server_port
        self.url = f"http://127.0.0.1:{self.port}/private/digest"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()

    def close(self):
        self.wait.set()
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()


class NotificationsTests(unittest.TestCase):
    setUp = fixture.MaintenanceTests.setUp
    token = "webhook-fixture-token"

    def webhook(self, port=0):
        server = Webhook(port)
        self.addCleanup(server.close)
        return server

    def start(self, *, profile="repository_maintenance", notifications=True, **options):
        spec = ServiceSpec(
            str(self.workspace), profile=profile,
            maintenance={"scopes": {"code": {"write_paths": ["src"]}}} if profile != "manual" else {},
            notifications={"url_env": "WEBHOOK_URL", "token_env": "WEBHOOK_TOKEN", "mode": "local",
                           "cooldown_seconds": 0, **options} if notifications else {},
        )
        service = self.host.services.start(spec)
        self.addCleanup(self.stop, service)
        return service

    def stop(self, service):
        if not service.wait_closed(0):
            service.stop()
            self.assertTrue(service.wait_closed(8))

    def configure(self, url, *, file=None):
        text = f"WEBHOOK_URL={url}\nWEBHOOK_TOKEN={self.token}\n"
        if file is not None:
            file.write_text(text)
            file.chmod(0o600)
        else:
            with self.env.open("a") as stream:
                stream.write(text)

    def seed(self, service, identity, *, expires_at=None, plan=None):
        runtime = service.runtime
        store = runtime.maintenance.store
        proposal = {**self.peer.triage, "disposition": "accept", "plan": plan or identity}
        with runtime.lock, store.db.write() as conn:
            work = runtime.store.enqueue(conn, service.id, identity, identity)
            work.update(triage=proposal, triage_revision=0, state="proposed")
            store.put(conn, "work", work)
            request = store.work_request(conn, work, proposal)
            if expires_at is not None:
                store.put(conn, "decisions", {**request, "expires_at": expires_at})
        return request["id"]

    def send_count(self, service, count, seconds=8):
        return eventually(lambda: (status if (status := service.notification_status())["sent_count"] >= count else None), seconds)

    def cli(self, service, *args):
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["service", "--cwd", str(self.control), "inbox", service.id, *args]), 0)
        return json.loads(output.getvalue())

    def test_schedule_sends_same_inbox_and_skips_empty_or_unchanged_within_period(self):
        webhook = self.webhook()
        self.configure(webhook.url)
        service = self.start(every_seconds=1.4)
        self.seed(service, "Earlier expired item", expires_at=time.time() - 1)
        time.sleep(0.3)
        self.assertEqual(webhook.calls, [])
        self.assertIsNone(service.notification_status()["last_sent_at"])
        pending = self.seed(service, "Current item")
        first = self.send_count(service, 1)
        path, headers, payload = webhook.calls[0]
        self.assertEqual(path, "/private/digest")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Authorization"], "Bearer " + self.token)
        self.assertEqual(headers["Idempotency-Key"], first["last_delivery_id"])
        self.assertEqual(payload["event"], "service.inbox.digest")
        self.assertEqual(payload["service_id"], service.id)
        self.assertEqual(payload["trigger"], "schedule")
        self.assertEqual(payload["items"][0]["ref"]["id"], pending)
        self.assertEqual(payload["counts"]["expired"], 1)
        self.assertIsInstance(payload["generated_at"], float)
        inbox = service.inbox(include_expired=True)
        self.assertEqual({key: payload[key] for key in inbox}, inbox)
        time.sleep(0.35)
        self.assertEqual(len(webhook.calls), 1)
        second = self.send_count(service, 2)
        self.assertEqual(second["last_digest"], first["last_digest"])
        self.assertGreaterEqual(second["last_sent_at"] - first["last_sent_at"], 1.4)
        self.assertNotEqual(second["last_delivery_id"], first["last_delivery_id"])

    def test_threshold_changed_digest_respects_cooldown_pause_and_restart(self):
        webhook = self.webhook()
        self.configure(webhook.url)
        service = self.start(every_seconds=3600, min_pending=2, cooldown_seconds=1.2)
        first_id = self.seed(service, "First")
        first = self.send_count(service, 1)
        self.assertEqual(first["last_trigger"], "schedule")
        service.pause("all")
        second_id = self.seed(service, "Second")
        time.sleep(0.25)
        self.assertEqual(len(webhook.calls), 1)
        service.resume()
        time.sleep(0.25)
        self.assertEqual(len(webhook.calls), 1)  # cooldown, despite a changed digest above the threshold
        second = self.send_count(service, 2)
        self.assertEqual(second["last_trigger"], "threshold")
        self.assertEqual(second["last_digest"], fingerprint([(r, 0, "pending") for r in (first_id, second_id)]))
        self.assertGreaterEqual(second["last_sent_at"] - first["last_sent_at"], 1.2)
        transport = service.runtime.notifications.transport
        self.stop(service)
        self.assertTrue(transport.loop.is_closed())
        self.assertFalse(service.runtime.notifications.thread.is_alive())
        resumed = self.host.services.start(identity=service.id)
        self.addCleanup(self.stop, resumed)
        self.assertEqual(resumed.notification_status(), second)
        time.sleep(1.3)
        self.assertEqual(len(webhook.calls), 2)  # unchanged digest is not a threshold trigger after restart

    def test_attached_restart_uses_saved_webhook_environment(self):
        saved = self.webhook()
        other = self.webhook()
        self.configure(saved.url)
        service = self.start(profile="manual")
        self.stop(service)
        attach_env = self.base / "attach.env"
        attach_env.write_text(
            self.env.read_text().replace(saved.url, other.url).replace(self.token, "other-webhook-token")
        )
        with Harness(workdir=self.control, config=HostConfig.from_files(env_file=attach_env)) as host:
            resumed = host.services.start(identity=service.id)
            try:
                self.assertTrue(resumed.send_notification(force=True, request_id="saved-webhook")["sent"])
                self.assertEqual(len(saved.calls), 1)
                self.assertEqual(saved.calls[0][1]["Authorization"], "Bearer " + self.token)
                self.assertEqual(other.calls, [])
            finally:
                self.stop(resumed)

    def test_backoff_for_500_and_429_and_recovery_does_not_echo_secrets(self):
        webhook = self.webhook()
        webhook.status = 500
        self.configure(webhook.url)
        service = self.start()
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        logger = logging.getLogger("httpx")
        handler, previous_level = Capture(), logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        self.addCleanup(logger.setLevel, previous_level)
        self.addCleanup(logger.removeHandler, handler)
        self.seed(service, "Review incoming proposal", plan=f"Avoid exposing {self.token} and {webhook.url}")
        first = eventually(lambda: (status if (status := service.notification_status())["attempts"] == 1 else None))
        self.assertEqual(first["last_error"], "Webhook returned HTTP 500.")
        self.assertEqual(first["sent_count"], 0)
        self.assertGreater(first["next_retry_at"], time.time())
        webhook.status, webhook.retry_after = 429, 2.5
        time.sleep(0.2)
        self.assertEqual(len(webhook.calls), 1)
        limited = eventually(lambda: (status if (status := service.notification_status())["attempts"] == 2 else None))
        self.assertEqual(limited["last_error"], "Webhook returned HTTP 429.")
        self.assertGreater(limited["next_retry_at"], time.time() + 2)
        webhook.status, webhook.retry_after = 204, None
        before = service.snapshot().details["heartbeat_at"]
        eventually(lambda: service.snapshot().details["heartbeat_at"] > before)
        self.assertEqual(service.snapshot().state, "running")
        done = self.send_count(service, 1)
        self.assertEqual(done["attempts"], 0)
        self.assertIsNone(done["next_retry_at"])
        self.assertIsNone(done["last_error"])
        self.assertEqual(len(webhook.calls), 3)
        self.assertEqual({call[1]["Idempotency-Key"] for call in webhook.calls}, {done["last_delivery_id"]})
        data = str((first, limited, done, service.snapshot().data(), records, [call[2] for call in webhook.calls]))
        for private in (webhook.url, self.token):
            self.assertNotIn(private, data)
            self.assertEqual(redact(private), "[redacted]")

    def test_retry_keeps_wire_body_and_backoff_when_outcome_writes_fail(self):
        webhook = self.webhook()
        webhook.status, webhook.retry_after = 429, 30
        self.configure(webhook.url)
        # Drive the worker directly with a controlled clock while using real POSTs.
        with patch("homebrew_harness.services.notifications.ServiceNotifications.start"):
            service = self.start(every_seconds=3600)
        self.seed(service, "First pending item")
        worker = service.runtime.notifications
        db = worker.store.db
        put = db._put

        def fail_outcome(conn, section, key, value):
            if section == "service_notifications":
                raise OSError("outcome store temporarily unavailable")
            return put(conn, section, key, value)

        initial = now = time.time()
        clock = SimpleNamespace(time=lambda: now)
        with patch("homebrew_harness.services.notifications.time", clock):
            with patch.object(db, "_put", fail_outcome), self.assertRaisesRegex(OSError, "outcome store"):
                worker.tick()
            self.assertEqual(len(webhook.calls), 1)
            original_body = webhook.bodies[0]
            original_key = webhook.calls[0][1]["Idempotency-Key"]
            # An expired row changes the outgoing body without changing the pending digest.
            self.seed(service, "Now visible as expired", expires_at=initial - 1)
            self.assertEqual(service.inbox(include_expired=True)["counts"]["expired"], 1)
            webhook.status, webhook.retry_after = 204, None
            now = initial + 10
            worker.tick()
            failed = service.notification_status()
            self.assertEqual(failed["attempts"], 1)
            self.assertEqual(failed["next_retry_at"], initial + 30)
            self.assertEqual(len(webhook.calls), 1)

            now = initial + 30
            with patch.object(db, "_put", fail_outcome), self.assertRaisesRegex(OSError, "outcome store"):
                worker.tick()
            self.assertEqual(webhook.bodies, [original_body, original_body])
            self.assertEqual(webhook.calls[1][1]["Idempotency-Key"], original_key)
            # The successful POST should settle without a third request.
            worker.tick()
            settled = service.notification_status()
            self.assertEqual(settled["sent_count"], 1)
            self.assertEqual(settled["attempts"], 0)
            self.assertIsNone(settled["next_retry_at"])
            self.assertEqual(settled["last_delivery_id"], original_key)
            self.assertEqual(len(webhook.calls), 2)

    def test_connection_failure_retries_and_reuses_its_transport(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.configure(f"http://127.0.0.1:{port}/private/digest")
        service = self.start(request_seconds=0.5)
        self.seed(service, "Await a reachable receiver")
        failed = eventually(lambda: (status if (status := service.notification_status())["last_error"] else None))
        self.assertIn(failed["last_error"], {"Webhook connection failed.", "Webhook request timed out."})
        self.assertGreater(failed["next_retry_at"], time.time())
        transport = service.runtime.notifications.transport
        webhook = self.webhook(port)
        self.send_count(service, 1)
        self.assertEqual(len(webhook.calls), 1)
        self.assertIs(service.runtime.notifications.transport, transport)

    def test_slow_post_does_not_block_coordinator_and_is_cancelled_on_close(self):
        webhook = self.webhook()
        webhook.wait.clear()
        self.configure(webhook.url)
        service = self.start(request_seconds=10)
        self.seed(service, "Call the receiver")
        eventually(lambda: webhook.calls)
        before = service.snapshot().details["heartbeat_at"]
        eventually(lambda: service.snapshot().details["heartbeat_at"] > before, 3)
        with RemoteService.from_root(self.control, control=True) as remote:
            self.assertEqual(remote.notification_status()["sent_count"], 0)
            self.assertTrue(remote.stop()["accepted"])
        self.assertTrue(service.wait_closed(4))
        self.assertFalse(service.runtime.notifications.thread.is_alive())

    def test_quiesce_drains_webhook_blocks_new_sends_and_resumes_notifications(self):
        webhook = self.webhook()
        self.configure(webhook.url)
        service = self.start(request_seconds=10, every_seconds=3600, min_pending=2)
        service.pause("all")  # Let the manual send reach the receiver first.
        self.seed(service, "Before operations park")
        webhook.wait.clear()
        with (
            RemoteService.from_root(self.control, control=True) as remote,
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            first = pool.submit(remote.send_notification, request_id="before-park")
            try:
                eventually(lambda: webhook.calls, 3)
                park = remote.quiesce()
                self.assertTrue(service.runtime.notifications.parked.wait(3))
                self.assertEqual(remote.health()["operations_phase"], "draining")
                self.assertFalse(first.done())
                self.assertEqual(remote.notification_status()["sent_count"], 0)
                refused = pool.submit(
                    remote.send_notification, force=True, request_id="after-park"
                )
                with self.assertRaisesRegex(SDKError, r"Service is stopping or parked\."):
                    refused.result(timeout=2)
                self.assertIsNone(remote.receipt("after-park"))
                self.assertEqual(remote.operation(park["id"])["state"], "running")
                self.assertEqual(len(webhook.calls), 1)
            finally:
                webhook.wait.set()

            delivered = first.result(timeout=4)
            eventually(lambda: remote.operation(park["id"])["state"] == "completed")
            self.assertTrue(remote.operation(park["id"])["result"]["parked"])
            self.assertEqual(remote.health()["operations_phase"], "parked")
            self.assertTrue(delivered["sent"])
            self.assertEqual(delivered["delivery_id"], webhook.calls[0][1]["Idempotency-Key"])
            self.assertEqual(remote.notification_status()["sent_count"], 1)
            self.assertEqual(delivered, remote.receipt("before-park")["result"])
            self.assertEqual(delivered, remote.send_notification(request_id="before-park"))
            with self.assertRaisesRegex(SDKError, "another command payload"):
                remote.send_notification(force=True, request_id="before-park")
            with self.assertRaisesRegex(SDKError, "parked"):
                remote.send_notification(force=True, request_id="after-park")
            self.assertEqual(len(webhook.calls), 1)

            resume = remote.resume_operations()
            eventually(lambda: remote.operation(resume["id"])["state"] == "completed")
            self.assertTrue(remote.send_notification(force=True, request_id="after-park")["sent"])
            self.seed(service, "After operations resume")
            self.assertEqual(self.send_count(service, 3)["last_trigger"], "threshold")
            self.assertEqual(
                [payload["trigger"] for _, _, payload in webhook.calls],
                ["manual", "manual", "threshold"],
            )

    def test_manual_force_replay_cli_and_text_ui_in_manual_profile(self):
        webhook = self.webhook()
        credentials = self.base / "webhook.env"
        self.configure(webhook.url, file=credentials)
        service = self.start(profile="manual", credentials_file=str(credentials))
        self.assertEqual(service.send_notification(), {"sent": False, "reason": "no_actionable_items"})
        self.assertEqual(self.cli(service, "notify"), {"sent": False, "reason": "no_actionable_items"})
        self.assertEqual(webhook.calls, [])
        sent = self.cli(service, "notify", "--force", "--request-id", "send-once")
        self.assertTrue(sent["sent"])
        self.assertEqual(sent["trigger"], "manual")
        self.assertEqual(sent["counts"]["total"], 0)
        self.assertEqual(sent, service.send_notification(force=True, request_id="send-once"))
        self.assertEqual(sent, service.receipt("send-once")["result"])
        with self.assertRaises(SDKError):
            service.send_notification(force=False, request_id="send-once")
        with self.assertRaises(SDKError):
            service.send_notification(force=1)
        self.assertEqual(sent["delivery_id"], webhook.calls[0][1]["Idempotency-Key"])
        status = self.cli(service, "notify-status")
        self.assertEqual(status["sent_count"], 1)
        self.assertEqual(status["config"]["credential_source"], "file")
        self.assertNotIn(str(credentials), json.dumps(status))
        lines = iter(["inbox notify-status", "inbox notify --force --request-id from-ui", "exit"])
        with RemoteService.from_root(self.control, control=True) as remote, redirect_stdout(io.StringIO()) as output:
            attach(remote, input_fn=lambda _: next(lines))
        self.assertIn('"enabled": true', output.getvalue())
        self.assertIn('"sent": true', output.getvalue())
        self.assertEqual(len(webhook.calls), 2)

    def test_manual_replay_never_reposts_after_receipt_write_failure(self):
        webhook = self.webhook()
        self.configure(webhook.url)
        with patch("homebrew_harness.services.notifications.ServiceNotifications.start"):
            service = self.start(profile="manual")
        worker = service.runtime.notifications
        db = worker.store.db
        put = db._put

        def fail_final_receipt(conn, section, key, value):
            if section == "service_commands" and (value.get("result") or {}).get("sent"):
                raise OSError("receipt store temporarily unavailable")
            return put(conn, section, key, value)

        with patch.object(db, "_put", fail_final_receipt):
            with self.assertRaises(SDKError):
                service.send_notification(force=True, request_id="uncertain-send")
            prior = service.receipt("uncertain-send")["result"]
            self.assertEqual(prior["reason"], "delivery_unknown")
            self.assertEqual(service.send_notification(force=True, request_id="uncertain-send"), prior)
            self.assertEqual(len(webhook.calls), 1)
        worker.tick()
        settled = service.receipt("uncertain-send")["result"]
        self.assertTrue(settled["sent"])
        self.assertEqual(service.send_notification(force=True, request_id="uncertain-send"), settled)
        self.assertEqual(service.notification_status()["sent_count"], 1)
        self.assertEqual(len(webhook.calls), 1)

    def test_short_bearer_is_redacted_before_webhook_preview_clipping(self):
        webhook = self.webhook()
        short_token = "Q7kP2vZ"
        credentials = self.base / "webhook.env"
        credentials.write_text(f"WEBHOOK_URL={webhook.url}\nWEBHOOK_TOKEN={short_token}\n")
        credentials.chmod(0o600)
        service = self.start(credentials_file=str(credentials))
        service.pause("all")
        # Leave room for only part of the second token if redaction runs after clipping.
        plan = f"Secret {short_token}: " + "x" * 483 + " " + short_token
        self.seed(service, "Short credential in preview", plan=plan)
        sent = service.send_notification(request_id="short-credential")
        self.assertTrue(sent["sent"])
        _, headers, body = webhook.calls[0]
        self.assertEqual(headers["Authorization"], "Bearer " + short_token)
        self.assertNotIn(short_token, json.dumps(body))
        self.assertIn("Secret [redacted]", body["items"][0]["preview"])
        self.assertNotIn(" Q7", body["items"][0]["preview"])
        self.assertEqual(redact(short_token), short_token)  # not a global redaction rule
        durable = service.store.runtime.get("service_notifications", service.id)
        self.assertNotIn(short_token, json.dumps([durable, service.receipt("short-credential")]))

    def test_manual_send_with_pending_items_bypasses_retry_and_cooldown(self):
        webhook = self.webhook()
        self.configure(webhook.url)
        service = self.start(cooldown_seconds=3600, include_expired=False)
        service.pause("all")
        self.seed(service, "Current item")
        self.seed(service, "Expired item", expires_at=time.time() - 1)
        webhook.status = 401
        failed = service.send_notification(request_id="bad-token")
        self.assertEqual(failed["last_error"], "Webhook returned HTTP 401.")
        self.assertGreater(service.notification_status()["next_retry_at"], time.time() + 50)
        webhook.status = 204
        sent = service.send_notification(request_id="try-now")
        self.assertTrue(sent["sent"])
        self.assertEqual(sent["counts"]["total"], 1)
        self.assertTrue(service.send_notification()["sent"])
        self.assertEqual([payload["trigger"] for _, _, payload in webhook.calls], ["manual"] * 3)

    def test_disabled_never_reads_credentials_starts_a_worker_or_sends(self):
        credentials = self.base / "missing-credentials.env"
        with patch("homebrew_harness.services.notifications.read_notification_credentials") as read:
            service = self.start(profile="manual", enabled=False, credentials_file=str(credentials))
            self.assertIsNone(service.runtime.notifications.thread)
            self.assertIsNone(service.runtime.notifications.transport)
            self.assertFalse(service.notification_status()["enabled"])
            self.assertEqual(service.send_notification(force=True), {"sent": False, "reason": "notifications_disabled"})
            read.assert_not_called()

    def test_spec_compatibility_and_credential_file_boundaries(self):
        spec = ServiceSpec(str(self.workspace))
        legacy = spec.data()
        self.assertNotIn("notifications", legacy)
        self.assertEqual(legacy, ServiceSpec(**legacy, notifications={}).data())
        self.assertEqual(fingerprint(legacy), fingerprint(ServiceSpec(**legacy).data()))
        service = self.start(profile="manual", notifications=False)
        self.assertNotIn("notifications", service.store.get(service.id)["spec"])
        self.assertIsNone(service.runtime.notifications.thread)
        self.assertEqual(service.notification_status()["config"], {})
        external = self.base / "webhook.env"
        self.configure("http://localhost:9999/notify", file=external)
        link = self.base / "linked.env"
        link.symlink_to(external)
        for path in (self.workspace / "private.env", self.control / "private.env"):
            self.configure("http://localhost:9999/notify", file=path)
        for path in (link, self.workspace / "private.env", self.control / "private.env"):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "external"):
                ServiceSpec(str(self.workspace), notifications={"url_env": "WEBHOOK_URL", "credentials_file": str(path)}).validate_location(self.control)
        configured = ServiceSpec(str(self.workspace), notifications={"url_env": "WEBHOOK_URL", "credentials_file": str(external)})
        configured.validate_location(self.control)
        external.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "owner-only"):
            configured.validate_location(self.control)

    def test_config_schema_and_url_boundaries(self):
        options = {"url_env": "WEBHOOK_URL"}
        spec = ServiceSpec(str(self.workspace), notifications=options)
        self.assertEqual(spec.notifications, {
            **options, "enabled": True, "mode": "remote", "token_env": None, "ca_file_env": None,
            "credentials_file": None, "every_seconds": 43200, "min_pending": None,
            "cooldown_seconds": 300, "request_seconds": 10, "include_expired": True,
        })
        for invalid in ({"enabled": 0}, {"include_expired": "true"}, {"min_pending": True},
                        {"min_pending": 0}, {"every_seconds": 0.05}, {"every_seconds": 31536001},
                        {"cooldown_seconds": -1}, {"request_seconds": float("nan")}, {"request_seconds": 61},
                        {"ca_file_env": "lower_case"}, {"url_env": None}, {"mode": "example"},
                        {"credentials_file": "relative.env"}, {"url": "https://invalid.example"}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                ServiceSpec(str(self.workspace), notifications={**options, **invalid})
        for mode, url in (("remote", "http://external.invalid/secret"), ("remote", "https://private@external.invalid"),
                          ("remote", "https://external.invalid:nonport/secret"), ("remote", "https://external.invalid/?secret"),
                          ("remote", "https://external.invalid/#secret"), ("local", "https://external.invalid")):
            with self.subTest(mode=mode, url=url), self.assertRaises(ValueError) as caught:
                open_notification_transport({**spec.notifications, "mode": mode}, {"WEBHOOK_URL": url}, ())
            self.assertNotIn(url, str(caught.exception))
        for url in ("https://external.invalid/digest", "http://127.0.0.1:8000/digest"):
            open_notification_transport(spec.notifications, {"WEBHOOK_URL": url}, ()).close()
