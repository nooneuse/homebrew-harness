"""Failure after completion must preserve errors, resources and retry ownership."""

import asyncio
import errno
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout, redirect_stderr
from types import SimpleNamespace
from unittest.mock import patch

from homebrew_harness.execution.execution_leases import ExecutionLeases
from homebrew_harness.runtime.session import HarnessSession
from tests.support.clients import fake_client
from homebrew_harness.orchestration.workflow_runtime import (
    WorkflowRuntime,
    WorkflowRegistry,
    WorkflowSpec,
    WorkflowJournal,
    WorkflowError,
    RunnerOutput,
)


class ShutdownRecoveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        for context in (redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)

    def workflow(self, script=None):
        async def default(ctx, args):
            return await ctx.agent("fixture")

        runner = SimpleNamespace(
            identity="fixture", run=lambda *args: RunnerOutput("saved")
        )
        runtime = WorkflowRuntime(
            self.root,
            runner,
            WorkflowRegistry(
                [
                    WorkflowSpec(
                        "fixture", "fixture", script or default, {"type": "object"}
                    )
                ]
            ),
        )
        self.addCleanup(runtime.close)
        return runtime

    def session(self, **kwargs):
        client, _ = fake_client([])
        session = HarnessSession(
            client, "fixture", self.root, shutdown_timeout=0.05, **kwargs
        )
        self.addCleanup(lambda: session.wait_closed(3))
        self.addCleanup(session.close)
        return session

    def journal_failure(self, runtime, *, write=False, before_close=False):
        original = WorkflowJournal.__init__
        descriptors = []

        class Handle:
            def __init__(self, value):
                self.value = value

            def write(self, text):
                if write:
                    raise OSError("fixture write failure")
                return self.value.write(text)

            def close(self):
                if not before_close:
                    self.value.close()
                raise OSError("fixture close failure")

            def __getattr__(self, name):
                return getattr(self.value, name)

        def init(journal, *args, **kwargs):
            original(journal, *args, **kwargs)
            descriptors.append(journal.fd)
            journal.handle = Handle(journal.handle)

        with patch.object(WorkflowJournal, "__init__", init):
            with self.assertRaisesRegex(WorkflowError, "journal close failed"):
                runtime.run("fixture", {})
        return descriptors

    def test_journal_close_error_releases_run_and_preserves_saved_result(self):
        runtime = self.workflow()
        descriptors = self.journal_failure(runtime)
        self.assertFalse(runtime._stops)
        self.assertTrue(runtime.wait_closed(1))
        self.assertEqual(runtime.cleanup_errors[0]["component"], "workflow_journal")
        run_id = runtime.cleanup_errors[0]["run_id"]
        state = runtime.inspect(run_id)
        self.assertEqual(state["task"]["status"], "failed")
        self.assertTrue(state["task"]["inspection_required"])
        self.assertEqual(
            json.loads(runtime.store.path(run_id, ".output.json").read_text()), "saved"
        )
        self.assertEqual(state["task"]["cleanup_errors"][0]["error_type"], "OSError")
        for fd in descriptors:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_write_and_close_errors_do_not_leave_fake_pending_or_open_descriptor(self):
        runtime = self.workflow()
        descriptors = self.journal_failure(runtime, write=True, before_close=True)
        self.assertFalse(runtime._stops)
        self.assertTrue(runtime.wait_closed(1))
        run_id = runtime.cleanup_errors[0]["run_id"]
        result = json.loads(runtime.store.path(run_id, ".output.json").read_text())
        self.assertIn("persist", result["error"])
        self.assertNotIn("close failed", result["error"])
        for fd in descriptors:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_primary_failure_survives_a_second_journal_close_failure(self):
        runtime = self.workflow()
        original = WorkflowJournal.__init__

        class Handle:
            def __init__(self, value):
                self.value = value

            def close(self):
                self.value.close()
                raise OSError("secondary close error")

            def __getattr__(self, name):
                return getattr(self.value, name)

        def init(journal, *args, **kwargs):
            original(journal, *args, **kwargs)
            journal.handle = Handle(journal.handle)

        with (
            patch.object(WorkflowJournal, "__init__", init),
            patch.object(
                runtime.store, "write", side_effect=ValueError("primary store error")
            ),
        ):
            with self.assertRaisesRegex(ValueError, "primary store error"):
                runtime.run("fixture", {})
        self.assertTrue(runtime.wait_closed(1))
        self.assertEqual(
            {item["component"] for item in runtime.cleanup_errors},
            {"workflow_journal", "workflow_cleanup_record"},
        )

    def test_session_closes_resources_after_workflow_cleanup_failure_and_reports_error(
        self,
    ):
        runtime = self.workflow()
        session = self.session(workflow_runtime=runtime)
        session.run_user("/usage")
        self.journal_failure(runtime)
        session.close()
        self.assertTrue(session.wait_closed(2))
        status = session.shutdown_status()
        self.assertEqual(status["state"], "error")
        self.assertTrue(status["resources_closed"])
        self.assertFalse(status["persistence_ok"])
        self.assertTrue(
            any(item["component"] == "workflow_journal" for item in status["errors"])
        )
        self.assertFalse(
            session.runtime_store.get("session_meta", session.session_id)[
                "close_complete"
            ]
        )
        with ExecutionLeases(self.root).use("session"):
            pass

    def wait_retryable(self, session):
        end = time.monotonic() + 2
        while not session.shutdown_status()["retryable"] and time.monotonic() < end:
            threading.Event().wait(0.005)
        self.assertTrue(session.shutdown_status()["retryable"])

    def test_release_failure_is_pending_then_retry_completes_without_reclosing_components(
        self,
    ):
        session = self.session()
        session.run_user("/usage")
        with (
            patch.object(
                session.tests, "close", wraps=session.tests.close
            ) as stop_tests,
            patch.object(
                session, "_save_checkpoint", wraps=session._save_checkpoint
            ) as checkpoint,
            patch.object(
                session.workspace,
                "release_session",
                side_effect=OSError("fixture release failure"),
            ),
        ):
            session.close()
            self.wait_retryable(session)
            self.assertFalse(session.wait_closed(0.01))
            status = session.shutdown_status()
            self.assertEqual(status["state"], "pending")
            self.assertFalse(status["resources_closed"])
            self.assertTrue(status["session_lease_held"])
            self.assertFalse(
                session.runtime_store.get("session_meta", session.session_id)[
                    "close_complete"
                ]
            )
            session.close()
            self.wait_retryable(session)
            self.assertEqual(stop_tests.call_count, 1)
            self.assertEqual(checkpoint.call_count, 1)
        session.close()
        self.assertTrue(session.wait_closed(2))
        status = session.shutdown_status()
        self.assertEqual(status["state"], "closed")
        self.assertFalse(status["errors"])
        self.assertEqual(
            [item["component"] for item in status["cleanup_history"]],
            ["session_lease", "session_lease"],
        )
        self.assertTrue(
            session.runtime_store.get("session_meta", session.session_id)[
                "close_complete"
            ]
        )
        with ExecutionLeases(self.root).use("session"):
            pass

    def test_native_descriptor_close_failure_preserves_lease_registration_for_retry(
        self,
    ):
        session = self.session()
        session.run_user("/usage")
        fd = session.workspace.leases._held["session"][0]
        original = os.close

        def close(handle):
            if handle == fd:
                raise OSError(errno.EIO, "fixture fd close failure")
            return original(handle)

        with patch("homebrew_harness.execution.execution_leases.os.close", close):
            session.close()
            self.wait_retryable(session)
            self.assertTrue(session.workspace.leases.held("session"))
            other = ExecutionLeases(self.root)
            with self.assertRaises(ValueError):
                other.acquire("session")
            other.close()
        session.close()
        self.assertTrue(session.wait_closed(2))
        self.assertFalse(session.workspace.leases.held("session"))
        self.assertEqual(session.shutdown_status()["state"], "closed")

    def test_error_after_actual_release_is_observed_without_writing_after_ownership_loss(
        self,
    ):
        session = self.session()
        session.run_user("/usage")
        original = session.workspace.release_session

        def release():
            original()
            raise OSError("fixture after release")

        with (
            patch.object(session.workspace, "release_session", release),
            patch.object(
                session.runtime_store,
                "finish_session",
                wraps=session.runtime_store.finish_session,
            ) as lifecycle,
        ):
            session.close()
            self.assertTrue(session.wait_closed(2))
            self.assertEqual(lifecycle.call_count, 1)
        self.assertFalse(session.shutdown_status()["session_lease_held"])
        self.assertEqual(session.shutdown_status()["state"], "error")
        with ExecutionLeases(self.root).use("session"):
            pass

    def test_retrying_final_release_keeps_lease_until_new_close_proof_is_saved(self):
        session = self.session()
        session.run_user("/usage")
        with patch.object(
            session.workspace, "release_session", side_effect=OSError("fixture")
        ):
            session.close()
            self.wait_retryable(session)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = session.runtime_store.finish_session

        def finish(*args, **kwargs):
            entered.set()
            release.wait(3)
            return original(*args, **kwargs)

        with patch.object(session.runtime_store, "finish_session", finish):
            session.close()
            self.assertTrue(entered.wait(1))
            self.assertFalse(session.wait_closed(0.01))
            other = ExecutionLeases(self.root)
            with self.assertRaises(ValueError):
                other.acquire("session")
            other.close()
            release.set()
            self.assertTrue(session.wait_closed(2))

    def test_shared_stop_event_keeps_each_active_run_registered(self):
        async def scenario():
            entered = asyncio.Event()
            shared = threading.Event()

            async def script(ctx, args):
                if args["which"] == "first":
                    await entered.wait()
                    return "first done"
                entered.set()
                while not shared.is_set():
                    await asyncio.sleep(0.005)
                return "second stopped"

            runtime = self.workflow(script)
            first = asyncio.create_task(
                runtime.arun("fixture", {"which": "first"}, stop_event=shared)
            )
            second = asyncio.create_task(
                runtime.arun("fixture", {"which": "second"}, stop_event=shared)
            )
            self.assertEqual((await first)["task"]["status"], "completed")
            self.assertFalse(runtime.wait_closed(0))
            runtime.close()
            self.assertTrue(shared.is_set())
            result = await asyncio.wait_for(second, 1)
            self.assertEqual(result["task"]["status"], "paused")
            self.assertTrue(runtime.wait_closed(0))

        asyncio.run(scenario())

    def test_cancellation_survives_journal_close_error_without_run_leak(self):
        async def scenario():
            entered = asyncio.Event()

            async def script(ctx, args):
                entered.set()
                await asyncio.Event().wait()

            runtime = self.workflow(script)
            original = WorkflowJournal.__init__

            class Handle:
                def __init__(self, value):
                    self.value = value

                def close(self):
                    self.value.close()
                    raise OSError("fixture close during cancel")

                def __getattr__(self, name):
                    return getattr(self.value, name)

            def init(journal, *args, **kwargs):
                original(journal, *args, **kwargs)
                journal.handle = Handle(journal.handle)

            with patch.object(WorkflowJournal, "__init__", init):
                pending = asyncio.create_task(runtime.arun("fixture", {}))
                await entered.wait()
                pending.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await pending
            self.assertTrue(runtime.wait_closed(0))
            self.assertTrue(runtime.cleanup_errors)

        asyncio.run(scenario())

    def test_failed_revocation_write_can_be_repaired_on_release_retry(self):
        session = self.session()
        session.run_user("/usage")
        original = session.runtime_store.finish_session

        def finish(*args, **kwargs):
            if kwargs["complete"] is False:
                raise OSError("fixture revocation write unavailable")
            return original(*args, **kwargs)

        with (
            patch.object(
                session.workspace,
                "release_session",
                side_effect=OSError("fixture release"),
            ),
            patch.object(session.runtime_store, "finish_session", finish),
        ):
            session.close()
            self.wait_retryable(session)
            self.assertTrue(session.shutdown_status()["session_lease_held"])
            self.assertTrue(
                any(
                    item["component"] == "session_lifecycle"
                    for item in session.shutdown_status()["errors"]
                )
            )
        session.close()
        self.assertTrue(session.wait_closed(2))
        self.assertEqual(session.shutdown_status()["state"], "closed")
        self.assertTrue(
            session.runtime_store.get("session_meta", session.session_id)[
                "close_complete"
            ]
        )
        self.assertTrue(
            any(
                item["component"] == "session_lifecycle"
                for item in session.shutdown_status()["cleanup_history"]
            )
        )

    def test_fd_reused_after_close_error_is_not_closed_as_the_old_lease(self):
        session = self.session()
        session.run_user("/usage")
        fd = session.workspace.leases._held["session"][0]
        replacement = self.root / "replacement.txt"
        replacement.write_text("fixture")
        original = os.close
        installed = []

        def close(handle):
            if handle != fd:
                return original(handle)
            original(handle)
            other = os.open(replacement, os.O_RDONLY)
            if other != fd:
                os.dup2(other, fd)
                original(other)
            installed.append(fd)
            raise OSError(errno.EIO, "fixture close returned an error after release")

        try:
            with patch("homebrew_harness.execution.execution_leases.os.close", close):
                session.close()
                self.assertTrue(session.wait_closed(2))
            self.assertEqual(installed, [fd])
            self.assertFalse(session.shutdown_status()["session_lease_held"])
            self.assertEqual(os.fstat(fd).st_ino, replacement.stat().st_ino)
            session.close()
            self.assertEqual(os.fstat(fd).st_ino, replacement.stat().st_ino)
            with ExecutionLeases(self.root).use("session"):
                pass
        finally:
            for handle in installed:
                original(handle)

    def test_dirty_journal_stream_cannot_flush_into_a_reused_descriptor(self):
        runtime = self.workflow()
        original = WorkflowJournal.__init__
        streams = []

        class Handle:
            def __init__(self, value):
                self.value = value

            def flush(self):
                raise OSError("fixture dirty flush")

            def close(self):
                raise OSError("fixture close before underlying close")

            def __getattr__(self, name):
                return getattr(self.value, name)

        def init(journal, *args, **kwargs):
            original(journal, *args, **kwargs)
            streams.append((journal.fd, journal.handle))
            journal.handle = Handle(journal.handle)

        with patch.object(WorkflowJournal, "__init__", init):
            with self.assertRaises(WorkflowError):
                runtime.run("fixture", {})
        fd, stream = streams[0]
        replacement = self.root / "untouched.txt"
        replacement.write_text("original")
        other = os.open(replacement, os.O_WRONLY)
        try:
            if other != fd:
                os.dup2(other, fd)
            try:
                stream.close()
            except (OSError, ValueError):
                pass
            self.assertEqual(replacement.read_text(), "original")
        finally:
            os.close(fd)
            if other != fd:
                os.close(other)
