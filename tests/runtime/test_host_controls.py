"""Local commands stay usable without accepting new model work or inspecting effects."""

import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from homebrew_harness.runtime.session import HarnessSession, HostCommand
from homebrew_harness.storage.durable_runtime import RecoveryRequired
from homebrew_harness.execution.execution_control import operation_deadline
from homebrew_harness.execution.model_gateway import Deadline, ModelDeadlineExceeded
from tests.support.clients import fake_client, call
from homebrew_harness.context.tracked_history import encoded_message


class HostControlTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        for context in (redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)

    def session(self, **kwargs):
        client, _ = fake_client([])
        session = HarnessSession(
            client, "fixture", self.root, shutdown_timeout=0.05, **kwargs
        )
        self.addCleanup(lambda: session.wait_closed(3))
        self.addCleanup(session.close)
        session.run_user("/usage")
        return session, client

    def test_expired_goal_status_and_cancel_aliases_are_local_and_preserve_intent(self):
        session, client = self.session()
        session.intent.update("Keep the original task and its constraints.")
        original = session.intent.to_dict()
        session.gateway._state.deadline = Deadline.after(0)
        for alias in ("clear", "stop", "cancel", "off", "reset", "none", "Cancel"):
            with self.subTest(alias=alias):
                session.goal.set_goal("retained condition")
                identity = session.goal.state["goal_id"]
                self.assertEqual(
                    json.loads(session.run_user("/goal"))["goal_id"], identity
                )
                self.assertEqual(session.run_user("/goal " + alias), "Goal cleared.")
                self.assertEqual(session.goal.store.load(identity)["status"], "cleared")
                self.assertIsNone(session.goal.lease)
                self.assertEqual(session.intent.to_dict(), original)
        client.chat.completions.create.assert_not_called()

    def test_expired_goal_set_spec_and_resume_accept_no_new_model_work(self):
        session, client = self.session()
        session.goal.set_goal("original")
        identity = session.goal.state["goal_id"]
        session.gateway._state.deadline = Deadline.after(0)
        spec = {
            "condition": "file exists",
            "judge": False,
            "predicates": [{"id": "file", "type": "file_exists", "path": "result.txt"}],
        }
        for query in (
            "/goal new condition",
            "/goal clear more",
            "/goal resume " + identity,
            "/goal spec " + json.dumps(spec),
        ):
            with self.subTest(query=query):
                with self.assertRaises(ModelDeadlineExceeded):
                    session.run_user(query)
                self.assertEqual(session.goal.state["goal_id"], identity)
                self.assertEqual(session.goal.store.load(identity)["status"], "active")
        client.chat.completions.create.assert_not_called()

    def test_goal_cancel_does_not_acknowledge_pending_operation(self):
        session, client = self.session()
        session.goal.set_goal("fixture")
        assistant = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                call(
                    "pending",
                    name="write_file",
                    arguments='{"path":"effect.txt","content":"fixture"}',
                )
            ],
        }
        session.messages.extend(
            [
                assistant,
                {
                    "role": "tool",
                    "tool_call_id": "pending",
                    "content": '{"outcome":"unknown","error":"fixture interruption"}',
                },
            ]
        )
        row = {
            "checkpoint": session.checkpoint_key,
            "state": "unknown",
            "revision": 0,
            "name": "write_file",
            "call_id": "pending",
            "cwd": ".",
            "batch_id": encoded_message(assistant)[1],
        }
        session.runtime_store.put("operations", "fixture:pending", row)
        session._recovery_blocked = ["pending"]
        session.gateway._state.deadline = Deadline.after(0)
        self.assertEqual(json.loads(session.run_user("/goal"))["status"], "active")
        self.assertEqual(session.run_user("/goal cancel"), "Goal cleared.")
        self.assertEqual(
            session.runtime_store.get("operations", "fixture:pending"), row
        )
        self.assertTrue(json.loads(session.run_user("/recovery"))["blocked"])
        with self.assertRaises(RecoveryRequired):
            session.run_user("/recovery continue")
        self.assertEqual(
            json.loads(
                next(
                    m["content"]
                    for m in session.messages
                    if m.get("tool_call_id") == "pending"
                )
            )["outcome"],
            "unknown",
        )
        client.chat.completions.create.assert_not_called()

    def test_command_is_parsed_once_and_deadline_is_shared_with_handler(self):
        session, client = self.session()
        session.goal.set_goal("fixture")
        seen = []
        original = session._run_user
        status = session.goal.status

        def run(command, deadline):
            seen.append(deadline.expires_at)
            return original(command, deadline)

        def read():
            seen.append(operation_deadline().expires_at)
            return status()

        with (
            patch.object(HostCommand, "parse", wraps=HostCommand.parse) as parse,
            patch.object(session, "_run_user", run),
            patch.object(session.goal, "status", read),
        ):
            session.run_user("/goal")
        parse.assert_called_once_with("/goal")
        self.assertEqual(seen[0], seen[1])
        self.assertEqual(HostCommand.parse("/testsx").name, "")
        self.assertFalse(HostCommand.parse("/goal clear more").local)
        client.chat.completions.create.assert_not_called()

    def test_closed_session_does_not_reopen_for_goal_control(self):
        session, client = self.session()
        session.close()
        self.assertTrue(session.wait_closed(3))
        for query in ("/goal", "/goal cancel"):
            with self.assertRaisesRegex(ValueError, "closed"):
                session.run_user(query)
        self.assertFalse(session.workspace.leases.held("session"))
        client.chat.completions.create.assert_not_called()

    def test_local_goal_control_does_not_reset_provider_failure(self):
        session, client = self.session()
        session.goal.set_goal("fixture")
        session.gateway._state.blocked = "provider quota unavailable"
        session.gateway._state.deadline = Deadline.after(0)
        self.assertEqual(json.loads(session.run_user("/goal"))["status"], "active")
        self.assertEqual(session.run_user("/goal cancel"), "Goal cleared.")
        self.assertEqual(session.gateway._state.blocked, "provider quota unavailable")
        client.chat.completions.create.assert_not_called()
