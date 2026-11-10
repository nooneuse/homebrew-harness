"""Post-convergence regressions: exercise the next step after a failure."""

from tests.support.clients import model_binding

from tests.support.clients import cli_model_factory

import io
import asyncio
import json
import os
from pathlib import Path
import tempfile
import subprocess
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch, MagicMock
from types import SimpleNamespace

import homebrew_harness.cli
import homebrew_harness.session_cli

import homebrew_harness.configuration

import homebrew_harness.context.compaction

import homebrew_harness.runtime.assembly

import homebrew_harness.runtime.loop
from homebrew_harness.execution.background import BackgroundManager
from homebrew_harness.runtime.capabilities import Capabilities
from homebrew_harness.storage.durable_runtime import RecoveryRequired
from homebrew_harness.execution.execution_leases import ExecutionLeases
from homebrew_harness.runtime.session import HarnessSession
from homebrew_harness.execution.model_gateway import (
    Deadline,
    ModelDeadlineExceeded,
    ModelGateway,
    GatewayLimits,
)
from homebrew_harness.security.permissions import PermissionDecision
from homebrew_harness.orchestration.teams import TeammateRuntime
from tests.support.clients import call, fake_client, reply
from homebrew_harness.context.tracked_history import (
    ConversationHistory,
    encoded_message,
)


class FollowupTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        for context in (redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)

    def session(self, responses=(), **kwargs):
        client, snapshots = fake_client(responses)
        session = HarnessSession(
            client, "fixture", self.root, shutdown_timeout=0.05, **kwargs
        )
        self.addCleanup(lambda: session.wait_closed(3))
        self.addCleanup(session.close)
        return session, client, snapshots

    def test_accepted_background_interrupt_blocks_batch_and_retains_identity(self):
        release, entered = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def command(*args, **kwargs):
            entered.set()
            release.wait(3)
            return '{"exit_code":0,"stdout":"done","stderr":""}'

        manager = BackgroundManager(self.root, command)
        session, _, _ = self.session(
            [
                reply(
                    calls=[
                        call(
                            "start",
                            name="bash",
                            arguments='{"command":"fixture","run_in_background":true}',
                        ),
                        call(
                            "later",
                            name="write_file",
                            arguments='{"path":"later.txt","content":"wrong"}',
                        ),
                    ]
                )
            ],
            background_manager=manager,
        )
        original = threading.Thread.start

        def start(thread):
            original(thread)
            if thread.name.startswith("background-"):
                raise RuntimeError("injected after acceptance")

        with (
            patch.object(threading.Thread, "start", start),
            patch(
                "homebrew_harness.security.permissions.ask_user",
                return_value=PermissionDecision("allow", "fixture"),
            ),
        ):
            with self.assertRaises(RecoveryRequired):
                session.run_user("Start independent work")
        self.assertTrue(entered.wait(1))
        result = json.loads(
            next(
                m["content"]
                for m in session.messages
                if m.get("tool_call_id") == "start"
            )
        )
        self.assertEqual(result["outcome"], "unknown")
        self.assertTrue(result["accepted"])
        self.assertEqual(manager.snapshot()[0]["bg_id"], result["bg_id"])
        self.assertFalse((self.root / "later.txt").exists())
        release.set()
        self.assertTrue(manager.wait_closed(2))
        self.assertEqual(
            json.loads(manager.check(result["bg_id"], workdir=self.root))["status"],
            "completed",
        )
        operation = session.recovery_operations()[0]
        session.run_user(
            "/recovery inspect "
            + operation["operation_id"]
            + " "
            + str(operation["revision"])
        )
        self.assertFalse(session.recovery_operations())

    def test_inspection_without_result_survives_restart_and_member_resume(self):
        caps = Capabilities({"teams"})
        first, _, _ = self.session(capabilities=caps)
        first.run_user("/usage")
        member = TeammateRuntime(first.team, "peer", "reader", "fixture task", False)
        member.messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    call(
                        "write",
                        name="write_file",
                        arguments='{"path":"artifact.txt","content":"fixture"}',
                    )
                ],
            }
        )
        first.team.members["peer"] = member
        member.model_binding = first.team.model_selector(member.role, None)
        first.team.persist_checkpoint()
        key = "member:" + first.session_id + ":peer"
        first.runtime_store.put(
            "checkpoints",
            key,
            {
                "session_id": first.session_id,
                "messages": member.messages,
                "phase": "running",
            },
        )
        first.runtime_store.put(
            "operations",
            "unfinished",
            {
                "checkpoint": key,
                "state": "started",
                "revision": 0,
                "name": "write_file",
                "call_id": "write",
                "cwd": ".",
                "batch_id": encoded_message(member.messages[-1])[1],
            },
        )
        first.run_user("/recovery inspect unfinished 0")
        self.assertNotIn("output", first.runtime_store.get("operations", "unfinished"))
        saved = first.runtime_store.get("checkpoints", key)["messages"]
        checked = json.loads(saved[-1]["content"])
        self.assertEqual(checked["outcome"], "inspected")
        self.assertNotIn("exit_code", checked)
        sid = first.session_id
        first.close()
        self.assertTrue(first.wait_closed(3))
        restored, client, snapshots = self.session(
            [reply("Checked history received")], capabilities=caps, resume_session=sid
        )
        restored.run_user("/recovery")
        args = dict(name="peer", role="reader", prompt="Resume checked task")
        restored.team.authorize_spawn(args)
        restored.team.spawn(**args)
        end = time.monotonic() + 2
        while not snapshots and time.monotonic() < end:
            threading.Event().wait(0.01)
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertTrue(
            any(
                m.get("role") == "tool"
                and json.loads(m["content"]).get("host_inspected")
                for m in snapshots[0]
            )
        )
        self.assertFalse((self.root / "artifact.txt").exists())

    def test_expired_resume_allows_local_control_but_no_new_model_action(self):
        first, _, _ = self.session([reply("done")])
        first.run_user("hi")
        sid = first.session_id
        first.gateway._state.deadline = Deadline.after(0)
        first.close()
        self.assertTrue(first.wait_closed(3))
        restored, client, _ = self.session(resume_session=sid)
        self.assertEqual(json.loads(restored.run_user("/usage"))["calls"], 1)
        self.assertEqual(json.loads(restored.run_user("/recovery"))["session_id"], sid)
        with self.assertRaises(ModelDeadlineExceeded):
            restored.run_user("Continue")
        client.chat.completions.create.assert_not_called()
        restored.close()
        self.assertTrue(restored.wait_closed(3))
        with ExecutionLeases(self.root).use("session"):
            pass

    def test_initialization_compensation_read_failure_still_releases_session(self):
        session, _, _ = self.session()

        def failed(deadline):
            session.workspace.acquire_session()
            raise ValueError("original initialization error")

        with (
            patch.object(session, "_initialize_runtime", failed),
            patch.object(
                session.runtime_store,
                "get",
                side_effect=OSError("unavailable metadata"),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "original initialization error"):
                session.run_user("/usage")
        self.assertFalse(session.workspace.leases.held("session"))
        self.assertTrue(
            any(e["component"] == "initialization" for e in session._cleanup_errors)
        )
        with ExecutionLeases(self.root).use("session"):
            pass

    def git(self, *args):
        env = {
            "PATH": os.defpath,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
        return subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args],
            cwd=self.root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )

    def init_git(self):
        self.git("init", "-q")
        (self.root / ".gitignore").write_text(".tasks/\n.harness/\n.worktrees/\n")
        (self.root / ".gitattributes").write_text("sample.txt filter=fixture\n")
        (self.root / "sample.txt").write_text("fixture")
        self.git("add", ".")
        self.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        )

    def test_git_rejects_included_conditional_and_worktree_filters_before_checkout(
        self,
    ):
        from homebrew_harness.orchestration.task_store import TaskStore
        from homebrew_harness.orchestration.worktrees import WorktreeManager

        self.init_git()
        marker = self.root / ".git/filter-ran"
        script = self.root / ".git/smudge.sh"
        script.write_text("#!/bin/sh\ncat\ntouch " + str(marker) + "\n")
        script.chmod(0o700)
        include = self.root / ".git/filters.conf"
        include.write_text('[filter "fixture"]\n smudge = ' + str(script) + "\n")
        store = TaskStore(self.root)
        self.addCleanup(store.close)
        task = store.create("Fixture")
        manager = WorktreeManager(self.root, store)
        cases = [
            ("local", "filter.fixture.smudge", str(script)),
            ("local", "include.path", str(include)),
            ("local", "includeIf.onbranch:codex/wt/*.path", str(include)),
            ("worktree", "filter.fixture.smudge", str(script)),
            ("worktree", "include.path", str(include)),
        ]
        self.git("config", "extensions.worktreeConfig", "true")
        for scope, key, value in cases:
            with self.subTest(scope=scope, key=key):
                self.git("config", "--" + scope, key, value)
                try:
                    with self.assertRaisesRegex(
                        ValueError, "includes and checkout filters"
                    ):
                        manager.create("fixture", task.id)
                    self.assertFalse(marker.exists())
                    self.assertFalse((self.root / ".worktrees/fixture").exists())
                finally:
                    self.git("config", "--" + scope, "--unset", key)

    def test_independent_worktree_tool_creates_real_checkout_without_team(self):
        self.init_git()
        session, client, _ = self.session(capabilities=Capabilities({"worktrees"}))
        session.run_user("/usage")
        task = session.workspace.store.create("Fixture")
        client.chat.completions.create.side_effect = [
            reply(
                calls=[
                    call(
                        "wt",
                        name="create_worktree",
                        arguments=json.dumps({"name": "fixture", "task_id": task.id}),
                    )
                ]
            ),
            reply("Created"),
        ]
        session.run_user("Create the task worktree")
        result = json.loads(
            next(
                m["content"] for m in session.messages if m.get("tool_call_id") == "wt"
            )
        )
        self.assertIn("task_id", result, result)
        self.assertEqual(result["task_id"], task.id)
        self.assertEqual(session.workspace.store.load(task.id).worktree, "fixture")
        self.assertTrue((self.root / ".worktrees/fixture/sample.txt").exists())
        self.assertIsNone(session.team)

    def test_team_without_worktree_does_not_construct_or_advertise_worktree(self):
        session, client, _ = self.session(
            [reply("done")], capabilities=Capabilities({"teams"})
        )
        session.run_user("hi")
        self.assertIsNone(session.workspace.worktrees)
        tools = client.chat.completions.create.call_args.kwargs["tools"]
        self.assertNotIn("create_worktree", [t["function"]["name"] for t in tools])
        self.assertNotIn("create_worktree", session.workspace.handlers())

    def test_unscoped_member_cannot_auto_or_manually_claim_unrelated_task(self):
        from homebrew_harness.orchestration.teams import TeamManager

        manager = TeamManager(self.root, lambda member: "done", scan_interval=0.01)
        self.addCleanup(lambda: manager.wait_closed(3))
        self.addCleanup(manager.close)
        task = manager.store.create("Unrelated")
        args = dict(name="peer", role="reader", prompt="One local task")
        manager.authorize_spawn(args)
        manager.spawn(**args)
        member = manager.members["peer"]
        end = time.monotonic() + 1
        while member.status != "idle" and time.monotonic() < end:
            threading.Event().wait(0.01)
        self.assertEqual(member.status, "idle")
        self.assertIsNone(member.store.claim_next(member.allowed_tasks))
        with self.assertRaisesRegex(ValueError, "host-approved task scope"):
            member.claim(task.id, workdir=self.root)
        self.assertEqual(manager.store.load(task.id).status, "pending")

    def test_fresh_spawn_approval_does_not_restore_old_task_scope(self):
        from homebrew_harness.orchestration.teams import TeamManager

        manager = TeamManager(self.root, lambda member: "done")
        self.addCleanup(lambda: manager.wait_closed(3))
        self.addCleanup(manager.close)
        task = manager.store.create("Old scope")
        member = TeammateRuntime(manager, "peer", "reader", "Old", False)
        member.allowed_tasks = frozenset({task.id})
        manager.recovered_members = {
            "peer": {
                **manager.checkpoint()["members"].get("peer", {}),
                "role": "reader",
                "task_id": None,
                "messages": [{"role": "user", "content": "Old"}],
                "allowed_tasks": [task.id],
            }
        }
        args = dict(name="peer", role="reader", prompt="New approval")
        manager.authorize_spawn(args)
        manager.spawn(**args)
        self.assertEqual(manager.members["peer"].allowed_tasks, frozenset())

    def test_cli_context_limit_reaches_actual_session(self):
        instances = []
        sdk = MagicMock()
        sdk.__enter__.return_value = sdk

        def construct(*args, **kwargs):
            value = HarnessSession(*args, **kwargs)
            instances.append(value)
            return value

        with (
            patch.dict(
                os.environ,
                {
                    name: "fixture"
                    for name in homebrew_harness.configuration.CONFIG_VARIABLES
                },
            ),
            patch("homebrew_harness.session_cli.load_dotenv"),
            cli_model_factory( return_value=sdk),
            patch(
                "homebrew_harness.runtime.session.HarnessSession", side_effect=construct
            ),
        ):
            result = homebrew_harness.cli.main(
                ["session"]
                + [
                    "--profile",
                    "core",
                    "--context-chars",
                    "12000",
                    "--cwd",
                    str(self.root),
                    "/usage",
                ]
            )
        self.assertEqual(result, 0)
        self.assertEqual(instances[0].compactor.context_chars, 12000)
        self.assertTrue(instances[0].wait_closed(3))

    def test_standalone_core_does_not_construct_task_store(self):
        client, _ = fake_client([reply("done")])
        with patch(
            "homebrew_harness.runtime.assembly.TaskStore",
            side_effect=AssertionError("unexpected optional construction"),
        ):
            self.assertEqual(
                homebrew_harness.runtime.loop.agent_loop(
                    client,
                    "fixture",
                    [{"role": "user", "content": "hi"}],
                    self.root,
                    capabilities=Capabilities(),
                ),
                "done",
            )

    def test_standalone_worktree_capability_routes_the_registered_handler(self):
        from homebrew_harness.orchestration.task_store import TaskStore

        self.init_git()
        store = TaskStore(self.root)
        self.addCleanup(store.close)
        task = store.create("Standalone checkout")
        client, _ = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "wt",
                            name="create_worktree",
                            arguments=json.dumps(
                                {"name": "standalone", "task_id": task.id}
                            ),
                        )
                    ]
                ),
                reply("done"),
            ]
        )
        messages = [{"role": "user", "content": "Create the checkout"}]
        homebrew_harness.runtime.loop.agent_loop(
            client,
            "fixture",
            messages,
            self.root,
            capabilities=Capabilities({"worktrees"}),
        )
        result = json.loads(
            next(m["content"] for m in messages if m.get("tool_call_id") == "wt")
        )
        self.assertEqual(result["task_id"], task.id)
        self.assertTrue((self.root / ".worktrees/standalone/sample.txt").exists())

    def test_child_compactor_inherits_session_context_limit(self):
        session, _, _ = self.session(
            [
                reply(
                    calls=[
                        call(
                            "child",
                            name="task",
                            arguments='{"prompt":"Explain the supplied fact"}',
                        )
                    ]
                ),
                reply("Child explanation"),
                reply("Final explanation"),
            ],
            context_chars=12000,
            capabilities=Capabilities({"subagents"}),
        )
        limits = []
        original = homebrew_harness.context.compaction.ContextCompactor.prepare

        def prepare(compactor, *args, **kwargs):
            limits.append(compactor.context_chars)
            return original(compactor, *args, **kwargs)

        with patch.object(
            homebrew_harness.context.compaction.ContextCompactor, "prepare", prepare
        ):
            session.run_user("Delegate a local explanation")
        self.assertEqual(limits, [12000, 12000, 12000])
        self.assertEqual(session.gateway.summary()["by_role"]["subagent"]["calls"], 1)

    def test_real_session_append_work_is_independent_of_retained_history_length(self):
        from homebrew_harness.storage.storage_metrics import metrics_for

        session, _, _ = self.session(
            [
                reply(
                    calls=[
                        call("read", name="read_file", arguments='{"path":"note.txt"}')
                    ]
                ),
                reply("done"),
            ],
            context_chars=100000,
        )
        session.compactor.trigger_messages = (
            1000  # Measure retained history, without requesting snip compaction.
        )
        (self.root / "note.txt").write_text("fixture")
        session.run_user("/usage")
        session.intent.update("Read note.txt")
        session.messages.extend(
            [
                {
                    "role": "user" if index % 2 == 0 else "assistant",
                    "content": "History " + str(index),
                }
                for index in range(200)
            ]
        )
        homebrew_harness.runtime.loop.set_runtime_context(
            session.messages, self.root, team_manager=session.workspace
        )
        session.messages[:] = session.compactor.prepare(
            session.messages, "Read note.txt", tools=[]
        )
        session._save_checkpoint()
        before = metrics_for(self.root).snapshot()["messages_examined"]
        self.assertEqual(session.run_user("Read note.txt"), "done")
        examined = metrics_for(self.root).snapshot()["messages_examined"] - before
        self.assertLess(examined, 20, examined)
        self.assertEqual(
            sum(
                (m.get("content") or "").startswith("History ")
                for m in session.messages
            ),
            200,
        )
        self.assertIn(
            "fixture",
            next(
                m["content"]
                for m in session.messages
                if m.get("tool_call_id") == "read"
            ),
        )

    def test_async_gateway_cancellation_retains_sent_work_and_does_not_block_loop(self):
        release, entered = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def create(**kwargs):
            entered.set()
            release.wait(3)
            return SimpleNamespace(usage=SimpleNamespace(total_tokens=17))

        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        gateway = ModelGateway(client, GatewayLimits(concurrency=1))
        self.addCleanup(gateway.close)

        async def scenario():
            first = asyncio.create_task(gateway.arequest(model="fixture", messages=[]))
            while not entered.is_set():
                await asyncio.sleep(0.005)
            second = asyncio.create_task(gateway.arequest(model="fixture", messages=[]))
            await asyncio.sleep(0.03)
            second.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await second
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            self.assertEqual(gateway.inflight, 1)
            self.assertEqual(gateway.summary()["calls"], 1)
            release.set()

        asyncio.run(scenario())
        self.assertTrue(gateway.wait_idle(2))
        self.assertEqual(gateway.summary()["known_tokens"], 17)
        self.assertEqual(gateway.summary()["late_calls"], 1)

    def test_virtual_result_fast_path_does_not_hide_intervening_history_change(self):
        session, _, _ = self.session(
            [
                reply(
                    calls=[
                        call("read", name="read_file", arguments='{"path":"note.txt"}')
                    ]
                ),
                reply("done"),
            ]
        )
        (self.root / "note.txt").write_text("fixture")
        session.run_user("/usage")
        session.messages.append(
            {"role": "assistant", "content": "reference before tool"}
        )
        reference = session.messages[-1]
        session.hooks.register_hook(
            "PostToolUse", lambda *args: reference.update(content="updated reference")
        )
        session.run_user("Read note.txt")
        saved = session.runtime_store.get("checkpoints", session.checkpoint_key)[
            "messages"
        ]
        self.assertTrue(any(m.get("content") == "updated reference" for m in saved))
        self.assertEqual(sum(m.get("tool_call_id") == "read" for m in saved), 1)

    def test_member_constraint_refresh_preserves_append_events_and_applies_amendments(
        self,
    ):
        from homebrew_harness.context.task_intent import TaskIntent
        from homebrew_harness.orchestration.teams import TeamManager

        manager = TeamManager(self.root, lambda member: "done")
        self.addCleanup(manager.close)
        manager.task_intent = TaskIntent("Preserve the source files")
        member = TeammateRuntime(manager, "peer", "reader", "Inspect a fact", False)
        manager.members[member.name] = member
        previous = member.messages.version
        member.messages.append({"role": "assistant", "content": "Observation"})
        member.before_model()
        delta = member.messages.append_delta(previous)
        self.assertIsNotNone(delta)
        self.assertEqual([m["content"] for m in delta], ["Observation"])
        manager.task_intent.update("Also inspect tests")
        member.before_model()
        inherited = next(
            m for m in member.messages if m.get("name") == "inherited_task_constraints"
        )
        self.assertIn("Also inspect tests", inherited["content"])

    def test_builtin_workflow_uses_one_execution_owner_and_gateway_transport(
        self,
    ):
        from homebrew_harness.orchestration.workflow_runtime import (
            WorkflowRuntime,
            WorkflowRegistry,
            WorkflowSpec,
            ModelWorkflowRunner,
        )

        response = reply("saved")
        from types import SimpleNamespace

        response.usage = SimpleNamespace(
            total_tokens=23, prompt_tokens=0, completion_tokens=23
        )
        client, _ = fake_client([response])

        async def script(ctx, args):
            return await ctx.agent("fixture")

        runtime = WorkflowRuntime(
            self.root,
            ModelWorkflowRunner(model_binding(client, "fixture")),
            WorkflowRegistry(
                [WorkflowSpec("fixture", "fixture", script, {"type": "object"})]
            ),
        )
        self.addCleanup(runtime.close)
        with patch.object(
            runtime._threads, "start", wraps=runtime._threads.start
        ) as start:
            result = runtime.run("fixture", {})
        self.assertEqual(
            [call.kwargs.get("name") for call in start.call_args_list],
            ["workflow-execution"],
        )
        self.assertEqual(result["task"]["status"], "completed")
        rows = [
            json.loads(line)
            for line in runtime.store.path(result["task"]["run_id"], ".journal.jsonl")
            .read_text()
            .splitlines()
        ]
        self.assertEqual([row["type"] for row in rows], ["checkpoint"])
        self.assertEqual(runtime.runner.client.summary()["known_tokens"], 23)
        self.assertEqual(
            runtime.runner.client.summary()["by_purpose"]["workflow_step"]["calls"], 1
        )
        self.assertNotIn("tokens", result["task"]["progress"])

    def test_trace_rotates_at_bound_and_observation_failure_does_not_escape(self):
        from homebrew_harness.runtime.observability import RuntimeTrace

        trace = RuntimeTrace(self.root, max_bytes=512)
        for index in range(20):
            trace.emit("fixture", index=index, text="x" * 80)
        files = list(trace.path.parent.glob("events*.jsonl"))
        self.assertEqual(len(files), 2)
        self.assertTrue(all(path.stat().st_size <= 512 for path in files))
        rows = [
            json.loads(line) for path in files for line in path.read_text().splitlines()
        ]
        self.assertTrue(any(row["index"] == 19 for row in rows))
        with patch(
            "homebrew_harness.runtime.observability.os.open",
            side_effect=OSError("fixture full disk"),
        ):
            trace.emit("failed")
        self.assertEqual(trace.status()["dropped"], 1)
