import io
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

from homebrew_harness import HarnessSession, Capabilities
from homebrew_harness.execution.model_gateway import ModelDeadlineExceeded
from homebrew_harness.execution.test_runner import TestRunner, TestRecipe
from homebrew_harness.runs import RunStore, RunSpec, RunController
from homebrew_harness.runs.supervisor import RunSupervisor
from homebrew_harness.storage.checkpoint_retirement import workspace_blockers
from tests.support.clients import fake_client, reply, call


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.store = RunStore(self.root)
        self.addCleanup(self.store.close)
        for capture in (redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            capture.__enter__()
            self.addCleanup(capture.__exit__, None, None, None)

    def session(self, responses, spec, **kwargs):
        state = self.store.create(spec)
        state = self.store.claim(state["id"])
        control = RunController(
            self.store, state["id"], state["owner"], state["generation"]
        )
        client, _ = fake_client(responses)
        session = HarnessSession(
            client,
            "fixture",
            self.root,
            capabilities=Capabilities(),
            run_control=control,
            shutdown_timeout=0.02,
            **kwargs,
        )
        self.addCleanup(control.close_heartbeat)
        self.addCleanup(lambda: self.assertTrue(session.wait_closed(5)))
        self.addCleanup(session.close)
        return control, session, client

    def goal(self):
        return {
            "condition": "file exists",
            "judge": False,
            "predicates": [{"id": "file", "type": "file_exists", "path": "out.txt"}],
        }

    def test_goal_stop_limit_survives_normal_episode_yields(self):
        control, session, client = self.session(
            [reply("not yet") for _ in range(10)],
            RunSpec("Create file", goal=self.goal(), episode_model_calls=1),
        )
        outcome = control.execute()
        self.assertEqual(outcome.status, "blocked")
        self.assertEqual(outcome.reason_code, "goal_limit")
        self.assertLessEqual(client.chat.completions.create.call_count, 9)
        self.assertGreater(session.goal.state["episode_checks"], 1)

    def test_model_deadline_report_cannot_become_completed(self):
        control, session, client = self.session(
            [], RunSpec("Create file", goal=self.goal())
        )
        client.chat.completions.create.side_effect = ModelDeadlineExceeded(
            "request deadline"
        )
        outcome = control.execute()
        self.assertNotEqual(outcome.status, "completed")
        self.assertEqual(outcome.status, "blocked")

    def test_fixed_test_attempts_do_not_replenish_on_restart(self):
        recipe = TestRecipe(
            "check",
            (sys.executable, "-c", 'print("ok")'),
            max_runs=1,
            approval_seconds=300,
            input_globs=("*.txt",),
            locked_globs=(),
        )
        tests = TestRunner(self.root, [recipe])
        self.addCleanup(tests.close)
        response = reply(
            calls=[call("check", name="run_tests", arguments='{"name":"check"}')]
        )
        control, session, _ = self.session(
            [response, reply("done")],
            RunSpec("Run test", allow_tests=("check",)),
            test_runner=tests,
        )
        self.assertEqual(control.execute().status, "completed")
        self.assertEqual(self.store.get(control.id)["test_attempts"]["check"], 1)
        session.close()
        self.assertTrue(session.wait_closed(5))
        control.close_heartbeat()
        # Test only recovery authorization: a nonterminal snapshot with consumed grant.
        self.store.update(control.header, {"status": "retrying", "next_retry_at": None})
        state = self.store.claim(control.id)
        resumed = RunController(
            self.store, control.id, state["owner"], state["generation"]
        )
        api, _ = fake_client(
            [
                reply(
                    calls=[
                        call("renew", name="run_tests", arguments='{"name":"check"}')
                    ]
                )
            ]
        )
        again = TestRunner(self.root, [recipe])
        self.addCleanup(again.close)
        s2 = HarnessSession(
            api,
            "fixture",
            self.root,
            run_control=resumed,
            capabilities=Capabilities(),
            test_runner=again,
            shutdown_timeout=0.02,
        )
        self.addCleanup(resumed.close_heartbeat)
        self.addCleanup(lambda: self.assertTrue(s2.wait_closed(5)))
        self.addCleanup(s2.close)
        result = resumed.execute()
        self.assertEqual(result.status, "waiting")
        self.assertEqual(result.reason_code, "approval_required")
        self.assertEqual(again.runs, 0)
        self.assertEqual(self.store.get(control.id)["test_attempts"]["check"], 1)

    def test_unknown_running_operation_prevents_recovery_proof(self):
        state = self.store.create(RunSpec("Work"))
        self.store.runtime.put(
            "operations",
            "op",
            {
                "checkpoint": "lead:" + state["session_id"],
                "state": "unknown",
                "call_id": "x",
            },
        )
        supervisor = RunSupervisor(self.store)
        self.addCleanup(supervisor.close)
        self.assertFalse(supervisor.recovery_safe(state))

    def test_expired_orphaned_run_preserves_unknown_operation_and_retirement_root(self):
        state = self.store.create(
            RunSpec("Work", total_seconds=0.1), now=time.time() - 1
        )
        self.store.runtime.put(
            "operations",
            "op",
            {
                "checkpoint": "lead:" + state["session_id"],
                "state": "unknown",
                "call_id": "x",
            },
        )
        supervisor = RunSupervisor(self.store)
        self.addCleanup(supervisor.close)
        result = supervisor.supervise(state["id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["last_outcome"]["reason_code"], "deadline_reached")
        self.assertFalse(result["cleanup_complete"])
        self.assertFalse(result["last_outcome"]["cleanup_complete"])
        self.assertTrue(result["last_outcome"]["required_host_actions"])
        self.assertEqual(result["generation"], 0)
        self.assertEqual(self.store.runtime.get("operations", "op")["state"], "unknown")
        with self.assertRaises(ValueError):
            self.store.create(RunSpec("New task"))
        data = {
            "runs": {state["id"]: result},
            "inbox": {},
            "background": {},
            "cron": {},
            "goals": {},
            "input_receipts": {},
        }
        self.assertIn(
            "managed_run_unfinished",
            workspace_blockers(data, {"records": {}}, state["session_id"]),
        )

    def test_expired_run_preserves_unsettled_background_cleanup(self):
        state = self.store.create(
            RunSpec("Work", total_seconds=0.1), now=time.time() - 1
        )
        self.store.runtime.put(
            "background", "bg_test", {"status": "running", "instance": "old"}
        )
        supervisor = RunSupervisor(self.store)
        self.addCleanup(supervisor.close)
        result = supervisor.supervise(state["id"])
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["cleanup_complete"])
        self.assertTrue(result["last_outcome"]["required_host_actions"])
        self.assertEqual(result["generation"], 0)

    def test_live_pid_is_not_killed_or_replaced(self):
        state = self.store.create(RunSpec("Work"))
        state = self.store.claim(state["id"])
        header = {
            "run_id": state["id"],
            "owner": state["owner"],
            "generation": state["generation"],
        }
        self.store.pid(header, os.getpid())
        supervisor = RunSupervisor(self.store)
        self.addCleanup(supervisor.close)
        result = supervisor.supervise(state["id"])
        self.assertEqual(result["supervisor_reason"], "worker_process_alive")
        self.assertEqual(result["generation"], 1)

    def test_active_run_protects_closed_checkpoint_from_retirement(self):
        state = self.store.create(RunSpec("Work"))
        data = {
            "runs": {state["id"]: state},
            "inbox": {},
            "background": {},
            "cron": {},
            "goals": {},
            "input_receipts": {},
        }
        self.assertIn(
            "managed_run_unfinished",
            workspace_blockers(data, {"records": {}}, state["session_id"]),
        )
        data["runs"][state["id"]].update(status="completed", cleanup_complete=True)
        self.assertNotIn(
            "managed_run_unfinished",
            workspace_blockers(data, {"records": {}}, state["session_id"]),
        )

    def test_input_commit_failure_does_not_lose_initial_request(self):
        control, session, client = self.session(
            [reply("done")], RunSpec("Original request")
        )
        original = self.store.runtime.checkpoint
        injected = []

        def checkpoint(*args, **kwargs):
            managed = (kwargs.get("boundary") or {}).get("managed")
            if managed and managed["value"].get("submitted") and not injected:
                injected.append(True)
                raise OSError("injected commit failure")
            return original(*args, **kwargs)

        # Same RuntimeStore object as Session uses, not the independent store facade.
        session.runtime_store.checkpoint = checkpoint
        result = control.execute()
        self.assertNotEqual(result.status, "completed")
        self.assertFalse(self.store.get(control.id)["submitted"])
        client.chat.completions.create.assert_not_called()

    def test_managed_session_cannot_reset_failure_state_through_run_user(self):
        control, session, client = self.session([], RunSpec("Work"))
        with self.assertRaises(ValueError):
            session.run_user("continue")
        client.chat.completions.create.assert_not_called()

    def test_initial_commit_return_loss_is_confirmed_without_duplicate_submission(self):
        control, session, client = self.session(
            [reply("done")], RunSpec("Original request")
        )
        original = session.runtime_store.checkpoint
        injected = []

        def checkpoint(*args, **kwargs):
            result = original(*args, **kwargs)
            managed = (kwargs.get("boundary") or {}).get("managed")
            if managed and managed["value"].get("submitted") and not injected:
                injected.append(True)
                raise OSError("commit reply lost")
            return result

        session.runtime_store.checkpoint = checkpoint
        self.assertEqual(control.execute().status, "completed")
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertEqual(
            sum(m.get("managed_initial") == control.id for m in session.messages), 1
        )

    def test_yield_commit_return_loss_does_not_replay_previous_tool(self):
        response = reply(
            calls=[
                call(
                    "write",
                    name="write_file",
                    arguments='{"path":"out.txt","content":"done"}',
                )
            ]
        )
        control, session, client = self.session(
            [response, reply("done")], RunSpec("Write output", episode_model_calls=1)
        )
        original = session.runtime_store.checkpoint
        injected = []

        def checkpoint(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[1].get("phase") == "yielded" and not injected:
                injected.append(True)
                raise OSError("commit reply lost")
            return result

        session.runtime_store.checkpoint = checkpoint
        self.assertEqual(control.execute().status, "completed")
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertFalse(session.recovery_operations())

    def test_old_generation_input_is_rejected_transactionally(self):
        state = self.store.create(RunSpec("Work"))
        self.store.claim(state["id"])
        with self.assertRaises(ValueError):
            self.store.submit(state["id"], "Amend", expected_generation=0)
        self.assertFalse(self.store.status(state["id"])["pending_inputs"])

    def test_cancel_invalidates_old_proposals_before_execution_acceptance(self):
        control, session, client = self.session(
            [
                reply(
                    calls=[
                        call(
                            "write",
                            name="write_file",
                            arguments='{"path":"out.txt","content":"no"}',
                        )
                    ]
                )
            ],
            RunSpec("Write output"),
        )
        original = client.chat.completions.create.side_effect

        def create(**kwargs):
            value = original(**kwargs)
            self.store.control(control.id, "cancel")
            return value

        client.chat.completions.create.side_effect = create
        self.assertEqual(control.execute().status, "cancelled")
        self.assertFalse((self.root / "out.txt").exists())

    def test_pause_during_retry_wait_does_not_launch_or_wait_for_retry_due(self):
        state = self.store.create(RunSpec("Work"))
        state = self.store.claim(state["id"])
        header = {
            "run_id": state["id"],
            "owner": state["owner"],
            "generation": state["generation"],
        }
        self.store.update(
            header,
            {
                "status": "retrying",
                "next_retry_at": time.time() + 60,
                "cleanup_complete": True,
            },
        )
        self.store.control(state["id"], "pause")
        supervisor = RunSupervisor(self.store)
        self.addCleanup(supervisor.close)
        start = time.monotonic()
        result = supervisor.supervise(state["id"])
        self.assertEqual(result["status"], "paused")
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(result["generation"], 1)

    def test_retry_wait_cannot_extend_deadline_when_wall_clock_stalls(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        state = self.store.create(RunSpec("Work", total_seconds=0.2))
        state = self.store.claim(state["id"])
        header = {
            "run_id": state["id"],
            "owner": state["owner"],
            "generation": state["generation"],
        }
        self.store.update(
            header,
            {
                "status": "retrying",
                "next_retry_at": state["created_at"] + 60,
                "cleanup_complete": True,
            },
        )
        supervisor = RunSupervisor(self.store)
        self.addCleanup(supervisor.close)
        start = time.monotonic()
        # Also bound a regressed implementation: wall time resumes after one second.
        clock = SimpleNamespace(
            time=lambda: (
                state["created_at"] + (0.01 if time.monotonic() - start < 1 else 2)
            ),
            monotonic=time.monotonic,
            sleep=time.sleep,
        )
        with patch("homebrew_harness.runs.supervisor.time", clock):
            result = supervisor.supervise(state["id"])
        self.assertLess(time.monotonic() - start, 0.8)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["last_outcome"]["reason_code"], "deadline_reached")
        self.assertTrue(result["cleanup_complete"])
        self.assertEqual(result["generation"], 1)

    def test_resume_is_refused_while_cleanup_is_pending(self):
        state = self.store.create(RunSpec("Work"))
        state = self.store.claim(state["id"])
        header = {
            "run_id": state["id"],
            "owner": state["owner"],
            "generation": state["generation"],
        }
        self.store.update(header, {"status": "paused", "cleanup_complete": False})
        with self.assertRaises(ValueError):
            self.store.control(state["id"], "run")

    def test_dead_model_observations_become_unknown_once(self):
        from homebrew_harness.runtime.runtime_state import settle_dead_usage

        data = {
            "unknown_usage_calls": 1,
            "pending_requests": [{"sent": True, "role": "lead", "purpose": "worker"}],
            "inflight": 1,
            "pending_usage_calls": 1,
        }
        result = settle_dead_usage(data)
        self.assertEqual(result["unknown_usage_calls"], 2)
        self.assertEqual(result["inflight"], 0)
        self.assertEqual(settle_dead_usage(result)["unknown_usage_calls"], 2)

    def test_unsettled_background_record_blocks_takeover(self):
        state = self.store.create(RunSpec("Work"))
        self.store.runtime.put(
            "background", "bg_test", {"status": "running", "instance": "old"}
        )
        supervisor = RunSupervisor(self.store)
        self.addCleanup(supervisor.close)
        self.assertFalse(supervisor.recovery_safe(state))

    def test_enabled_subagents_and_workflow_receive_their_tools(self):
        spec = RunSpec("Work", enabled=("subagents", "workflow"))
        self.assertIn("task", spec.tools)
        self.assertIn("workflow", spec.tools)
