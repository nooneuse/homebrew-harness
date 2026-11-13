"""Fault recovery and explicit capability boundaries for the converged runtime."""

import asyncio
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

import homebrew_harness.tools.files

import homebrew_harness.tools.registry
from homebrew_harness.runtime.capabilities import Capabilities
from homebrew_harness.storage.durable_runtime import DurableStore, RecoveryRequired
from homebrew_harness.execution.execution_control import cleanup_scope
from homebrew_harness.execution.execution_leases import ExecutionLeases
from homebrew_harness.runtime.session import HarnessSession
from homebrew_harness.context.memory import MemoryRuntime
from homebrew_harness.execution.model_gateway import (
    ModelGateway,
    Deadline,
    UsageLedger,
    ModelDeadlineExceeded,
)
from homebrew_harness.runtime.observability import RuntimeTrace
from tests.support.clients import fake_client, reply, call
from tests.context.test_memory import record
from homebrew_harness.orchestration.workflow_runtime import (
    WorkflowRuntime,
    WorkflowRegistry,
    WorkflowSpec,
    RunnerOutput,
)
from homebrew_harness.orchestration.worktrees import WorktreeManager
from homebrew_harness.orchestration.task_store import TaskStore


class ConvergenceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        for context in (redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)

    def session(self, responses=(), **kwargs):
        client, _ = fake_client(responses)
        session = HarnessSession(
            client, "fixture", self.root, shutdown_timeout=0.05, **kwargs
        )
        self.addCleanup(lambda: session.wait_closed(3))
        self.addCleanup(session.close)
        return session, client

    def test_normal_sqlite_wait_does_not_commit_after_deadline(self):
        store = DurableStore(self.root)
        store.put("cron", "seed", {"state": "fixture"})
        lock = sqlite3.connect(
            str(store.path), isolation_level=None, check_same_thread=False
        )
        lock.execute("BEGIN IMMEDIATE")

        def release():
            time.sleep(0.12)
            lock.execute("ROLLBACK")
            lock.close()

        thread = threading.Thread(target=release)
        thread.start()
        gateway = ModelGateway(fake_client([])[0])
        before = time.monotonic()
        with gateway.scope(deadline=Deadline.after(0.025)):
            with self.assertRaises(ModelDeadlineExceeded):
                store.put("cron", "late", {"state": "fixture"})
        self.assertLess(time.monotonic() - before, 0.1)
        thread.join(1)
        gateway.close()
        self.assertIsNone(store.get("cron", "late"))
        with gateway.scope(deadline=Deadline.after(0)), cleanup_scope():
            store.put("cron", "settled", {"state": "fixture"})
        self.assertIsNotNone(store.get("cron", "settled"))

    def test_workflow_launch_failure_leaves_no_pending_future(self):
        class Runner:
            identity = "fixture"

            def run(self, *args):
                return RunnerOutput("done")

        async def script(ctx, args):
            return await ctx.agent("fixture")

        runtime = WorkflowRuntime(
            self.root,
            Runner(),
            WorkflowRegistry(
                [WorkflowSpec("fixture", "fixture", script, {"type": "object"})]
            ),
        )
        with patch.object(
            threading.Thread, "start", side_effect=RuntimeError("injected")
        ):
            out = runtime.run("fixture", {})
        self.assertEqual(out["task"]["status"], "failed")
        self.assertEqual(runtime._workers, set())
        runtime.close()
        self.assertTrue(runtime.wait_closed(0.1))

    def test_shutdown_launch_failure_can_retry_and_release_lease(self):
        session, _ = self.session()
        session.run_user("/usage")
        with patch.object(
            threading.Thread, "start", side_effect=RuntimeError("injected")
        ):
            session.close()
        self.assertFalse(session.wait_closed(0.01))
        self.assertEqual(session.shutdown_status()["start_error"], "RuntimeError")
        session.close()
        self.assertTrue(session.wait_closed(3))
        leases = ExecutionLeases(self.root)
        leases.acquire("session")
        leases.close()

    def test_live_member_operations_cannot_be_acknowledged(self):
        session, _ = self.session(
            [
                reply(
                    calls=[
                        call(
                            "write",
                            name="write_file",
                            arguments=json.dumps(
                                {"path": "artifact.txt", "content": "fixture"}
                            ),
                        )
                    ]
                ),
                reply("done"),
            ],
            capabilities=Capabilities.full(memory_mode="off"),
        )
        session.run_user("/usage")
        task = session.team.store.create("fixture")
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        write = homebrew_harness.tools.files.run_write

        def blocked(**kwargs):
            entered.set()
            release.wait(2)
            return write(**kwargs)

        args = dict(
            name="worker", role="writer", prompt="Write fixture", task_id=task.id
        )
        with patch.dict(
            homebrew_harness.tools.registry.TOOL_HANDLERS, {"write_file": blocked}
        ):
            session.team.authorize_spawn(args)
            session.team.spawn(**args)
            self.assertTrue(entered.wait(1))
            row = session.recovery_operations()[0]
            self.assertTrue(row["active"])
            with self.assertRaises(RecoveryRequired):
                session.run_user("/recovery continue")
            with self.assertRaises(RecoveryRequired):
                session.run_user(
                    "/recovery inspect "
                    + row["operation_id"]
                    + " "
                    + str(row["revision"])
                )
            self.assertIsNotNone(
                session.runtime_store.get("operations", row["operation_id"])
            )
            release.set()
            session.team.members["worker"].thread.join(2)
        self.assertEqual((self.root / "artifact.txt").read_text(), "fixture")
        self.assertNotEqual(session.team.members["worker"].status, "failed")

    def test_damaged_optional_observation_does_not_block_recovery(self):
        client, _ = fake_client([reply("done")])
        ledger = UsageLedger(self.root, enabled=True)
        session = HarnessSession(
            client,
            "fixture",
            self.root,
            gateway=ModelGateway(client, ledger=ledger),
            shutdown_timeout=0.05,
        )
        session.run_user("hi")
        self.assertTrue(ledger.flush(1))
        sid = session.session_id
        session.close()
        self.assertTrue(session.wait_closed(3))
        ledger.flush(1)
        path = self.root / ledger.reference
        with path.open("a") as stream:
            stream.write("{")
        restored, _ = self.session(resume_session=sid)
        restored.run_user("/recovery")
        self.assertTrue(restored.gateway.summary()["observation_incomplete"])
        restored.close()
        self.assertTrue(restored.wait_closed(3))
        self.assertTrue(
            restored.runtime_store.get("session_meta", sid)["close_complete"]
        )

    def test_local_memory_queries_do_not_require_provider_health(self):
        client, _ = fake_client([])
        memory = MemoryRuntime(client, "fixture", self.root)
        memory.store.replace([record()], memory.store.snapshot().version)
        session, lead = self.session(memory_runtime=memory)
        session.run_user("/usage")
        session.gateway._state.blocked = "provider quota unavailable"
        self.assertTrue(json.loads(session.run_user("/memory")))
        lead.chat.completions.create.assert_not_called()
        client.chat.completions.create.assert_not_called()

    def test_sqlite_replacement_is_rejected_before_external_initialization(self):
        outside = self.root / "outside"
        outside.mkdir(mode=0o700)
        external = outside / "runtime.sqlite3"
        external.touch(mode=0o600)
        directory = self.root / ".harness/runtime/database"
        directory.mkdir(parents=True, mode=0o700)
        for parent in (self.root / ".harness", self.root / ".harness/runtime"):
            parent.chmod(0o700)
        (directory / "runtime.sqlite3").touch(mode=0o600)
        original = sqlite3.connect
        changed = False

        def replace(*args, **kwargs):
            nonlocal changed
            if not changed:
                changed = True
                directory.rename(directory.with_name("original"))
                directory.symlink_to(outside, target_is_directory=True)
            return original(*args, **kwargs)

        with patch(
            "homebrew_harness.storage.sqlite_store.sqlite3.connect", side_effect=replace
        ):
            with self.assertRaises(ValueError):
                DurableStore(self.root).put("cron", "fixture", {"state": "fixture"})
        with original(str(external)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM sqlite_master WHERE type='table'"
                ).fetchone()[0],
                0,
            )

    def test_trace_replacement_cannot_create_external_log(self):
        outside = self.root / "outside"
        outside.mkdir()
        trace = RuntimeTrace(self.root)
        original = os.open
        changed = False

        def replace(path, *args, **kwargs):
            nonlocal changed
            if str(path) == "events.jsonl" and not changed:
                changed = True
                parent = trace.path.parent
                parent.rename(parent.with_name("original"))
                parent.symlink_to(outside, target_is_directory=True)
            return original(path, *args, **kwargs)

        with patch(
            "homebrew_harness.runtime.observability.os.open", side_effect=replace
        ):
            trace.emit("fixture")
        self.assertTrue(changed)
        self.assertFalse((outside / "events.jsonl").exists())

    def test_git_status_never_invokes_repository_fsmonitor(self):
        repository = self.root / "repo"
        repository.mkdir()
        hook = self.root / "monitor.sh"
        marker = self.root / "executed"
        hook.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\nprintf "fixture\\0"\n')
        hook.chmod(0o700)
        env = {
            **os.environ,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
        for args in (["init", "-q"], ["config", "core.fsmonitor", str(hook)]):
            subprocess.run(
                ["git", *args], cwd=repository, env=env, check=True, capture_output=True
            )
        (repository / "fixture.txt").write_text("fixture")
        subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "add", "fixture.txt"],
            cwd=repository,
            env=env,
            check=True,
            capture_output=True,
        )
        store = TaskStore(repository)
        task = store.create("fixture")
        with self.assertRaises(ValueError):
            WorktreeManager(repository, store).create("fixture", task.id)
        store.close()
        self.assertFalse(marker.exists())

    def test_core_ignores_unused_cron_and_exposes_only_core_tools(self):
        (self.root / ".scheduled_tasks.json").write_text("invalid fixture")
        session, client = self.session([reply("done")])
        self.assertEqual(session.run_user("hi"), "done")
        names = {
            tool["function"]["name"]
            for tool in client.chat.completions.create.call_args.kwargs["tools"]
        }
        self.assertEqual(
            names,
            {
                "bash",
                "read_file",
                "write_file",
                "edit_file",
                "glob",
                "search_text",
                "git_status",
                "git_diff",
                "todo_write",
                "load_skill",
                "compact",
            },
        )
        self.assertIsNone(session.team)
        self.assertIsNone(session.cron)
        self.assertIsNone(session.workflow)
        self.assertIsNone(session.background)

    def test_profile_mismatch_does_not_change_closed_session_proof(self):
        first, _ = self.session([reply("done")])
        first.run_user("hi")
        sid = first.session_id
        first.close()
        self.assertTrue(first.wait_closed(3))
        previous = first.runtime_store.get("session_meta", sid)
        other, _ = self.session(
            resume_session=sid, capabilities=Capabilities.full(memory_mode="off")
        )
        with self.assertRaises(RecoveryRequired):
            other.run_user("/recovery")
        self.assertEqual(other.runtime_store.get("session_meta", sid), previous)

    def test_telemetry_failure_does_not_discard_successful_model_response(self):
        client, _ = fake_client([reply("done")])
        ledger = UsageLedger(self.root, enabled=True)
        with patch.object(ledger, "append", side_effect=OSError("fixture failure")):
            gateway = ModelGateway(client, ledger=ledger)
            self.assertEqual(
                gateway.request(model="fixture", messages=[], max_tokens=16).text,
                "done",
            )
        self.assertFalse(gateway.blocked)
        self.assertTrue(gateway.summary()["observation_incomplete"])
        gateway.close()

    def test_inspection_targets_one_batch_when_call_ids_repeat(self):
        from homebrew_harness.context.tracked_history import encoded_message

        session, _ = self.session()
        session.run_user("/usage")
        first = {
            "role": "assistant",
            "content": "first",
            "tool_calls": [
                {
                    "id": "same",
                    "type": "function",
                    "function": {"name": "write_file", "arguments": "{}"},
                }
            ],
        }
        second = {
            "role": "assistant",
            "content": "second",
            "tool_calls": [
                {
                    "id": "same",
                    "type": "function",
                    "function": {"name": "write_file", "arguments": "{}"},
                }
            ],
        }
        unknown = json.dumps({"outcome": "unknown", "error": "fixture"})
        session.messages.extend(
            [
                first,
                {"role": "tool", "tool_call_id": "same", "content": unknown},
                second,
                {"role": "tool", "tool_call_id": "same", "content": unknown},
            ]
        )
        for identity, batch in (("one", first), ("two", second)):
            session.runtime_store.put(
                "operations",
                identity,
                {
                    "checkpoint": session.checkpoint_key,
                    "call_id": "same",
                    "state": "unknown",
                    "revision": 1,
                    "batch_id": encoded_message(batch)[1],
                },
            )
        session._save_checkpoint()
        session.run_user("/recovery inspect one 1")
        results = [
            json.loads(m["content"])["outcome"]
            for m in session.messages
            if m["role"] == "tool"
        ]
        self.assertEqual(results, ["inspected", "unknown"])
        self.assertEqual(
            session.runtime_store.get("operations", "two")["state"], "unknown"
        )
        with self.assertRaises(RecoveryRequired):
            session.run_user("/recovery continue")
        with self.assertRaises(RecoveryRequired):
            session.run_user("/recovery inspect one 1")

    def test_goal_custom_usage_cannot_produce_negative_unknown_count(self):
        from homebrew_harness.orchestration.goal_loop import (
            GoalController,
            GoalEvaluation,
        )

        class Evaluator:
            total_tokens = 0
            known_usage_calls = 0

            def evaluate(self, packet):
                self.total_tokens += 10
                self.known_usage_calls += 1
                return GoalEvaluation(False, "fixture")

        goal = GoalController(self.root, Evaluator())
        goal.set_goal("fixture")
        try:
            self.assertEqual(goal.evaluate_after_turn("fixture").action, "block")
            self.assertEqual(goal.status()["progress"]["judge_attempts"], 1)
            self.assertNotIn("judge_unknown_responses", goal.status()["progress"])
        finally:
            goal.close()

    def test_unchanged_prepare_preserves_incremental_append_and_original_history(self):
        from homebrew_harness.context.tracked_history import ConversationHistory
        from homebrew_harness.context.compaction import ContextCompactor

        client, _ = fake_client([])
        compactor = ContextCompactor(client, "fixture", self.root)
        messages = ConversationHistory(
            [
                {"role": "system", "content": "policy"},
                {"role": "user", "content": "fixture"},
            ]
        )
        messages[:] = compactor.prepare(messages, "fixture", tools=[])
        previous = messages.version
        message = messages[2]
        messages.append({"role": "assistant", "content": "new"})
        messages[:] = compactor.prepare(messages, "fixture", tools=[])
        self.assertIs(messages[2], message)
        self.assertEqual(len(messages.append_delta(previous)), 1)

    def test_explicit_task_capability_routes_worktree_without_team_actors(self):
        session, _ = self.session(capabilities=Capabilities({"tasks", "worktrees"}))
        session.run_user("/usage")
        self.assertIsNone(session.team)
        self.assertIsNotNone(session.workspace.store)
        task = session.workspace.store.create("fixture")
        session.workspace.lead_claim(task.id, workdir=self.root)
        self.assertEqual(session.workspace.lead_cwd(), self.root)
        session.workspace.lead_complete(task.id, workdir=self.root)
        session.workspace.end_lead_turn()
        self.assertIsNone(session.workspace.lead_assignment)

    def test_disabled_workflow_cannot_be_invoked_by_invented_model_call(self):
        session, client = self.session(
            [
                reply(
                    calls=[
                        call(
                            "invented",
                            name="workflow",
                            arguments='{"name":"review-changes"}',
                        )
                    ]
                ),
                reply("done"),
            ]
        )
        self.assertEqual(session.run_user("Read the project"), "done")
        output = next(m["content"] for m in session.messages if m["role"] == "tool")
        self.assertIn("Unknown tool", output)
        self.assertFalse((self.root / ".harness/workflows").exists())

    def test_valid_json_with_invalid_observed_counters_is_only_degraded(self):
        client, _ = fake_client([])
        ledger = UsageLedger(self.root, enabled=True)
        gateway = ModelGateway(client, ledger=ledger)
        snapshot = gateway.summary()
        directory = self.root / ".harness/usage"
        directory.mkdir(parents=True)
        invalid = {**snapshot, "sequence": 1, "known_tokens": "invalid"}
        (self.root / ledger.reference).write_text(
            json.dumps({"event": "usage_snapshot", "observation": invalid}) + "\n"
        )
        restored = ModelGateway(client, workdir=self.root)
        restored.restore_usage(snapshot)
        self.assertEqual(restored.summary()["known_tokens"], 0)
        self.assertTrue(restored.summary()["observation_incomplete"])
        gateway.close()
        restored.close()

    def test_core_resume_keeps_tool_selection_and_actual_usage(self):
        first, _ = self.session([reply("done")])
        first.run_user("hi")
        sid = first.session_id
        first.close()
        self.assertTrue(first.wait_closed(3))
        second, client = self.session([reply("continued")], resume_session=sid)
        second.run_user("/usage")
        self.assertEqual(second.gateway.summary()["calls"], 1)
        self.assertEqual(second.run_user("Continue"), "continued")
        self.assertEqual(second.gateway.summary()["calls"], 2)
        self.assertIsNone(second.team)

    def test_launch_interruption_after_acceptance_keeps_real_worker_owned(self):
        from homebrew_harness.runtime.runtime_control import Workers, WorkAccepted

        workers = Workers()
        release = threading.Event()
        self.addCleanup(release.set)
        original = threading.Thread.start

        def interrupted(thread):
            original(thread)
            raise RuntimeError("injected after acceptance")

        with patch.object(threading.Thread, "start", interrupted):
            with self.assertRaises(WorkAccepted):
                workers.start(lambda: release.wait(2))
        self.assertFalse(workers.wait(0.01))
        release.set()
        self.assertTrue(workers.wait(1))

    def test_shutdown_accepted_interruption_does_not_start_two_closers(self):
        from homebrew_harness.runtime.runtime_control import Shutdown

        closer = Shutdown()
        release = threading.Event()
        self.addCleanup(release.set)
        count = []
        original = threading.Thread.start

        def action():
            count.append(1)
            release.wait(2)

        def interrupted(thread):
            original(thread)
            raise RuntimeError("injected after acceptance")

        with patch.object(threading.Thread, "start", interrupted):
            closer.request(action, 0.01)
        closer.request(action, 0.01)
        self.assertEqual(count, [1])
        release.set()
        self.assertTrue(closer.finished.wait(1))

    def test_host_configuration_has_blank_defaults_and_independent_memory(self):
        import argparse
        from homebrew_harness.runtime.capabilities import add_arguments, from_args

        parser = argparse.ArgumentParser()
        add_arguments(parser)
        with patch.dict(os.environ, {"HARNESS_PROFILE": "", "HARNESS_MEMORY_MODE": ""}):
            self.assertEqual(
                from_args(parser.parse_args([])).manifest(), Capabilities().manifest()
            )
        with patch.dict(os.environ, {"HARNESS_MEMORY_MODE": "auto"}):
            self.assertEqual(
                from_args(parser.parse_args(["--profile", "full"])).memory_mode, "auto"
            )
            self.assertEqual(
                from_args(
                    parser.parse_args(["--profile", "full", "--memory-mode", "off"])
                ).memory_mode,
                "off",
            )

    def test_model_launch_interruption_retains_inflight_until_actual_return(self):
        from homebrew_harness.execution.model_gateway import ModelGatewayError

        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        client, _ = fake_client([reply("done")])
        create = client.chat.completions.create.side_effect
        client.chat.completions.create.side_effect = lambda **kwargs: (
            entered.set(),
            release.wait(2),
            create(**kwargs),
        )[2]
        gateway = ModelGateway(client)
        original = threading.Thread.start

        def interrupted(thread):
            original(thread)
            raise RuntimeError("injected after acceptance")

        with patch.object(threading.Thread, "start", interrupted):
            with self.assertRaises(ModelGatewayError):
                gateway.request(model="fixture", messages=[], max_tokens=16)
        self.assertTrue(entered.wait(1))
        self.assertEqual(gateway.inflight, 1)
        release.set()
        self.assertTrue(gateway.wait_idle(1))
        gateway.close()
