"""Ownership, acceptance uncertainty and bounded closure at the SDK boundary."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from homebrew_harness import Harness, HostConfig
from homebrew_harness.api import SDKError, SessionOptions
from homebrew_harness.runtime.interaction_config import InteractionAssembler
from tests.support.clients import fake_client, reply


class APILifecycle(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()

    def config(self, *, own=False):
        client, _ = fake_client([reply("done")])
        client.close = Mock()
        return client, HostConfig.from_components(
            client=client, model="fixture", own_client=own
        )

    def test_closing_tracks_a_factory_already_admitted(self):
        client, config = self.config(own=True)
        host = Harness(workdir=self.root, config=config)
        self.addCleanup(host.close)
        entered, release = threading.Event(), threading.Event()
        original = InteractionAssembler.session
        handles, errors = [], []

        def prepare(builder, *args, **kwargs):
            entered.set()
            release.wait(5)
            return original(builder, *args, **kwargs)

        def open_session():
            try:
                handles.append(host.sessions.open())
            except Exception as error:
                errors.append(error)

        with patch.object(InteractionAssembler, "session", prepare):
            worker = threading.Thread(target=open_session)
            worker.start()
            self.assertTrue(entered.wait(5))
            report = host.close(0.01)
            self.assertEqual(report.state, "cleanup_pending")
            self.assertEqual(report.pending_calls, 1)
            with self.assertRaises(SDKError):
                host.sessions.open()
            release.set()
            worker.join(5)
        self.assertFalse(errors)
        self.assertEqual(len(handles), 1)
        self.assertEqual(host.wait_closed(5).state, "closed")
        self.assertTrue(handles[0].runtime.wait_closed(0))
        client.close.assert_called_once()

    def test_borrowed_client_is_retained_and_owned_client_closes_once(self):
        for own in (False, True):
            with self.subTest(own=own), tempfile.TemporaryDirectory() as root:
                client, config = self.config(own=own)
                host = Harness(workdir=Path(root), config=config)
                session = host.sessions.open()
                session.submit("Work").wait(5)
                self.assertEqual(host.close().state, "closed")
                self.assertEqual(host.close().state, "closed")
                self.assertEqual(client.close.call_count, int(own))
                self.assertIsNone(host.run_store._anchor)

    def test_close_reports_pending_real_model_work_then_settles(self):
        client, config = self.config()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def create(**_kwargs):
            entered.set()
            release.wait(6)
            return reply("late")

        client.chat.completions.create.side_effect = create
        host = Harness(workdir=self.root, config=config)
        session = host.sessions.open()
        turn = session.submit("Work")
        self.assertTrue(entered.wait(5))
        report = host.close(0.01)
        self.assertFalse(report.resources_closed)
        release.set()
        self.assertEqual(host.wait_closed(8).state, "closed")
        self.assertEqual(turn.result().state, "cancelled")

    def test_options_validate_before_creating_execution(self):
        for value in (
            {"enabled": "teams"},
            {"max_turns": True},
            {"turn_seconds": float("nan")},
            {"mcp": {"docs": []}},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                SessionOptions(**value)

    def test_saved_session_inspection_requires_explicit_owner(self):
        _, config = self.config()
        host = Harness(workdir=self.root, config=config)
        session = host.sessions.open()
        identity = session.id
        host.close()
        with Harness(workdir=self.root, config=config) as reader:
            with self.assertRaises(SDKError):
                reader.admin.inspect(reader.sessions.get(identity), "operation", 0)

    def test_failed_component_close_can_be_retried_without_losing_ownership(self):
        client, config = self.config(own=True)
        client.close.side_effect = [OSError("temporary close failure"), None]
        host = Harness(workdir=self.root, config=config)
        self.assertEqual(host.close(1).state, "cleanup_pending")
        self.assertEqual(host.close(1).state, "closed")
        self.assertEqual(client.close.call_count, 2)

    def test_persistence_failure_reports_error_after_real_resource_release(self):
        client, config = self.config(own=True)
        host = Harness(workdir=self.root, config=config)
        session = host.sessions.open()
        original = session.runtime.shutdown_status

        def status():
            value = original()
            if value["resources_closed"]:
                value.update(
                    persistence_ok=False,
                    errors=[
                        {"component": "fixture", "error_type": "persistence_failed"}
                    ],
                )
            return value

        with patch.object(session.runtime, "shutdown_status", side_effect=status):
            report = host.close(5)
        self.assertEqual(report.state, "error")
        self.assertTrue(report.resources_closed)
        self.assertFalse(report.persistence_ok)
        client.close.assert_called_once()
