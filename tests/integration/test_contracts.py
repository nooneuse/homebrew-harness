from tests.support.tools import fixture_invocation
"""Host contracts across delegation, persistence, lifecycle and effective tools."""

import asyncio
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import Mock, patch
from types import SimpleNamespace

import homebrew_harness.context.compaction

import homebrew_harness.runtime.loop

import homebrew_harness.tools.catalog

import homebrew_harness.tools.registry

import homebrew_harness.tools.tool_pool
from homebrew_harness.runtime.capabilities import Capabilities
from homebrew_harness.runtime.session import HarnessSession
from homebrew_harness.storage.durable_runtime import RecoveryRequired
from homebrew_harness.execution.execution_leases import ExecutionLeases
from homebrew_harness.execution.model_gateway import ModelCancelled
from homebrew_harness.security.permissions import PermissionDecision
from homebrew_harness.execution.background import BackgroundManager
from tests.support.clients import fake_client, reply, call
from homebrew_harness.orchestration.workflow_runtime import (
    WorkflowRuntime,
    WorkflowRegistry,
    WorkflowSpec,
    WorkflowJournal,
    RunnerOutput,
    WorkflowError,
)
from homebrew_harness.orchestration.teams import TeammateRuntime


class ContractTests(unittest.TestCase):
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

    def test_delegation_intersects_parent_allowlist_for_discovery_and_execution(self):
        client, snapshots = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "child",
                            name="task",
                            arguments='{"prompt":"Read the supplied fact"}',
                        )
                    ]
                ),
                reply(
                    calls=[
                        call(
                            "write",
                            name="write_file",
                            arguments='{"path":"wrong.txt","content":"wrong"}',
                        ),
                        call("bash", "printf wrong"),
                        call(
                            "tests", name="run_tests", arguments='{"name":"unittest"}'
                        ),
                    ]
                ),
                reply("Checked"),
                reply("done"),
            ]
        )
        bash = Mock()
        with patch.dict(homebrew_harness.tools.registry.TOOL_HANDLERS, {"bash": bash}):
            messages = [{"role": "user", "content": "Delegate a read task"}]
            homebrew_harness.runtime.loop.agent_loop(
                client,
                "fixture",
                messages,
                self.root,
                capabilities=Capabilities({"subagents"}),
                tool_allowlist=frozenset({"task", "read_file"}),
            )
        self.assertEqual(
            {
                t["function"]["name"]
                for t in snapshots
                and client.chat.completions.create.call_args_list[1].kwargs["tools"]
            },
            {"read_file"},
        )
        self.assertFalse((self.root / "wrong.txt").exists())
        bash.assert_not_called()
        child_results = [m for m in snapshots[2] if m.get("role") == "tool"]
        self.assertTrue(
            all(
                json.loads(m["content"])["error"] == "Unknown tool."
                for m in child_results
            )
        )

    def test_dynamic_mcp_tool_cannot_escape_parent_scope_in_child(self):
        definition = homebrew_harness.tools.catalog.define_tool(
            "mcp__fixture__write", "fixture", {}, []
        )
        mcp = SimpleNamespace(
            specs={"fixture": object()},
            snapshot=lambda: ([definition], {"mcp__fixture__write": object()}),
            call=Mock(),
            run_connect=Mock(),
        )
        client, _ = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "child", name="task", arguments='{"prompt":"Read fixture"}'
                        )
                    ]
                ),
                reply(
                    calls=[call("external", name="mcp__fixture__write", arguments="{}")]
                ),
                reply("done"),
                reply("done"),
            ]
        )
        homebrew_harness.runtime.loop.agent_loop(
            client,
            "fixture",
            [{"role": "user", "content": "Delegate fixture"}],
            self.root,
            capabilities=Capabilities({"subagents", "mcp"}),
            tool_allowlist=frozenset({"task", "read_file"}),
            mcp_runtime=mcp,
        )
        child_tools = client.chat.completions.create.call_args_list[1].kwargs["tools"]
        self.assertNotIn(
            "mcp__fixture__write", [t["function"]["name"] for t in child_tools]
        )
        mcp.call.assert_not_called()

    def test_standalone_partial_result_stops_before_next_model_round(self):
        responses = iter(
            [
                reply(
                    calls=[
                        call(
                            "child", name="task", arguments='{"prompt":"Write fixture"}'
                        )
                    ]
                ),
                reply(
                    calls=[
                        call(
                            "write",
                            name="write_file",
                            arguments='{"path":"effect.txt","content":"fixture"}',
                        )
                    ]
                ),
                RuntimeError("fixture child request failure"),
                reply(
                    calls=[
                        call(
                            "later",
                            name="write_file",
                            arguments='{"path":"wrong.txt","content":"wrong"}',
                        )
                    ]
                ),
            ]
        )
        client, _ = fake_client([])

        def create(**kwargs):
            response = next(responses)
            if isinstance(response, Exception):
                raise response
            return response

        client.chat.completions.create.side_effect = create
        messages = [{"role": "user", "content": "Delegate fixture"}]
        with self.assertRaises(RecoveryRequired):
            homebrew_harness.runtime.loop.agent_loop(
                client,
                "fixture",
                messages,
                self.root,
                capabilities=Capabilities({"subagents"}),
            )
        self.assertEqual(client.chat.completions.create.call_count, 3)
        self.assertTrue((self.root / "effect.txt").exists())
        self.assertFalse((self.root / "wrong.txt").exists())
        homebrew_harness.context.compaction.ContextCompactor.groups(messages)
        self.assertEqual(json.loads(messages[-1]["content"])["outcome"], "partial")

    def test_successful_child_json_is_data_not_execution_status(self):
        client, _ = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "child",
                            name="task",
                            arguments='{"prompt":"Return the supplied JSON"}',
                        )
                    ]
                ),
                reply('{"outcome":"unknown"}'),
                reply("done"),
            ]
        )
        messages = [{"role": "user", "content": "Delegate a JSON answer"}]
        self.assertEqual(
            homebrew_harness.runtime.loop.agent_loop(
                client,
                "fixture",
                messages,
                self.root,
                capabilities=Capabilities({"subagents"}),
            ),
            "done",
        )
        output = json.loads(
            next(m["content"] for m in messages if m.get("tool_call_id") == "child")
        )
        self.assertEqual(output["outcome"], "completed")
        self.assertEqual(output["result"], '{"outcome":"unknown"}')

    def test_event_failure_after_effect_is_not_replayed_when_host_restarts(self):
        first, client, _ = self.session()
        first.run_user("/usage")
        first.workspace.bus.send("peer", "lead", "Apply the fixture", "result")
        responses = iter(
            [
                reply(
                    calls=[
                        call(
                            "write",
                            name="write_file",
                            arguments='{"path":"effect.txt","content":"once"}',
                        )
                    ]
                ),
                RuntimeError("fixture after effect"),
            ]
        )

        def create(**kwargs):
            value = next(responses)
            if isinstance(value, Exception):
                raise value
            return value

        client.chat.completions.create.side_effect = create
        self.assertTrue(first.process_once())
        sid = first.session_id
        self.assertFalse(first.workspace.bus.peek("lead"))
        first.close()
        self.assertTrue(first.wait_closed(3))
        restored, second, _ = self.session(resume_session=sid)
        restored.run_user("/recovery")
        self.assertFalse(restored.process_once())
        second.chat.completions.create.assert_not_called()
        self.assertEqual((self.root / "effect.txt").read_text(), "once")

    def test_journal_io_failure_cannot_publish_cache_even_when_script_catches_it(self):
        for operation in ("write", "flush", "fsync"):
            with self.subTest(operation=operation):
                calls = []
                runner = SimpleNamespace(
                    identity="fixture",
                    run=lambda *a: (calls.append(1), RunnerOutput("value"))[1],
                )

                async def script(ctx, args):
                    try:
                        await ctx.agent("same fixture")
                    except WorkflowError:
                        pass
                    return await ctx.agent("same fixture")

                runtime = WorkflowRuntime(
                    self.root,
                    runner,
                    WorkflowRegistry(
                        [WorkflowSpec("fixture", "fixture", script, {"type": "object"})]
                    ),
                )
                original = WorkflowJournal.__init__

                class Handle:
                    def __init__(self, value):
                        self.value = value

                    def write(self, text):
                        if operation == "write":
                            raise OSError("fixture write")
                        return self.value.write(text)

                    def flush(self):
                        if operation == "flush":
                            raise OSError("fixture flush")
                        return self.value.flush()

                    def __getattr__(self, name):
                        return getattr(self.value, name)

                def init(journal, *args, **kwargs):
                    original(journal, *args, **kwargs)
                    journal.handle = Handle(journal.handle)

                original_record = WorkflowJournal.record

                def record(journal, row):
                    if operation == "fsync":
                        with patch(
                            "homebrew_harness.orchestration.workflow_runtime.os.fsync",
                            side_effect=OSError("fixture fsync"),
                        ):
                            return original_record(journal, row)
                    return original_record(journal, row)

                with (
                    patch.object(WorkflowJournal, "__init__", init),
                    patch.object(WorkflowJournal, "record", record),
                ):
                    result = runtime.run("fixture", {})
                self.assertEqual(result["task"]["status"], "failed")
                self.assertEqual(result["task"]["durable_steps"], 0)
                self.assertTrue(result["task"]["inspection_required"])
                self.assertEqual(calls, [1])
                with self.assertRaisesRegex(
                    WorkflowError, "Automatic resume is refused"
                ):
                    runtime.run("fixture", resume_from_run_id=result["task"]["run_id"])
                runtime.close()

    def test_closed_uninitialized_host_cannot_reopen_from_event_or_workspace(self):
        session, client, _ = self.session()
        session.close()
        self.assertTrue(session.wait_closed(3))
        self.assertFalse(session.process_once())
        self.assertFalse(session._runtime_started)
        self.assertFalse(session.workspace.leases.held("session"))
        with self.assertRaisesRegex(ValueError, "closed"):
            session.workspace.acquire_session()
        client.chat.completions.create.assert_not_called()
        self.assertIsNone(session.runtime_store.get("session_meta", session.session_id))

    def test_close_waits_for_initialization_and_its_compensation(self):
        session, _, _ = self.session()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = session.runtime_store.load_session
        errors = []

        def load(*args):
            entered.set()
            release.wait(3)
            return original(*args)

        def run():
            try:
                session.run_user("/usage")
            except Exception as error:
                errors.append(type(error).__name__)

        with patch.object(session.runtime_store, "load_session", load):
            thread = threading.Thread(target=run)
            thread.start()
            self.assertTrue(entered.wait(1))
            session.close()
            self.assertFalse(session.wait_closed(0.01))
            self.assertTrue(session.workspace.leases.held("session"))
            release.set()
            thread.join(2)
            self.assertTrue(session.wait_closed(3))
        self.assertFalse(thread.is_alive())
        self.assertTrue(errors)
        with ExecutionLeases(self.root).use("session"):
            pass

    def test_final_lifecycle_write_retains_lease_until_transaction_finishes(self):
        first, _, _ = self.session()
        first.run_user("/usage")
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = first.runtime_store.finish_session

        def finish(*args, **kwargs):
            entered.set()
            release.wait(3)
            return original(*args, **kwargs)

        with patch.object(first.runtime_store, "finish_session", finish):
            first.close()
            self.assertTrue(entered.wait(1))
            self.assertFalse(first.wait_closed(0))
            other = ExecutionLeases(self.root)
            with self.assertRaises(ValueError):
                other.acquire("session")
            other.close()
            release.set()
            self.assertTrue(first.wait_closed(3))
        self.assertEqual(first.shutdown_status()["state"], "closed")
        self.assertTrue(
            first.runtime_store.get("session_meta", first.session_id)["close_complete"]
        )
        with ExecutionLeases(self.root).use("session"):
            pass

    def test_accepted_python_background_cannot_be_inspected_until_worker_finishes(self):
        entered, release = threading.Event(), threading.Event()
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
                            "bg",
                            name="bash",
                            arguments='{"command":"fixture","run_in_background":true}',
                        )
                    ]
                )
            ],
            background_manager=manager,
        )
        original = threading.Thread.start

        def start(thread):
            original(thread)
            if thread.name.startswith("background-"):
                raise RuntimeError("fixture accepted interruption")

        with (
            patch.object(threading.Thread, "start", start),
            patch(
                "homebrew_harness.security.permissions.ask_user",
                return_value=PermissionDecision("allow", "fixture"),
            ),
        ):
            with self.assertRaises(RecoveryRequired):
                session.run_user("Run fixture")
        self.assertTrue(entered.wait(1))
        row = session.recovery_operations()[0]
        self.assertTrue(row["active"])
        with self.assertRaisesRegex(RecoveryRequired, "still executing"):
            session.run_user(
                "/recovery inspect " + row["operation_id"] + " " + str(row["revision"])
            )
        release.set()
        self.assertTrue(manager.wait_closed(2))
        self.assertFalse(session.recovery_operations()[0]["active"])
        session.run_user(
            "/recovery inspect " + row["operation_id"] + " " + str(row["revision"])
        )
        self.assertFalse(session.recovery_operations())

    def test_nested_version_changes_invalidate_only_used_child_steps(self):
        calls = []
        runner = SimpleNamespace(
            identity="fixture",
            run=lambda *args: (
                calls.append(1),
                RunnerOutput("value-" + str(len(calls))),
            )[1],
        )

        async def child(ctx, args):
            return await ctx.agent("same input")

        async def parent(ctx, args):
            return await ctx.workflow("child", {})

        async def unused(ctx, args):
            return "unused"

        def runtime(child_version, unused_version):
            return WorkflowRuntime(
                self.root,
                runner,
                WorkflowRegistry(
                    [
                        WorkflowSpec(
                            "parent", "fixture", parent, {"type": "object"}, version="1"
                        ),
                        WorkflowSpec(
                            "child",
                            "fixture",
                            child,
                            {"type": "object"},
                            version=child_version,
                        ),
                        WorkflowSpec(
                            "unused",
                            "fixture",
                            unused,
                            {"type": "object"},
                            version=unused_version,
                        ),
                    ]
                ),
            )

        first = runtime("1", "1")
        out = first.run("parent", {})
        same = runtime("1", "2")
        cached = same.run("parent", resume_from_run_id=out["task"]["run_id"])
        self.assertEqual(cached["result"], "value-1")
        self.assertEqual(calls, [1])
        changed = runtime("2", "2")
        updated = changed.run("parent", resume_from_run_id=out["task"]["run_id"])
        self.assertEqual(updated["result"], "value-2")
        self.assertEqual(calls, [1, 1])
        for item in (first, same, changed):
            item.close()

    def test_waiting_approval_blocks_goal_and_exit_from_the_same_state(self):
        session, _, _ = self.session(capabilities=Capabilities({"teams"}))
        session.run_user("/usage")
        member = TeammateRuntime(session.team, "peer", "reader", "fixture", True)
        task = session.team.store.create("Await approval")
        member.allowed_tasks = frozenset({task.id})
        member.bind(member.store.claim(task.id))
        member.status = "waiting_approval"
        member.gate = "pending"
        session.team.members["peer"] = member
        (self.root / "done.txt").write_text("ready")
        session.goal.set_goal(
            {
                "condition": "fixture",
                "judge": False,
                "predicates": [
                    {"id": "file", "type": "file_exists", "path": "done.txt"}
                ],
            }
        )
        pending = session._goal_pending_work()
        self.assertIn("team", pending)
        self.assertIn("team", session.pending_work())
        decision = session.goal.evaluate_after_turn("fixture", pending_work=pending)
        self.assertEqual(decision.action, "defer")
        self.assertEqual(session.goal.status()["status"], "waiting")

    def test_fixed_floor_and_model_request_use_the_same_effective_tool_pool(self):
        caps = Capabilities.full(memory_mode="off")
        first, _, _ = self.session(capabilities=caps)
        pool = first.tool_pool()
        floor = first.compactor.estimate_chars(
            [first.messages[0], {"role": "user", "content": ""}], pool.tools
        )
        self.assertLess(
            floor,
            first.compactor.estimate_chars(
                [first.messages[0], {"role": "user", "content": ""}],
                caps.filter(homebrew_harness.tools.catalog.TOOLS),
            ),
        )
        first.intent.update("hi")
        homebrew_harness.runtime.loop.set_runtime_context(
            first.messages, self.root, team_manager=first.workspace
        )
        first.messages[:] = first.compactor.pin_intent(first.messages, "hi")
        minimum = first.compactor.validate_fixed_budget(
            first.messages, "hi", tools=pool.tools
        )
        first.close()
        self.assertTrue(first.wait_closed(3))
        session, client, _ = self.session(
            [reply("done")], capabilities=caps, context_chars=minimum + 64
        )
        self.assertEqual(session.run_user("hi"), "done")
        self.assertEqual(
            session.tool_pool().tools,
            client.chat.completions.create.call_args.kwargs["tools"],
        )

    def test_memory_uses_actual_tool_capacity_and_skips_unregistered_tools(self):
        memory = SimpleNamespace(
            mode="recall", recall=Mock(return_value=""), finish_turn=Mock()
        )
        session, client, _ = self.session(
            [reply("done")],
            capabilities=Capabilities({"subagents"}),
            memory_runtime=memory,
        )
        pool = session.tool_pool()
        original = session.compactor.estimate_chars
        tools_seen = []

        def estimate(messages, tools=None):
            if tools is not None:
                tools_seen.append(tools)
            return original(messages, tools)

        with patch.object(session.compactor, "estimate_chars", estimate):
            session.run_user("hi")
        self.assertEqual(tools_seen[0], pool.tools)
        self.assertNotIn("workflow", pool.names)
        self.assertNotIn("run_tests", pool.names)
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_text_only_child_does_not_send_empty_tools_array(self):
        client, _ = fake_client(
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
                reply("explanation"),
                reply("done"),
            ]
        )
        messages = [{"role": "user", "content": "Delegate explanation"}]
        homebrew_harness.runtime.loop.agent_loop(
            client,
            "fixture",
            messages,
            self.root,
            capabilities=Capabilities({"subagents"}),
            tool_allowlist=frozenset({"task"}),
        )
        self.assertNotIn(
            "tools", client.chat.completions.create.call_args_list[1].kwargs
        )
        self.assertEqual(
            json.loads(
                next(m["content"] for m in messages if m.get("tool_call_id") == "child")
            )["result"],
            "explanation",
        )

    def test_approved_member_plan_endpoint_survives_parent_scope_without_write_expansion(
        self,
    ):
        session, _, _ = self.session(
            [
                reply(
                    calls=[
                        call(
                            "plan",
                            name="submit_plan",
                            arguments='{"plan":"Read fixture"}',
                        )
                    ]
                )
            ],
            capabilities=Capabilities({"teams"}),
        )
        session.run_user("/usage")
        task = session.team.store.create("Fixture")
        args = dict(
            name="peer",
            role="reader",
            prompt="Submit a plan",
            task_id=task.id,
            require_plan=True,
        )
        pool = homebrew_harness.tools.tool_pool.ToolPool(
            [
                homebrew_harness.tools.catalog.define_tool(name, "fixture", {}, [])
                for name in ("spawn_teammate", "read_file")
            ],
            {},
            {},
        )
        session.team.authorize_spawn(args)
        with pool.scope():
            session.team.spawn(**args)
        member = session.team.members["peer"]
        end = time.monotonic() + 2
        while member.status != "waiting_approval" and time.monotonic() < end:
            threading.Event().wait(0.005)
        self.assertEqual(member.status, "waiting_approval")
        tools = session.client._client.chat.completions.create.call_args.kwargs["tools"]
        self.assertIn("submit_plan", [t["function"]["name"] for t in tools])
        self.assertNotIn("write_file", [t["function"]["name"] for t in tools])

    def test_close_finishes_after_the_last_user_checkpoint(self):
        session, _, _ = self.session([reply("done")])
        session.run_user("/usage")
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = session._save_checkpoint

        def save(phase="idle", **kwargs):
            if phase == "idle" and threading.current_thread().name == "fixture-user":
                entered.set()
                release.wait(3)
            return original(phase, **kwargs)

        with patch.object(session, "_save_checkpoint", save):
            thread = threading.Thread(
                target=lambda: session.run_user("hi"), name="fixture-user"
            )
            thread.start()
            self.assertTrue(entered.wait(1))
            session.close()
            self.assertFalse(session.wait_closed(0.01))
            self.assertTrue(session.workspace.leases.held("session"))
            release.set()
            thread.join(2)
            self.assertTrue(session.wait_closed(3))
        self.assertEqual(
            session.runtime_store.get("checkpoints", session.checkpoint_key)["phase"],
            "closed",
        )
        self.assertTrue(
            session.runtime_store.get("session_meta", session.session_id)[
                "close_complete"
            ]
        )

    def test_observer_thread_does_not_keep_a_completed_tool_operation_active(self):
        from homebrew_harness.storage.durable_runtime import (
            CheckpointWriter,
            checkpoint_scope,
            operation_active,
        )
        from homebrew_harness.execution.model_gateway import UsageLedger

        session, _, _ = self.session()
        session.run_user("/usage")
        writer = CheckpointWriter(
            session.runtime_store, session.checkpoint_key, lambda: session._snapshot()
        )
        ledger = UsageLedger(self.root, enabled=True)
        self.addCleanup(lambda: ledger.close(2))
        tool = (
            reply(
                calls=[
                    call("fixture", name="read_file", arguments='{"path":"note.txt"}')
                ]
            )
            .choices[0]
            .message.tool_calls[0]
        )
        with checkpoint_scope(writer):
            identity = writer.tool_started(fixture_invocation(tool), self.root, track=True)
            ledger.append({"event": "fixture"})
            self.assertTrue(ledger.flush(1))
            writer.end_operation(identity)
        self.assertFalse(operation_active(self.root, identity))
        self.assertFalse(ledger.workers.wait(0))
