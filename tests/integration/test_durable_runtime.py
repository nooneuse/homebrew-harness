from tests.support.paths import PROJECT_ROOT

"""Crash recovery, transactional acknowledgement and bounded execution chains."""
from homebrew_harness.runtime.capabilities import Capabilities
import asyncio
import copy
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch
from datetime import datetime

from tests.support.tools import execute_fixture_tool
import homebrew_harness.context.compaction
import homebrew_harness.runtime.errors
import homebrew_harness.tools.shell
from homebrew_harness.execution.background import BackgroundManager, ShellRunner
from homebrew_harness.storage.durable_runtime import (
    DurableStore,
    RecoveryRequired,
    repair_history,
)
from homebrew_harness.execution.execution_control import acquired
from homebrew_harness.runtime.session import HarnessSession
from homebrew_harness.storage.message_bus import MessageBus
from homebrew_harness.execution.model_gateway import (
    ModelGateway,
    GatewayLimits,
    Deadline,
    ModelDeadlineExceeded,
    ModelCancelled,
)
from homebrew_harness.tools.mcp.runtime import (
    MCPRuntime,
    ServerSpec,
    ToolPolicy,
    MCPError,
)
from homebrew_harness.orchestration.cron_scheduler import CronScheduler
from homebrew_harness.orchestration.workflow_runtime import (
    WorkflowRuntime,
    WorkflowRegistry,
    WorkflowSpec,
    RunnerOutput,
)
from tests.support.clients import fake_client, call, reply


class DurableTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        for capture in (redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            capture.__enter__()
            self.addCleanup(capture.__exit__, None, None, None)

    def session(self, responses=(), **kwargs):
        client, requests = fake_client(responses)
        session = HarnessSession(
            client,
            "fixture",
            self.root,
            shutdown_timeout=0.3,
            retry_delay=0,
            **kwargs,
            capabilities=Capabilities.full(memory_mode="off"),
        )
        self.addCleanup(lambda: session.wait_closed(5))
        self.addCleanup(session.close)
        return session, client, requests

    def child(self, script, *args):
        return subprocess.run(
            [sys.executable, "-c", script, str(self.root), *args],
            cwd=PROJECT_ROOT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            text=True,
            timeout=15,
        )

    def test_claim_survives_actual_process_exit_and_same_id_recovers(self):
        bus = MessageBus(self.root)
        mid = bus.send("worker", "lead", "important")
        child = self.child(
            "from homebrew_harness.storage.message_bus import MessageBus; from pathlib import Path; import sys,os; b=MessageBus(Path(sys.argv[1])); assert b.claim('lead')[0]['id']==sys.argv[2]; os._exit(77)",
            mid,
        )
        self.assertEqual(child.returncode, 77)
        recovered = MessageBus(self.root)
        self.assertEqual(recovered.claim("lead"), [])
        recovered.recover("lead")
        messages = recovered.claim("lead")
        self.assertEqual([m["id"] for m in messages], [mid])
        recovered.acknowledge("lead", messages)
        self.assertFalse(MessageBus(self.root).peek("lead"))

    def test_checkpoint_and_ack_are_one_atomic_commit(self):
        bus = MessageBus(self.root)
        bus.send("worker", "lead", "event")
        records = bus.claim("lead")
        store = bus.store
        with patch.object(
            store.backend(), "_save_doc", side_effect=RuntimeError("injected")
        ):
            with self.assertRaises(RuntimeError):
                store.checkpoint(
                    "lead:fixture",
                    {"messages": []},
                    ack=[("lead", bus.consumer, [m["id"] for m in records])],
                )
        state = store.read()
        self.assertNotIn("lead:fixture", state["checkpoints"])
        self.assertEqual(state["inbox"]["lead"][records[0]["id"]]["state"], "inflight")
        store.checkpoint(
            "lead:fixture",
            {"messages": []},
            ack=[("lead", bus.consumer, [m["id"] for m in records])],
        )
        self.assertFalse(store.read()["inbox"]["lead"])
        self.assertIn("lead:fixture", store.read()["checkpoints"])

    def test_wrong_consumer_cannot_ack_another_claim(self):
        bus = MessageBus(self.root)
        bus.send("worker", "lead", "event")
        records = bus.claim("lead")
        MessageBus(self.root).acknowledge("lead", records)
        self.assertTrue(bus.peek("lead"))
        bus.acknowledge("lead", records)
        self.assertFalse(bus.peek("lead"))

    def test_concurrent_process_senders_do_not_lose_records(self):
        script = "from homebrew_harness.storage.message_bus import MessageBus; from pathlib import Path; import sys; b=MessageBus(Path(sys.argv[1])); [b.send('worker','lead',sys.argv[2]+str(i)) for i in range(8)]"
        children = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(self.root), str(i)],
                cwd=PROJECT_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for i in range(3)
        ]
        for child in children:
            child.communicate(timeout=15)
            self.assertEqual(child.returncode, 0)
        self.assertEqual(len(MessageBus(self.root).read_inbox("lead")), 24)

    def test_runtime_symlink_and_corruption_fail_without_overwrite(self):
        store = DurableStore(self.root)
        store.transaction(lambda s: None)
        outside = self.root / "untouched"
        outside.write_text("original")
        store.path.unlink()
        store.path.symlink_to(outside)
        with self.assertRaises((OSError, ValueError)):
            store.transaction(lambda s: None)
        self.assertEqual(outside.read_text(), "original")
        store.path.unlink()
        store.path.write_text("broken")
        with self.assertRaises(RecoveryRequired):
            store.read()
        self.assertEqual(store.path.read_text(), "broken")

    def test_original_intent_todo_and_history_resume_without_a_model_call(self):
        first, _, _ = self.session(
            [
                reply(
                    calls=[
                        call(
                            "todo",
                            name="todo_write",
                            arguments=json.dumps(
                                {
                                    "todos": [
                                        {"content": "verify", "status": "in_progress"}
                                    ]
                                }
                            ),
                        )
                    ]
                ),
                reply("partial"),
            ]
        )
        first.run_user("Preserve API compatibility and check every error")
        first.run_user("/usage")
        sid = first.session_id
        first.close()
        self.assertTrue(first.wait_closed(3))
        resumed, client, _ = self.session(resume_session=sid)
        resumed.run_user("/usage")
        self.assertEqual(
            resumed.intent.original, "Preserve API compatibility and check every error"
        )
        self.assertEqual(resumed.todo.items[0].content, "verify")
        self.assertEqual(resumed.todo.items[0].status, "in_progress")
        self.assertTrue(any(m.get("content") == "partial" for m in resumed.messages))
        self.assertEqual(resumed.gateway.summary()["calls"], 2)
        client.chat.completions.create.assert_not_called()

    def crash_tool_child(self, *, known):
        script = """
import os
os.environ['HARNESS_PROFILE']='full'
os.environ['HARNESS_MEMORY_MODE']='off'

import sys,os,json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import homebrew_harness.context.compaction
import homebrew_harness.runtime.errors
import homebrew_harness.tools.shell
from homebrew_harness.runtime.session import HarnessSession
from homebrew_harness.storage.durable_runtime import CheckpointWriter
call=SimpleNamespace(type='function',id='write-once',function=SimpleNamespace(name='write_file',arguments='{"path":"effect.txt","content":"one"}'))
call.model_dump=lambda **kw:{'type':'function','id':call.id,'function':{'name':call.function.name,'arguments':call.function.arguments}}
response=SimpleNamespace(choices=[SimpleNamespace(finish_reason='tool_calls',message=SimpleNamespace(content=None,tool_calls=[call]))],usage=None)
client=SimpleNamespace(_harness_model_identity='test-client',chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw:response)))
original=CheckpointWriter.tool_finished
def crash(self,op,output):
    if sys.argv[2]=='known': original(self,op,output)
    os._exit(77)
with patch.object(CheckpointWriter,'tool_finished',crash):
    HarnessSession(client,'fixture',Path(sys.argv[1])).run_user('Write once; preserve original requirements')
"""
        return self.child(script, "known" if known else "unknown")

    def test_real_crash_after_side_effect_blocks_replay_and_repairs_tool_batch(self):
        self.assertEqual(self.crash_tool_child(known=False).returncode, 77)
        self.assertEqual((self.root / "effect.txt").read_text(), "one")
        resumed, client, _ = self.session(
            [reply("Inspected existing effect")], resume_session="latest"
        )
        status = json.loads(resumed.run_user("/recovery"))
        self.assertIn("write-once", status["blocked"])
        with self.assertRaises(RecoveryRequired):
            resumed.run_user("Continue")
        client.chat.completions.create.assert_not_called()
        homebrew_harness.context.compaction.ContextCompactor.groups(resumed.messages)
        self.assertTrue(
            any(
                "outcome unknown" in (m.get("content") or "")
                for m in resumed.messages
                if m["role"] == "tool"
            )
        )
        for row in status["operations"]:
            resumed.run_user(
                "/recovery inspect " + row["operation_id"] + " " + str(row["revision"])
            )
        resumed.run_user("/recovery continue")
        self.assertEqual(
            resumed.run_user("Continue after checking the file"),
            "Inspected existing effect",
        )
        self.assertEqual((self.root / "effect.txt").read_text(), "one")
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_real_crash_after_logged_result_recovers_known_output(self):
        self.assertEqual(self.crash_tool_child(known=True).returncode, 77)
        resumed, _, _ = self.session(resume_session="latest")
        resumed.run_user("/recovery")
        tool = [m for m in resumed.messages if m["role"] == "tool"][0]
        self.assertIn("written", tool["content"])
        self.assertNotIn("outcome unknown", tool["content"])
        homebrew_harness.context.compaction.ContextCompactor.groups(resumed.messages)

    def test_closed_pending_inbox_restores_once(self):
        first, _, _ = self.session([])
        first.team.bus.send("worker", "lead", "event")
        first.process_once()
        first.close()
        self.assertTrue(first.wait_closed(3))
        resumed, client, _ = self.session(
            [reply("handled")], resume_session=first.session_id
        )
        self.assertTrue(resumed.process_once())
        self.assertFalse(resumed.process_once())
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertFalse(resumed.team.bus.peek("lead"))

    def test_background_crash_state_is_unknown_and_not_relaunched(self):
        store = DurableStore(self.root)
        store.transaction(
            lambda s: s["background"].__setitem__(
                "bg_01234567",
                {
                    "bg_id": "bg_01234567",
                    "command": "fixture",
                    "status": "running",
                    "result": None,
                    "cwd": str(self.root),
                    "assignment": None,
                    "instance": "old",
                    "ready": False,
                },
            )
        )
        launched = []
        manager = BackgroundManager(self.root, lambda *a, **kw: launched.append(a))
        self.addCleanup(manager.close)
        manager.recover()
        self.assertEqual(
            json.loads(manager.check("bg_01234567", workdir=self.root))["status"],
            "unknown",
        )
        self.assertEqual(launched, [])
        self.assertEqual(manager.collect()[0]["bg_id"], "bg_01234567")

    def test_cron_receipt_survives_and_requires_inspection_before_new_trigger(self):
        scheduler = CronScheduler(self.root)
        scheduler.schedule("* * * * *", "work", recurring=False)
        scheduler.poll_due(datetime(2026, 11, 4, 12))
        job = scheduler.take_pending()
        scheduler.received(job)
        restored = CronScheduler(self.root)
        self.assertFalse(restored.has_pending())
        self.assertTrue(restored.unresolved())
        restored.reconcile(inspect_unknown=True)
        self.assertEqual(restored.list(), [])
        self.assertEqual(restored.unresolved(), [])

    def test_cron_executed_record_repairs_crash_before_schedule_ack(self):
        scheduler = CronScheduler(self.root)
        scheduler.schedule("* * * * *", "work", recurring=False)
        scheduler.poll_due(datetime(2026, 11, 4, 12))
        job = scheduler.take_pending()
        with patch.object(scheduler, "acknowledge", side_effect=OSError("injected")):
            with self.assertRaises(OSError):
                scheduler.executed(job)
        recovered = CronScheduler(self.root)
        recovered.reconcile()
        self.assertEqual(recovered.list(), [])

    def test_usage_is_reconstructed_after_multiple_resumes(self):
        first, _, _ = self.session([reply("answer")])
        first.run_user("first")
        sid = first.session_id
        first.close()
        self.assertTrue(first.wait_closed(3))
        resumed, client, _ = self.session([reply("second")], resume_session=sid)
        resumed.run_user("/usage")
        self.assertEqual(resumed.gateway.summary()["calls"], 1)
        resumed.run_user("continue")
        resumed.close()
        self.assertTrue(resumed.wait_closed(3))
        again, _, _ = self.session(resume_session=sid)
        again.run_user("/usage")
        self.assertEqual(again.gateway.summary()["calls"], 2)
        self.assertEqual(again.gateway.summary()["unknown_usage_calls"], 2)

    def test_persistence_still_works_after_cancelled_model_scope(self):
        c, _ = fake_client([])
        gateway = ModelGateway(c)
        stop = threading.Event()
        stop.set()
        with gateway.scope(cancel_event=stop, deadline=Deadline.after(0)):
            with __import__(
                "homebrew_harness.execution.execution_control", fromlist=["*"]
            ).cleanup_scope():
                DurableStore(self.root).transaction(
                    lambda s: s.__setitem__("fixture", "retained")
                )
        self.assertEqual(DurableStore(self.root).read()["fixture"], "retained")

    def test_unknown_shell_result_blocks_later_tool_in_same_batch(self):
        session, client, _ = self.session(
            [
                reply(
                    calls=[
                        call("slow", name="bash", arguments='{"command":"sleep 2"}'),
                        call(
                            "later",
                            name="write_file",
                            arguments='{"path":"later.txt","content":"bad"}',
                        ),
                    ]
                )
            ]
        )
        with patch(
            "homebrew_harness.security.permissions.check_rules",
            return_value=__import__(
                "homebrew_harness.security.permissions", fromlist=["*"]
            ).PermissionDecision("allow", "fixture"),
        ):
            with session.gateway.scope(deadline=Deadline.after(0.12)):
                with self.assertRaises(
                    (
                        RecoveryRequired,
                        ModelDeadlineExceeded,
                        homebrew_harness.runtime.errors.AgentError,
                    )
                ):
                    session.run_user("Run a bounded command")
        self.assertFalse((self.root / "later.txt").exists())
        homebrew_harness.context.compaction.ContextCompactor.groups(session.messages)

    def test_actual_crash_inside_model_transport_preserves_uncertain_billing(self):
        script = """
import os
os.environ['HARNESS_PROFILE']='full'
os.environ['HARNESS_MEMORY_MODE']='off'

import os,sys
from pathlib import Path
from types import SimpleNamespace
from homebrew_harness.runtime.session import HarnessSession
import homebrew_harness.context.compaction
import homebrew_harness.runtime.errors
import homebrew_harness.tools.shell
def create(**kw):os._exit(77)
client=SimpleNamespace(_harness_model_identity='test-client',chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
HarnessSession(client,'fixture',Path(sys.argv[1])).run_user('Preserve every original constraint')
"""
        self.assertEqual(self.child(script).returncode, 77)
        resumed, client, _ = self.session(resume_session="latest")
        status = json.loads(resumed.run_user("/recovery"))
        self.assertIn("interrupted_turn", status["blocked"])
        self.assertTrue(resumed.gateway.summary()["observation_incomplete"])
        self.assertEqual(resumed.gateway.summary()["known_tokens"], 0)
        client.chat.completions.create.assert_not_called()

    def test_resume_empty_checkpoint_with_no_usage_ledger(self):
        first, _, _ = self.session()
        first.run_user("/usage")
        sid = first.session_id
        first.close()
        self.assertTrue(first.wait_closed(3))
        resumed, client, _ = self.session(resume_session=sid)
        self.assertEqual(json.loads(resumed.run_user("/usage"))["calls"], 0)
        client.chat.completions.create.assert_not_called()

    def test_goal_spec_restores_and_old_test_trust_does_not_cross_sessions(self):
        first, _, _ = self.session()
        first.run_user("/usage")
        specification = {
            "condition": "deliver verified file",
            "judge": False,
            "predicates": [
                {"id": "file", "type": "file_exists", "path": "deliver.txt"}
            ],
        }
        first.goal.set_goal(specification)
        first._save_checkpoint()
        sid = first.session_id
        first.close()
        self.assertTrue(first.wait_closed(3))
        resumed, client, _ = self.session(resume_session=sid)
        resumed.run_user("/recovery")
        self.assertEqual(
            resumed.goal.state["spec"]["condition"], specification["condition"]
        )
        self.assertFalse(
            resumed.goal.hard_verifier.check(resumed.goal.state["spec"])["ok"]
        )
        self.assertEqual(resumed.tests._receipts, {})
        client.chat.completions.create.assert_not_called()

    def test_live_session_lease_prevents_checkpoint_recovery(self):
        first, _, _ = self.session()
        first.run_user("/usage")
        second, client, _ = self.session(resume_session=first.session_id)
        with self.assertRaises(ValueError):
            second.run_user("/recovery")
        client.chat.completions.create.assert_not_called()

    def test_close_deadline_includes_slow_checkpoint_cleanup(self):
        session, _, _ = self.session()
        session.run_user("/usage")
        original = session.runtime_store.checkpoint
        gate = threading.Event()
        self.addCleanup(gate.set)

        def slow(*args, **kwargs):
            gate.wait(2)
            return original(*args, **kwargs)

        with patch.object(session.runtime_store, "checkpoint", slow):
            start = time.monotonic()
            session.close()
            self.assertLess(time.monotonic() - start, 0.6)
            self.assertFalse(session.wait_closed(0.01))
            gate.set()
            self.assertTrue(session.wait_closed(3))

    def test_completed_event_checkpoint_ack_prevents_repeating_model_turn_after_crash(
        self,
    ):
        script = """
import os
os.environ['HARNESS_PROFILE']='full'
os.environ['HARNESS_MEMORY_MODE']='off'

import os,sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from homebrew_harness.runtime.session import HarnessSession
from homebrew_harness.storage.message_bus import MessageBus
from homebrew_harness.storage.durable_runtime import CheckpointWriter
import homebrew_harness.context.compaction
import homebrew_harness.runtime.errors
import homebrew_harness.tools.shell
root=Path(sys.argv[1]);MessageBus(root).send('worker','lead','saved event')
response=SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(content='handled',tool_calls=[]))],usage=None)
client=SimpleNamespace(_harness_model_identity='test-client',chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw:response)))
original=CheckpointWriter.save
def crash(self,phase='running',**kw):
    original(self,phase,**kw)
    if phase=='executed':os._exit(77)
with patch.object(CheckpointWriter,'save',crash):
    HarnessSession(client,'fixture',root).process_once()
"""
        self.assertEqual(self.child(script).returncode, 77)
        resumed, client, _ = self.session(resume_session="latest")
        self.assertFalse(resumed.process_once())
        self.assertFalse(resumed.team.bus.peek("lead"))
        self.assertTrue(any(m.get("content") == "handled" for m in resumed.messages))
        client.chat.completions.create.assert_not_called()

    def test_owned_archive_access_survives_resume(self):
        first, _, _ = self.session()
        first.run_user("/usage")
        reference = first.compactor.store.save("tool-results", "preserved long output")
        first._save_checkpoint()
        sid = first.session_id
        first.close()
        self.assertTrue(first.wait_closed(3))
        resumed, _, _ = self.session(resume_session=sid)
        resumed.run_user("/usage")
        self.assertTrue(resumed.compactor.store.owns(reference))
        block = (
            reply(
                calls=[
                    call(
                        "read",
                        name="read_file",
                        arguments=json.dumps({"path": reference}),
                    )
                ]
            )
            .choices[0]
            .message.tool_calls[0]
        )
        output = execute_fixture_tool(
            block,
            self.root,
            archive_store=resumed.compactor.store,
            restrict_archives=True,
        )
        self.assertIn("preserved long output", output)

    def test_crash_in_cron_context_preserves_original_human_intent(self):
        script = """
import os
os.environ['HARNESS_PROFILE']='full'
os.environ['HARNESS_MEMORY_MODE']='off'

import os,sys
from pathlib import Path
from datetime import datetime
from types import SimpleNamespace
from homebrew_harness.runtime.session import HarnessSession
import homebrew_harness.context.compaction
import homebrew_harness.runtime.errors
import homebrew_harness.tools.shell
response=SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(content='accepted',tool_calls=[]))],usage=None)
method=SimpleNamespace(create=lambda **kw:response)
client=SimpleNamespace(_harness_model_identity='test-client',chat=SimpleNamespace(completions=method))
session=HarnessSession(client,'fixture',Path(sys.argv[1]))
session.run_user('Keep the original API and preserve all user constraints')
session.todo.update([{'content':'Review every changed API','status':'in_progress'}])
session.cron.schedule('* * * * *','Independent scheduled observation',recurring=False)
session.cron.poll_due(datetime(2026,11,4,12))
method.create=lambda **kw:os._exit(77)
session.process_once()
"""
        self.assertEqual(self.child(script).returncode, 77)
        resumed, client, _ = self.session(resume_session="latest")
        resumed.run_user("/recovery")
        self.assertEqual(
            resumed.intent.original,
            "Keep the original API and preserve all user constraints",
        )
        self.assertEqual(resumed.todo.items[0].content, "Review every changed API")
        self.assertEqual(resumed.todo.items[0].status, "in_progress")
        client.chat.completions.create.assert_not_called()


class DeadlineTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.client, _ = fake_client([])
        self.gateway = ModelGateway(self.client)

    def test_lock_wait_obeys_parent_absolute_deadline(self):
        lock = threading.Lock()
        lock.acquire()
        self.addCleanup(lock.release)
        start = time.monotonic()
        with self.gateway.scope(deadline=Deadline.after(0.06)):
            with self.assertRaises(ModelDeadlineExceeded):
                with acquired(lock):
                    pass
        self.assertLess(time.monotonic() - start, 0.3)

    def test_lock_wait_cancellation_propagates(self):
        lock = threading.Lock()
        lock.acquire()
        self.addCleanup(lock.release)
        stop = threading.Event()
        threading.Timer(0.03, stop.set).start()
        with self.gateway.scope(cancel_event=stop):
            with self.assertRaises(ModelCancelled):
                with acquired(lock):
                    pass

    def test_shell_parent_deadline_covers_child_holding_pipe(self):
        runner = ShellRunner()
        self.addCleanup(runner.close)
        start = time.monotonic()
        with self.gateway.scope(deadline=Deadline.after(0.1)):
            with self.assertRaises((subprocess.TimeoutExpired, ModelDeadlineExceeded)):
                runner.run("sleep 5 & exit 0", self.root, {}, 10)
        self.assertLess(time.monotonic() - start, 0.4)
        self.assertTrue(runner.wait_closed(2))

    def test_shell_output_limit_stops_unbounded_writer(self):
        runner = ShellRunner()
        self.addCleanup(runner.close)
        result = runner.run(
            [sys.executable, "-c", 'import os\nwhile True: os.write(1,b"x"*65536)'],
            self.root,
            {},
            5,
            output_bytes=100000,
        )
        self.assertTrue(result.output_limit)
        self.assertLessEqual(len(result.stdout), 100000)
        self.assertTrue(runner.wait_closed(2))

    def test_background_inherits_deadline_and_cancel_scope(self):
        manager = BackgroundManager(self.root, homebrew_harness.tools.shell.run_bash)
        self.addCleanup(manager.close)
        with self.gateway.scope(deadline=Deadline.after(0.1)):
            bid = manager.start("sleep 5")["bg_id"]
        for thread in manager._threads:
            thread.join(2)
        result = json.loads(manager.check(bid, workdir=self.root))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["result"]["outcome"], "unknown")

    def test_workflow_wait_is_bounded_and_late_output_is_discarded(self):
        release = threading.Event()
        self.addCleanup(release.set)

        class Runner:
            identity = "fixture"

            def run(self, *args):
                release.wait(3)
                return RunnerOutput("late")

        async def script(ctx, args):
            return await ctx.agent("one")

        registry = WorkflowRegistry()
        registry.register(
            WorkflowSpec("bounded", "fixture", script, {"type": "object"})
        )
        runtime = WorkflowRuntime(self.root, Runner(), registry=registry)
        self.addCleanup(runtime.close)
        start = time.monotonic()
        with self.gateway.scope(deadline=Deadline.after(0.08)):
            result = runtime.run("bounded", {})
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertEqual(result["task"]["status"], "paused")
        self.assertFalse(runtime.wait_closed(0.01))
        release.set()
        self.assertTrue(runtime.wait_closed(2))
        self.assertNotEqual(result["result"], "late")

    def test_test_runner_cancel_covers_process_that_closed_output_pipes(self):
        from homebrew_harness.execution.test_runner import TestRunner, TestRecipe

        runner = TestRunner(
            self.root,
            [
                TestRecipe(
                    "quiet",
                    (
                        sys.executable,
                        "-c",
                        "import os,time;os.close(1);os.close(2);time.sleep(5)",
                    ),
                    timeout=10,
                )
            ],
        )
        self.addCleanup(runner.close)
        runner.approve("quiet")
        start = time.monotonic()
        with self.gateway.scope(deadline=Deadline.after(0.12)):
            result = runner.run("quiet")
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertEqual(result["status"], "timeout")
        self.assertTrue(runner.wait_closed(2))

    def test_real_mcp_slow_call_obeys_parent_deadline_without_retry(self):
        spec = ServerSpec(
            "fixture",
            "stdio",
            sys.executable,
            (
                str(PROJECT_ROOT / "examples/mcp_demo_server.py"),
                "--transport",
                "stdio",
                "--test-tools",
            ),
            self.root,
            env={"MCP_DEMO_STATE_DIR": str(self.root)},
            tools={"delay": ToolPolicy("allow", True)},
            timeout_seconds=10,
        )
        runtime = MCPRuntime({"fixture": spec}, workdir=self.root)
        self.addCleanup(lambda: runtime.wait_closed(5))
        self.addCleanup(runtime.close)
        runtime.connect("fixture")
        binding = runtime.snapshot()[1]["mcp__fixture__delay"]
        start = time.monotonic()
        with self.gateway.scope(deadline=Deadline.after(0.1)):
            with self.assertRaises(MCPError):
                runtime.call(binding, {"seconds": 5})
        self.assertLess(time.monotonic() - start, 0.4)
        runtime.close(timeout=0.01)
        self.assertTrue(runtime.wait_closed(5))
        records = [
            json.loads(line)
            for line in (self.root / "calls.jsonl").read_text().splitlines()
        ]
        self.assertEqual(sum(r.get("name") == "delay" for r in records), 1)

    def test_stdio_closes_descendants_after_server_parent_has_exited(self):
        parent = self.root / "fixture_parent.py"
        parent.write_text(
            "import subprocess,sys,os\nsubprocess.Popen([sys.executable,'-c',\"import time,pathlib;time.sleep(.5);pathlib.Path('escaped-marker').write_text('bad')\"])\nos._exit(0)\n"
        )
        spec = ServerSpec(
            "fixture",
            "stdio",
            sys.executable,
            (str(parent),),
            self.root,
            connect_timeout_seconds=0.15,
        )
        runtime = MCPRuntime({"fixture": spec}, workdir=self.root)
        self.addCleanup(lambda: runtime.wait_closed(3))
        self.addCleanup(runtime.close)
        with self.assertRaises(MCPError):
            runtime.connect("fixture")
        runtime.close(timeout=0.1)
        self.assertTrue(runtime.wait_closed(3))
        threading.Event().wait(0.6)
        self.assertFalse((self.root / "escaped-marker").exists())

    def test_real_mcp_queued_expired_request_is_not_sent(self):
        spec = ServerSpec(
            "fixture",
            "stdio",
            sys.executable,
            (
                str(PROJECT_ROOT / "examples/mcp_demo_server.py"),
                "--transport",
                "stdio",
                "--test-tools",
            ),
            self.root,
            env={"MCP_DEMO_STATE_DIR": str(self.root)},
            timeout_seconds=10,
        )
        runtime = MCPRuntime({"fixture": spec}, workdir=self.root)
        self.addCleanup(lambda: runtime.wait_closed(5))
        self.addCleanup(runtime.close)
        runtime.connect("fixture")
        binding = runtime.snapshot()[1]["mcp__fixture__delay"]
        errors = []

        def slow():
            try:
                runtime.call(binding, {"seconds": 0.25})
            except Exception as error:
                errors.append(type(error).__name__)

        thread = threading.Thread(target=slow)
        thread.start()
        deadline = time.monotonic() + 2
        while not (self.root / "calls.jsonl").exists() and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        threading.Event().wait(0.03)
        with self.gateway.scope(deadline=Deadline.after(0.04)):
            with self.assertRaises(MCPError):
                runtime.call(binding, {"seconds": 0})
        thread.join(2)
        self.assertEqual(errors, [])
        records = [
            json.loads(line)
            for line in (self.root / "calls.jsonl").read_text().splitlines()
        ]
        self.assertEqual(sum(r.get("name") == "delay" for r in records), 1)


if __name__ == "__main__":
    unittest.main()
