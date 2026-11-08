from tests.support.tools import queued_cli_input

"""Cross-component regressions for second-review findings R13--R23."""

from homebrew_harness.runtime.capabilities import Capabilities
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from contextlib import redirect_stderr, redirect_stdout

from tests.support.tools import execute_fixture_tool
import homebrew_harness.cli
import homebrew_harness.session_cli
import homebrew_harness.configuration
import homebrew_harness.context.compaction
import homebrew_harness.context.todo
import homebrew_harness.runtime.turns
import homebrew_harness.tools.files
from homebrew_harness.execution.background import BackgroundManager
from homebrew_harness.context.compaction import ContextCompactor, CompactionError
from homebrew_harness.storage.durable_runtime import DurableStore, RecoveryRequired
from homebrew_harness.execution.execution_leases import ExecutionLeases
from homebrew_harness.orchestration.goal_loop import GoalController
from homebrew_harness.runtime.session import HarnessSession
from homebrew_harness.context.memory import MemoryRuntime, MemoryRecord, MemoryStore
from homebrew_harness.execution.model_gateway import (
    ModelGateway,
    Deadline,
    ModelDeadlineExceeded,
)
from homebrew_harness.execution.model_profile import profile_fingerprint
from homebrew_harness.security.safe_files import SafeFile
from homebrew_harness.context.skill_loader import SkillLoader
from homebrew_harness.orchestration.task_store import TaskStore
from homebrew_harness.orchestration.teams import TeammateRuntime
from homebrew_harness.execution.test_runner import TestRecipe, TestRunner
from homebrew_harness.tools.tool_outcome import needs_inspection
from homebrew_harness.orchestration.workflow_runtime import (
    ModelWorkflowRunner,
    WorkflowRuntime,
    WorkflowRegistry,
    WorkflowSpec,
    WorkflowError,
)
from tests.support.clients import fake_client, reply, call


class ReviewFixTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for capture in (redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO())):
            capture.__enter__()
            self.addCleanup(capture.__exit__, None, None, None)

    def session(self, responses=(), **kwargs):
        client, requests = fake_client(responses)
        s = HarnessSession(
            client,
            "fixture",
            self.root,
            shutdown_timeout=0.2,
            **kwargs,
            capabilities=Capabilities.full(memory_mode="off"),
        )
        self.addCleanup(lambda: s.wait_closed(5))
        self.addCleanup(s.close)
        return s, client, requests

    def test_checkpoint_failure_always_releases_lead_lock(self):
        s, _, _ = self.session([reply("handled")])
        s.run_user("/usage")
        s.team.bus.send("peer", "lead", "event")
        with patch.object(
            s.runtime_store, "checkpoint", side_effect=OSError("fixture ENOSPC")
        ):
            with self.assertRaises(OSError):
                s.process_once()
        self.assertFalse(s.lock.locked())
        self.assertFalse(s.run_user("/usage") is None)

    def test_unstarted_background_registration_rolls_back_lease_and_record(self):
        s, _, _ = self.session()
        s.run_user("/usage")
        with patch.object(
            s.background, "_persist", side_effect=OSError("fixture ENOSPC")
        ):
            with self.assertRaises(OSError):
                s.background.start("printf fixture")
        self.assertEqual(s.background.snapshot(), [])
        self.assertFalse(s.background._threads)
        other = ExecutionLeases(self.root)
        with other.use("workspace:" + str(self.root)):
            pass
        self.assertEqual(s.runtime_store.read()["background"], {})

    def test_thread_start_failure_removes_durable_running_record(self):
        s, _, _ = self.session()
        s.run_user("/usage")
        with patch(
            "homebrew_harness.execution.background.threading.Thread.start",
            side_effect=RuntimeError("start failure"),
        ):
            with self.assertRaises(RuntimeError):
                s.background.start("printf fixture")
        self.assertFalse(s.runtime_store.read()["background"])
        self.assertFalse(s.background.snapshot())

    def test_final_checkpoint_failure_is_observable_after_resources_close(self):
        s, _, _ = self.session()
        s.run_user("/usage")
        with patch.object(s, "_save_checkpoint", side_effect=OSError("fixture ENOSPC")):
            s.close()
            self.assertTrue(s.wait_closed(3))
        status = s.shutdown_status()
        self.assertEqual(status["state"], "error")
        self.assertTrue(status["resources_closed"])
        self.assertFalse(status["persistence_ok"])
        self.assertTrue(any(e["component"] == "checkpoint" for e in status["errors"]))

    def test_background_close_persist_failure_still_stops_process(self):
        s, _, _ = self.session()
        s.run_user("/usage")
        bid = s.background.start("sleep 5")["bg_id"]
        with patch.object(
            s.background, "_persist", side_effect=OSError("fixture ENOSPC")
        ):
            s.close()
            self.assertTrue(s.wait_closed(3))
        self.assertFalse(s.background.runner._processes)
        self.assertEqual(s.shutdown_status()["state"], "error")

    def test_repeated_restore_preserves_dormant_member_state(self):
        first, _, _ = self.session()
        first.run_user("/usage")
        member = TeammateRuntime(first.team, "peer", "reader", "original task", False)
        member.todo_manager = homebrew_harness.context.todo.TodoManager()
        member.todo_manager.update([{"content": "verify", "status": "in_progress"}])
        first.team.members["peer"] = member
        first._save_checkpoint()
        sid = first.session_id
        first.close()
        self.assertTrue(first.wait_closed(3))
        for _ in range(3):
            restored, client, _ = self.session(resume_session=sid)
            restored.run_user("/recovery")
            saved = restored.team.recovered_members["peer"]
            self.assertEqual(saved["todo"][0]["content"], "verify")
            self.assertTrue(
                any(m.get("content") == "original task" for m in saved["messages"])
            )
            self.assertEqual(restored.team.members, {})
            client.chat.completions.create.assert_not_called()
            restored.close()
            self.assertTrue(restored.wait_closed(3))

    def test_stale_team_snapshot_cannot_overwrite_newer_member(self):
        newer = {
            "revision": 5,
            "members": {
                "peer": {
                    "revision": 5,
                    "messages": [{"role": "user", "content": "new"}],
                }
            },
            "protocols": ["new"],
        }
        stale = {
            "revision": 4,
            "members": {
                "peer": {
                    "revision": 3,
                    "messages": [{"role": "user", "content": "old"}],
                }
            },
            "protocols": ["old"],
        }
        store = DurableStore(self.root)
        store.team_checkpoint("fixture", newer)
        store.team_checkpoint("fixture", stale)
        merged = store.get("team", "fixture")
        self.assertEqual(
            merged["members"]["peer"]["messages"], [{"role": "user", "content": "new"}]
        )
        self.assertEqual(merged["protocols"], ["new"])

    def test_real_goal_terminal_crash_restores_session_without_reactivation(self):
        script = """
import os
os.environ['HARNESS_PROFILE']='full'
os.environ['HARNESS_MEMORY_MODE']='off'

import os,sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from homebrew_harness.orchestration.goal_loop import GoalController
from homebrew_harness.runtime.session import HarnessSession
import homebrew_harness.cli
import homebrew_harness.session_cli
import homebrew_harness.configuration
import homebrew_harness.context.compaction
import homebrew_harness.context.todo
import homebrew_harness.runtime.turns
import homebrew_harness.tools.files
root=Path(sys.argv[1]);(root/'ready').write_text('yes')
response=SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(content='finished',tool_calls=[]))],usage=None)
client=SimpleNamespace(_harness_model_identity='test-client',chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw:response)))
original=GoalController.accept_candidate
def crash(self,state):
 original(self,state)
 if self.state['status']=='achieved':os._exit(77)
with patch.object(GoalController,'accept_candidate',crash):
 HarnessSession(client,'fixture',root).run_user('/goal spec {"condition":"ready file exists","judge":false,"predicates":[{"id":"file","type":"file_exists","path":"ready"}]}')
"""
        child = subprocess.run(
            [sys.executable, "-c", script, str(self.root)],
            capture_output=True,
            timeout=12,
        )
        self.assertEqual(child.returncode, 77)
        s, c, _ = self.session(resume_session="latest")
        s.run_user("/recovery")
        self.assertEqual(s.goal.state["status"], "achieved")
        self.assertFalse(s.goal.enabled)
        self.assertIsNone(s.goal.lease)
        c.chat.completions.create.assert_not_called()

    def test_slow_teammate_test_does_not_lock_lead_checkpoint(self):
        recipe = TestRecipe(
            "slow",
            (sys.executable, "-c", "import time;time.sleep(1.8)"),
            roles=("teammate",),
            timeout=5,
        )
        runner = TestRunner(self.root, [recipe])
        runner.approve("slow", role="teammate")
        s, c, _ = self.session(test_runner=runner)
        s.run_user("/usage")
        member = TeammateRuntime(s.team, "peer", "tester", "test", False)
        s.team.members["peer"] = member
        task = s.team.store.create("testing")
        member.bind(member.store.claim(task.id))
        block = (
            reply(calls=[call("run", name="run_tests", arguments='{"name":"slow"}')])
            .choices[0]
            .message.tool_calls[0]
        )
        outputs = []

        def worker():
            outputs.append(
                execute_fixture_tool(
                    block,
                    self.root,
                    team_member=member,
                    test_runner=runner,
                    handlers={
                        "run_tests": lambda name, workdir: json.dumps(
                            runner.run(name, workdir=workdir)
                        )
                    },
                )
            )

        member.thread = threading.Thread(target=worker)

        def respond(**kw):
            member.thread.start()
            until = time.monotonic() + 2
            while not runner.active.is_set() and time.monotonic() < until:
                time.sleep(0.005)
            return reply("lead finished")

        c.chat.completions.create.side_effect = respond
        started = time.monotonic()
        self.assertEqual(s.run_user("inspect progress"), "lead finished")
        self.assertLess(time.monotonic() - started, 1.2)
        self.assertTrue(member.thread.is_alive())
        with self.assertRaisesRegex(ValueError, "executing"):
            s.team.request_plan("peer", "change the plan")
        member.thread.join(4)
        self.assertEqual(json.loads(outputs[0])["status"], "passed")

    def test_child_partial_write_failure_blocks_parent_and_records_child(self):
        s, c, _ = self.session()
        items = iter(
            [
                reply(
                    calls=[
                        call(
                            "delegate",
                            name="task",
                            arguments='{"prompt":"write one file"}',
                        ),
                        call(
                            "later",
                            name="write_file",
                            arguments='{"path":"later","content":"bad"}',
                        ),
                    ]
                ),
                reply(
                    calls=[
                        call(
                            "childwrite",
                            name="write_file",
                            arguments='{"path":"effect","content":"done"}',
                        )
                    ]
                ),
                RuntimeError("fixture connection"),
            ]
        )

        def create(**kw):
            response = next(items)
            if isinstance(response, Exception):
                raise response
            return response

        c.chat.completions.create.side_effect = create
        with self.assertRaises(RecoveryRequired):
            s.run_user("delegate")
        self.assertTrue((self.root / "effect").exists())
        self.assertFalse((self.root / "later").exists())
        homebrew_harness.context.compaction.ContextCompactor.groups(s.messages)
        parent = next(m for m in s.messages if m.get("tool_call_id") == "delegate")
        self.assertTrue(needs_inspection(parent["content"]))
        children = [
            v
            for k, v in s.runtime_store.read()["checkpoints"].items()
            if k.startswith("sub:")
        ]
        self.assertEqual(children[0]["phase"], "partial")
        self.assertTrue(any(e["name"] == "write_file" for e in children[0]["effects"]))
        self.assertTrue(json.loads(s.run_user("/recovery"))["blocked"])

    def test_child_failure_before_actions_is_not_reported_as_partial(self):
        c, _ = fake_client([])
        c.chat.completions.create.side_effect = RuntimeError("fixture")
        result = json.loads(
            homebrew_harness.runtime.turns.run_subagent(
                "read", self.root, client=c, model="fixture"
            )
        )
        self.assertEqual(result["outcome"], "not_executed")
        self.assertFalse(result["executed"])

    def test_task_creation_deadline_includes_lock_and_prevents_side_effect(self):
        store = TaskStore(self.root)
        self.addCleanup(store.close)
        ready = threading.Event()
        release = threading.Event()

        def hold():
            with store._thread_lock:
                ready.set()
                release.wait(2)

        thread = threading.Thread(target=hold)
        thread.start()
        ready.wait(1)
        self.addCleanup(release.set)
        c, _ = fake_client([])
        gateway = ModelGateway(c)
        started = time.monotonic()
        try:
            with gateway.scope(deadline=Deadline.after(0.04)):
                with self.assertRaises(ModelDeadlineExceeded):
                    store.create("late")
            self.assertLess(time.monotonic() - started, 0.3)
        finally:
            release.set()
            thread.join(2)
        self.assertEqual(store.list(), [])

    def test_expired_file_write_creates_no_artifact(self):
        c, _ = fake_client([])
        with ModelGateway(c).scope(deadline=Deadline.after(0)):
            with self.assertRaises(ModelDeadlineExceeded):
                homebrew_harness.tools.files.run_write("late", "bad", self.root)
        self.assertFalse((self.root / "late").exists())

    def test_snapshot_has_explicit_size_limit(self):
        (self.root / "large").write_bytes(b"x" * 32)
        with SafeFile(self.root, "large") as target:
            with self.assertRaisesRegex(ValueError, "size limit"):
                target.snapshot(max_bytes=8)

    def test_idle_polls_do_not_publish_runtime_state(self):
        s, c, _ = self.session()
        s.run_user("/usage")
        with patch.object(SafeFile, "publish", wraps=SafeFile.publish) as publish:
            self.assertEqual([s.process_once() for _ in range(10)], [False] * 10)
        publish.assert_not_called()
        c.chat.completions.create.assert_not_called()

    def test_noop_store_transaction_preserves_file_version(self):
        store = DurableStore(self.root)
        store.transaction(lambda state: state.__setitem__("fixture", 1))
        before = store.path.stat().st_mtime_ns
        store.transaction(lambda state: state.__setitem__("fixture", 1))
        self.assertEqual(store.path.stat().st_mtime_ns, before)

    def test_interactive_cli_retains_session_after_recovery_error(self):
        c, _ = fake_client([])
        config = {
            key: "fixture-config"
            for key in homebrew_harness.configuration.CONFIG_VARIABLES
        }
        (self.root / "mcp.json").write_text('{"servers":{}}')
        with (
            patch.dict(os.environ, config),
            patch("homebrew_harness.session_cli.load_dotenv"),
            patch("homebrew_harness.session_cli.OpenAI") as factory,
            patch(
                "builtins.input",
                side_effect=queued_cli_input(
                    ["continue", "/recovery", "quit"], lambda: run.call_count >= 2
                ),
            ) as inp,
            patch.object(
                HarnessSession,
                "run_user",
                side_effect=[RecoveryRequired("inspect first"), "state retained"],
            ) as run,
        ):
            factory.return_value.__enter__.return_value = c
            self.assertEqual(
                homebrew_harness.cli.main(["session"] +
                    ["--profile", "full"]
                    + [
                        "--cwd",
                        str(self.root),
                        "--mcp-config",
                        str(self.root / "mcp.json"),
                        "--memory-mode",
                        "off",
                    ]
                ),
                0,
            )
            self.assertEqual(inp.call_count, 3)
            self.assertEqual(run.call_count, 2)

    def test_memory_parent_replacement_cannot_read_external_record(self):
        outer = self.root / "outer"
        outer.mkdir()
        work = self.root / "workspace"
        work.mkdir()
        record = MemoryRecord(
            "fixture",
            "project",
            "fixture",
            "OUTSIDE",
            "OUTSIDE",
            "2026-11-04T00:00:00+00:00",
        )
        (outer / "fixture.md").write_text(record.document())
        store = MemoryStore(work)
        store.root.mkdir()
        (store.root / "fixture.md").write_text(
            record.document().replace("OUTSIDE", "INSIDE")
        )
        original = store._path
        swapped = False

        def race(name):
            nonlocal swapped
            path = original(name)
            if not swapped:
                swapped = True
                store.root.rename(work / ".memory-old")
                store.root.symlink_to(outer, target_is_directory=True)
            return path

        with patch.object(store, "_path", race):
            try:
                result = store.snapshot()
                bodies = [r.body for r in result.records]
            except (OSError, ValueError):
                bodies = []
        self.assertNotIn("OUTSIDE", bodies)

    def test_skill_internal_symlink_is_skipped_without_hiding_real_source(self):
        root = self.root / "skills"
        (root / "real").mkdir(parents=True)
        (root / "real" / "SKILL.md").write_text(
            "---\nname: real\ndescription: real skill\n---\nbody"
        )
        (root / "alias").symlink_to(root / "real", target_is_directory=True)
        loader = SkillLoader(root)
        self.assertEqual(set(loader.skills), {"real"})

    def test_unsupported_memory_body_cannot_replace_user_evidence(self):
        c, _ = fake_client([])
        runtime = MemoryRuntime(c, "fixture", self.root, mode="auto")
        evidence = "Always write reports in Chinese."
        record = runtime._candidate(
            {
                "name": "language",
                "type": "user",
                "scope": "persistent",
                "description": "English only",
                "body": "Always write reports in English.",
                "evidence": evidence,
            },
            evidence,
        )
        self.assertEqual(record.body, evidence)
        self.assertNotIn("English", record.description)

    def test_host_can_correct_and_delete_memory(self):
        c, _ = fake_client([])
        memory = MemoryRuntime(c, "fixture", self.root, mode="auto")
        old = MemoryRecord(
            "fixture", "project", "fixture", "old", "old", "2026-11-04T00:00:00+00:00"
        )
        memory.store.replace([old], memory.store.snapshot().version)
        memory.correct("fixture", "New exact user statement.")
        self.assertEqual(
            memory.store.snapshot().records[0].body, "New exact user statement."
        )
        memory.delete("fixture")
        self.assertEqual(memory.store.snapshot().records, ())

    def test_greeting_skips_memory_maintenance_model_call(self):
        c, requests = fake_client([reply("hello")])
        memory = MemoryRuntime(c, "fixture", self.root, mode="auto")
        s = HarnessSession(
            c,
            "fixture",
            self.root,
            memory_runtime=memory,
            capabilities=Capabilities.full(memory_mode="auto"),
        )
        try:
            self.assertEqual(s.run_user("hello"), "hello")
        finally:
            s.close()
            s.wait_closed(3)
        self.assertEqual(len(requests), 1)

    def test_capability_profile_separates_cache_without_exposing_endpoint(self):
        a, _ = fake_client([])
        b, _ = fake_client([])
        a.base_url = "fixture-first-endpoint"
        b.base_url = "fixture-second-endpoint"
        first = profile_fingerprint(a, "same", profile="v1")
        self.assertNotEqual(first, profile_fingerprint(b, "same", profile="v1"))
        self.assertNotEqual(first, profile_fingerprint(a, "same", profile="v2"))
        self.assertEqual(first, profile_fingerprint(a, "same", profile="v1"))
        self.assertNotIn(a.base_url, first)
        self.assertNotEqual(
            first,
            profile_fingerprint(
                a, "same", profile="v1", settings={"reasoning_effort": "high"}
            ),
        )

    def test_distinct_unidentified_clients_cannot_resume_each_others_cache(self):
        async def script(ctx, args):
            return await ctx.agent("one word")

        registry = WorkflowRegistry(
            [WorkflowSpec("fixture", "fixture", script, {"type": "object"})]
        )
        a, _ = fake_client([reply("old")], identity=None)
        b, _ = fake_client([reply("new")], identity=None)
        first = WorkflowRuntime(self.root, ModelWorkflowRunner(a, "same"), registry)
        result = first.run("fixture", {})
        first.close()
        second = WorkflowRuntime(self.root, ModelWorkflowRunner(b, "same"), registry)
        with self.assertRaisesRegex(WorkflowError, "runner"):
            second.run("fixture", resume_from_run_id=result["task"]["run_id"])
        second.close()
        b.chat.completions.create.assert_not_called()

    def test_full_chunk_summary_contains_middle_without_silent_omission(self):
        c, requests = fake_client([reply("chunk") for _ in range(8)])
        comp = ContextCompactor(c, "fixture", self.root, context_chars=4000)
        history = [
            {
                "role": "assistant",
                "content": "a" * 1800 + "MIDDLE_CRITICAL_FACT" + "b" * 1800,
            }
        ]
        result = comp.summarize_history(history, "task", "")
        payloads = [json.loads(r[-1]["content"])["conversation"] for r in requests]
        self.assertIn("MIDDLE_CRITICAL_FACT", "".join(payloads))
        self.assertIn("All 2 transcript ranges", result)

    def test_fixed_context_floor_reports_required_budget_before_model(self):
        c, _ = fake_client([])
        with self.assertRaisesRegex(CompactionError, "require at least"):
            HarnessSession(
                c,
                "fixture",
                self.root,
                context_chars=10000,
                capabilities=Capabilities.full(memory_mode="off"),
            )
        c.chat.completions.create.assert_not_called()

    def test_early_relevant_goal_evidence_survives_later_noise(self):
        goal = GoalController(self.root, None, input_chars=3000)
        goal.set_goal("Verify important.txt has valid content")
        try:
            goal.capture(
                "tool",
                {"path": "important.txt", "content": "valid", "name": "read_file"},
            )
            first = goal.state["evidence"][0]["id"]
            for i in range(30):
                goal.capture(
                    "tool", {"path": str(i) + ".txt", "content": "noise " * 80}
                )
            packet = goal.packet("done")
            self.assertEqual(len(goal.state["evidence"]), 31)
            self.assertIn(first, [e["id"] for e in packet["evidence"]])
            self.assertGreater(packet["evidence_coverage"]["omitted"], 0)
        finally:
            goal.close()

    def test_result_notification_already_contains_idle_state(self):
        s, _, _ = self.session()
        s.run_user("/usage")
        member = TeammateRuntime(s.team, "peer", "reader", "task", False)
        s.team.members["peer"] = member
        member.finish_work_turn("verified result")
        records = s.team.consume_lead_inbox()
        self.assertEqual([r["type"] for r in records], ["result"])
        self.assertEqual(records[0]["metadata"]["member_status"], "idle")

    def test_review_workflow_reports_scope_and_possible_saturation(self):
        from homebrew_harness.orchestration.workflows import default_registry, FINDING
        from homebrew_harness.orchestration.workflow_runtime import RunnerOutput

        class Runner:
            identity = "review-fixture"

            def run(self, prompt, schema, label, max_tokens):
                if label.startswith("audit:"):
                    return RunnerOutput(
                        {
                            "findings": [
                                {
                                    "title": "A",
                                    "severity": "high",
                                    "evidence": "line 1",
                                },
                                {"title": "B", "severity": "low", "evidence": "line 2"},
                            ]
                        }
                    )
                return RunnerOutput(
                    {
                        key: {"is_real": True, "reason": "fixture"}
                        for key in schema["required"]
                    }
                )

        runtime = WorkflowRuntime(self.root, Runner(), default_registry())
        try:
            result = runtime.run(
                "review-changes",
                {"changes": "line 1\nline 2", "dimensions": ["correctness"]},
            )["result"]
        finally:
            runtime.close()
        coverage = result["coverage"]
        self.assertEqual(coverage["scope"], "explicit_changes_only")
        self.assertEqual(coverage["possible_saturation"], ["correctness"])
        self.assertFalse(coverage["complete_repository_review"])

    def test_same_explicit_profile_can_reuse_cached_inference(self):
        async def script(ctx, args):
            return await ctx.agent("one word")

        registry = WorkflowRegistry(
            [WorkflowSpec("fixture", "fixture", script, {"type": "object"})]
        )
        a, _ = fake_client([reply("retained")])
        b, _ = fake_client([])
        first = WorkflowRuntime(
            self.root,
            ModelWorkflowRunner(a, "same", profile="stable-fixture"),
            registry,
        )
        result = first.run("fixture", {})
        first.close()
        second = WorkflowRuntime(
            self.root,
            ModelWorkflowRunner(b, "same", profile="stable-fixture"),
            registry,
        )
        try:
            resumed = second.run("fixture", resume_from_run_id=result["task"]["run_id"])
        finally:
            second.close()
        self.assertEqual(resumed["result"], "retained")
        b.chat.completions.create.assert_not_called()

    def test_close_error_cannot_return_success_from_cli_exit(self):
        s, _, _ = self.session()
        s.run_user("/usage")
        with patch.object(s, "_save_checkpoint", side_effect=OSError("fixture ENOSPC")):
            status = homebrew_harness.session_cli.host_exit_status(s)
            self.assertTrue(s.wait_closed(3))
        self.assertIn(status, {1, 2})
        self.assertEqual(s.shutdown_status()["state"], "error")

    def test_deadline_read_offset_stops_before_traversing_file(self):
        (self.root / "large.txt").write_text("x" * 20000)
        c, _ = fake_client([])
        with ModelGateway(c).scope(deadline=Deadline.after(0)):
            with self.assertRaises(ModelDeadlineExceeded):
                homebrew_harness.tools.files.run_read(
                    "large.txt", self.root, offset=19000
                )

    def test_cron_failure_is_visible_to_recovery_without_restart(self):
        from datetime import datetime

        s, _, _ = self.session()
        s.run_user("/usage")
        s.cron.schedule("* * * * *", "fixture", recurring=False)
        s.cron.poll_due(datetime(2026, 11, 4, 12))
        job = s.cron.take_pending()
        s.cron.received(job)
        status = json.loads(s.run_user("/recovery"))
        self.assertTrue(status["blocked"])
        for row in status["cron"]:
            s.run_user(
                "/recovery inspect " + row["operation_id"] + " " + str(row["revision"])
            )
        s.run_user("/recovery continue")
        self.assertFalse(s.cron.unresolved())

    def test_worker_output_configuration_is_shared_by_bound_gateway(self):
        s, c, requests = self.session([reply("done")], worker_output_tokens=6000)
        s.run_user("perform a short task")
        self.assertEqual(c.chat.completions.create.call_args.kwargs["max_tokens"], 6000)
        self.assertEqual(s.gateway.bind(c).worker_output_tokens, 6000)

    def test_memory_persistent_preference_is_not_skipped(self):
        c, requests = fake_client(
            [
                reply(
                    '[{"name":"language","type":"user","scope":"persistent","evidence":"Always use Chinese."}]'
                )
            ]
        )
        memory = MemoryRuntime(c, "fixture", self.root, mode="auto")
        memory.finish_turn("Always use Chinese.")
        self.assertEqual(len(requests), 1)
        self.assertEqual(memory.store.snapshot().records[0].body, "Always use Chinese.")

    def test_memory_special_file_is_not_opened_as_blocking_stream(self):
        memory = MemoryStore(self.root)
        memory.root.mkdir()
        os.mkfifo(memory.root / "pipe.md")
        started = time.monotonic()
        try:
            memory.snapshot()
        except ValueError:
            pass
        self.assertLess(time.monotonic() - started, 0.3)

    def test_bounded_glob_preserves_recursive_middle_wildcard(self):
        directory = self.root / "src" / "one" / "two"
        directory.mkdir(parents=True)
        (directory / "deep.py").write_text("fixture")
        (self.root / "src" / "direct.py").write_text("fixture")
        result = json.loads(
            homebrew_harness.tools.files.run_glob("src/**/*.py", self.root)
        )
        self.assertEqual(set(result["paths"]), {"src/direct.py", "src/one/two/deep.py"})


if __name__ == "__main__":
    unittest.main()
