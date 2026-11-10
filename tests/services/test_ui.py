import io
import unittest
from contextlib import redirect_stdout

from homebrew_harness.cli import parser
from homebrew_harness.services import control
from homebrew_harness.services.cli import attach
from tests.services import test_runtime as fixture


class ServiceUI(unittest.TestCase):
    setUp = fixture.ServiceRuntimeTests.setUp
    host = fixture.ServiceRuntimeTests.host
    spec = fixture.ServiceRuntimeTests.spec

    def test_line_exit_keeps_owner_and_terminal_sequences_are_escaped(self):
        host, _ = self.host([])
        service = host.services.start(self.spec())
        lines = iter(["status", "exit"])
        output = io.StringIO()
        with redirect_stdout(output):
            attach(service, input_fn=lambda _prompt: next(lines))
        self.assertEqual(service.snapshot().state, "running")
        from homebrew_harness.services.cli import render

        with redirect_stdout(output):
            render({"body": "\x1b]52;c;fake\x07"})
        self.assertNotIn("\x1b", output.getvalue())
        self.assertIn("\\u001b", output.getvalue())
        service.stop()
        self.assertTrue(service.wait_closed(10))

    def test_bounded_invalid_http_body_does_not_dispatch_or_break_service(self):
        import httpx

        host, _ = self.host([])
        service = host.services.start(self.spec())
        credentials = control.ControlClient(self.control).credentials()
        headers = {
            "Authorization": "Bearer " + credentials.token,
            "Content-Type": "application/json",
        }
        with httpx.Client(trust_env=False) as client:
            response = client.post(
                credentials.endpoint + "/v1/control",
                content=b"x" * (control.MAX_FRAME + 1),
                headers=headers,
            )
            self.assertEqual(response.status_code, 413)
            response = client.post(
                credentials.endpoint + "/v1/control",
                json={
                    "version": 1,
                    "service_id": service.id,
                    "method": "snapshot",
                    "payload": {},
                    "request_id": None,
                    "actor": "owner",
                },
                headers=headers,
            )
            self.assertEqual(response.status_code, 400)
        self.assertEqual(service.snapshot().state, "running")
        service.stop()
        self.assertTrue(service.wait_closed(10))

    def test_unified_cli_routes_service_and_subcommand_locations(self):
        args = parser().parse_args(
            ["service", "status", "svc_" + "a" * 32, "--cwd", str(self.control)]
        )
        self.assertEqual(args.entrypoint, "service")
        self.assertEqual(args.cwd, self.control)
        self.assertEqual(args._handler, "homebrew_harness.services.cli")

    def test_control_context_focus_survives_reconnect_without_becoming_global(self):
        host, _ = self.host([])
        service = host.services.start(self.spec())
        service.pause()
        work = service.work.submit("Queued work")
        service.focus(work.id, context="first")
        client = control.ControlClient(self.control)
        focus = client.call(service.id, "focus_get", {"context": "first"})
        self.assertEqual(focus["work_id"], work.id)
        self.assertIsNone(client.call(service.id, "focus_get", {"context": "second"}))
        service.stop()
        self.assertTrue(service.wait_closed(10))
