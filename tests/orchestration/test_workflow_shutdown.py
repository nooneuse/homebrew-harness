"""Workflow front-end return is bounded while actual execution stays owned."""

import asyncio
import io
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from homebrew_harness import HarnessSession, Capabilities
from homebrew_harness.runtime.runtime_state import RecoveryRequired
from homebrew_harness.orchestration.workflow_runtime import (
    WorkflowError,
    WorkflowLimits,
    WorkflowRegistry,
    WorkflowRuntime,
    WorkflowSpec,
    RunnerOutput,
)
from tests.support.clients import fake_client, reply, call


class Runner:
    identity = "workflow-owner-fixture"

    def run(self, *_args):
        return RunnerOutput("ready")


class WorkflowShutdownTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.release, self.started = threading.Event(), threading.Event()
        self.addCleanup(self.release.set)
        self.attempts = 0
        for output in (redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            output.__enter__()
            self.addCleanup(output.__exit__, None, None, None)

    async def slow(self, ctx, args):
        self.attempts += 1
        if self.attempts > 1:
            return {"ready": True}
        self.started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            while not self.release.is_set():
                await asyncio.sleep(0.01)
        return {"late": "must not publish success"}

    def runtime(self, script=None, runner=None):
        rt = WorkflowRuntime(
            self.root,
            runner or Runner(),
            WorkflowRegistry(
                [WorkflowSpec("slow", "slow", script or self.slow, {"type": "object"})]
            ),
            limits=WorkflowLimits(timeout_seconds=0.06, cleanup_seconds=0.03),
        )
        self.addCleanup(lambda: rt.wait_closed(3))
        self.addCleanup(rt.close)
        self.addCleanup(self.release.set)
        return rt

    def test_sync_deadline_returns_pending_and_retains_lock_until_cleanup(self):
        runtime = self.runtime()
        started = time.monotonic()
        result = runtime.run("slow", {})
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(result["execution_pending"])
        identity = result["task"]["run_id"]
        self.assertEqual(result["task"]["status"], "paused")
        self.assertFalse(runtime.wait_closed(0.01))
        with self.assertRaises(WorkflowError):
            runtime.run("slow", resume_from_run_id=identity)
        with self.assertRaises(WorkflowError):
            other = self.runtime()
            other.run("slow", resume_from_run_id=identity)
        self.release.set()
        self.assertTrue(runtime.wait_closed(2))
        saved = runtime.inspect(identity)
        self.assertEqual(saved["task"]["status"], "paused")
        self.assertEqual(saved["task"]["cleanup"]["state"], "complete")
        resumed = runtime.run("slow", resume_from_run_id=identity)
        self.assertEqual(resumed["task"]["status"], "completed")
        self.assertEqual(resumed["result"], {"ready": True})

    def test_arun_caller_loop_exit_does_not_wait_for_workflow_loop(self):
        runtime = self.runtime()
        started = time.monotonic()
        result = asyncio.run(runtime.arun("slow", {}))
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(result["execution_pending"])
        self.assertTrue(runtime.pending())
        self.release.set()
        self.assertTrue(runtime.wait_closed(2))

    def test_sync_runner_remains_owned_and_late_output_is_not_cached(self):
        outer = self

        class SlowRunner:
            identity = "slow-inference"

            def run(self, *_args):
                outer.started.set()
                outer.release.wait(3)
                return RunnerOutput("late-inference")

        async def script(ctx, args):
            return await ctx.agent("step")

        runtime = self.runtime(script, SlowRunner())
        result = runtime.run("slow", {})
        self.assertTrue(result["execution_pending"])
        self.assertTrue(self.started.is_set())
        self.release.set()
        self.assertTrue(runtime.wait_closed(2))
        self.assertEqual(
            runtime.inspect(result["task"]["run_id"])["task"]["durable_steps"], 0
        )

    def test_session_goal_and_shutdown_keep_live_workflow_lease(self):
        runtime = self.runtime()
        client, _ = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "workflow",
                            name="workflow",
                            arguments='{"name":"slow","args":{}}',
                        )
                    ]
                ),
                reply("Claimed complete"),
            ]
        )
        session = HarnessSession(
            client,
            "fixture",
            self.root,
            workflow_runtime=runtime,
            capabilities=Capabilities({"workflow"}),
            shutdown_timeout=0.04,
        )
        self.addCleanup(lambda: session.wait_closed(3))
        self.addCleanup(session.close)
        self.addCleanup(self.release.set)
        with self.assertRaises(RecoveryRequired):
            session.run_user("Run the workflow")
        self.assertIn("workflow", session.pending_work())
        session.close()
        self.assertTrue(session.shutdown_status()["session_lease_held"])
        self.assertFalse(session.wait_closed(0.01))
        self.release.set()
        self.assertTrue(session.wait_closed(2))

    def test_failed_final_persistence_cannot_be_resumed_as_settled(self):
        async def script(ctx, args):
            return {"ready": True}

        runtime = self.runtime(script)
        original = runtime.store.write

        def fail(run_id, value, suffix=".json"):
            if (
                suffix == ".json"
                and value.get("task", {}).get("cleanup", {}).get("state") == "complete"
            ):
                raise OSError("final snapshot failed")
            return original(run_id, value, suffix)

        with (
            patch.object(runtime.store, "write", side_effect=fail),
            self.assertRaises(OSError),
        ):
            runtime.run("slow", {})
        identity = next(
            p.stem
            for p in (self.root / ".harness/workflows").glob("wf_*.json")
            if not p.name.endswith(".output.json")
        )
        with self.assertRaisesRegex(WorkflowError, "not confirmed"):
            runtime.run("slow", resume_from_run_id=identity)

    def test_failed_pause_record_does_not_claim_durable_confirmation(self):
        runtime = self.runtime()
        original = runtime.store.write

        def fail(run_id, value, suffix=".json"):
            task = value.get("task", {}) if isinstance(value, dict) else {}
            if (
                task.get("status") == "paused"
                and task.get("cleanup", {}).get("state") == "pending"
            ):
                raise OSError("pause record failed")
            return original(run_id, value, suffix)

        with patch.object(runtime.store, "write", side_effect=fail):
            result = runtime.run("slow", {})
            self.assertTrue(result["execution_pending"])
            self.assertFalse(result["persistence_confirmed"])
            self.assertTrue(result["task"]["inspection_required"])
        self.release.set()
        self.assertTrue(runtime.wait_closed(2))

    def test_close_and_resume_cannot_release_an_uncooperative_owner(self):
        runtime = self.runtime()
        result = runtime.run("slow", {})
        begin = time.monotonic()
        self.assertFalse(runtime.close(0.01))
        self.assertLess(time.monotonic() - begin, 0.2)
        self.assertTrue(runtime.pending())
        self.assertEqual(
            runtime.inspect(result["task"]["run_id"])["task"]["cleanup"]["state"],
            "pending",
        )
        self.release.set()
        self.assertTrue(runtime.wait_closed(2))
