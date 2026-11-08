import copy
import io
import json
import tempfile
import time
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from homebrew_harness import HarnessSession, Capabilities
from homebrew_harness.execution.execution_leases import ExecutionLeases
from homebrew_harness.execution.model_protocol import ProviderError
from homebrew_harness.runs import RunSpec, RunStore, RunController
from homebrew_harness.runs.supervisor import RunSupervisor
from homebrew_harness.storage.durable_runtime import checkpoint_scope
from tests.support.clients import fake_client, reply, call


class RunTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for capture in (redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            capture.__enter__()
            self.addCleanup(capture.__exit__, None, None, None)
        self.store = RunStore(self.root)
        self.addCleanup(self.store.close)

    def controller(self, responses, spec=None, identity=None, **kwargs):
        state = (
            self.store.create(
                spec
                or RunSpec(
                    "Original constraints: create and verify a file",
                    episode_model_calls=1,
                )
            )
            if identity is None
            else self.store.get(identity)
        )
        state = self.store.claim(state["id"])
        control = RunController(
            self.store, state["id"], state["owner"], state["generation"]
        )
        client, snapshots = fake_client(responses)
        session = HarnessSession(
            client,
            "fixture",
            self.root,
            capabilities=Capabilities(control.spec.enabled),
            run_control=control,
            shutdown_timeout=0.02,
            **kwargs,
        )
        self.addCleanup(control.close_heartbeat)
        self.addCleanup(lambda: self.assertTrue(session.wait_closed(5)))
        self.addCleanup(session.close)
        return control, session, client, snapshots

    def write(self, identity, path, content="ok"):
        return reply(
            calls=[
                call(
                    identity,
                    name="write_file",
                    arguments=json.dumps({"path": path, "content": content}),
                )
            ]
        )

    def test_multiple_episodes_submit_original_once_and_preserve_results(self):
        control, session, client, _ = self.controller(
            [self.write("a", "a.txt"), self.write("b", "b.txt"), reply("done")]
        )
        outcome = control.execute()
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.episode, 3)
        self.assertEqual(client.chat.completions.create.call_count, 3)
        self.assertEqual(
            sum(m.get("content") == control.spec.prompt for m in session.messages), 1
        )
        self.assertEqual(session.intent.original, control.spec.prompt)
        self.assertEqual((self.root / "a.txt").read_text(), "ok")
        self.assertEqual((self.root / "b.txt").read_text(), "ok")
        self.assertEqual(len(self.store.runtime.items("run_episodes")), 3)

    def test_goal_completion_uses_latest_user_revision_and_atomic_run_state(self):
        spec = RunSpec(
            "Create result.txt",
            goal={
                "condition": "result exists",
                "judge": False,
                "predicates": [
                    {"id": "result", "type": "file_exists", "path": "result.txt"}
                ],
            },
            episode_model_calls=1,
        )
        control, session, _, _ = self.controller([self.write("a", "result.txt")], spec)
        outcome = control.execute()
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(session.goal.status()["status"], "achieved")
        state = self.store.get(control.id)
        checkpoint = self.store.runtime.get("checkpoints", session.checkpoint_key)
        self.assertEqual(state["boundary_phase"], "executed")
        self.assertEqual(checkpoint["phase"], "executed")
        self.assertEqual(state["last_outcome"]["status"], "completed")

    def test_pending_queued_amendment_cannot_be_lost_at_completion(self):
        control, session, _, snapshots = self.controller(
            [reply("first"), reply("updated")]
        )
        seen = []

        def steer(messages):
            if not seen:
                seen.append(
                    self.store.submit(
                        control.id, "Also include the detail", mode="queued"
                    )
                )

        session.hooks.register_hook("Stop", steer)
        outcome = control.execute()
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.text, "updated")
        self.assertIn("Also include the detail", session.intent.amendments)
        self.assertFalse(self.store.status(control.id)["pending_inputs"])
        self.assertEqual(session.intent.original, control.spec.prompt)

    def test_transient_retry_counter_persists_across_controller_restart(self):
        control, session, client, _ = self.controller([])
        client.chat.completions.create.side_effect = ProviderError("transport")
        outcome = control.execute()
        self.assertEqual(outcome.status, "retrying")
        identity = control.id
        session.close()
        self.assertTrue(session.wait_closed(5))
        control.close_heartbeat()
        self.store.update(control.header, {"next_retry_at": 0})
        next_control, next_session, _, _ = self.controller(
            [reply("recovered")], identity=identity
        )
        self.assertEqual(next_control.execute().status, "completed")
        self.assertEqual(self.store.get(identity)["recovery_count"], 1)
        self.assertEqual(
            sum(m.get("content") == control.spec.prompt for m in next_session.messages),
            1,
        )

    def test_unknown_tool_effect_blocks_and_does_not_run_next_proposal(self):
        control, session, _, _ = self.controller(
            [
                reply(
                    calls=[
                        call(
                            "a",
                            name="write_file",
                            arguments='{"path":"a.txt","content":"a"}',
                        ),
                        call(
                            "b",
                            name="write_file",
                            arguments='{"path":"b.txt","content":"b"}',
                        ),
                    ]
                )
            ]
        )
        from homebrew_harness.tools.tool_outcome import ToolExecutionError

        with patch(
            "homebrew_harness.tools.registry.TOOL_HANDLERS",
            {
                **__import__(
                    "homebrew_harness.tools.registry", fromlist=["TOOL_HANDLERS"]
                ).TOOL_HANDLERS,
                "write_file": lambda **kwargs: (_ for _ in ()).throw(
                    ToolExecutionError(
                        "effect unknown",
                        outcome="unknown",
                        details={"path": kwargs["path"]},
                    )
                ),
            },
        ):
            outcome = control.execute()
        self.assertEqual(outcome.status, "blocked")
        self.assertEqual(outcome.reason_code, "recovery_required")
        self.assertFalse((self.root / "b.txt").exists())
        self.assertTrue(session.recovery_operations())

    def test_expired_run_sends_no_model_request(self):
        control, session, client, _ = self.controller(
            [], RunSpec("Do work", total_seconds=0.1)
        )
        self.store.update(control.header, {"deadline_at": time.time() - 1})
        control.refresh()
        self.assertEqual(control.execute().reason_code, "deadline_reached")
        client.chat.completions.create.assert_not_called()

    def test_clock_rollback_blocks_without_extending_time(self):
        control, session, client, _ = self.controller([])
        with patch(
            "homebrew_harness.runs.controller.time.time",
            return_value=control.started_clock - 10,
        ):
            outcome = control.execute()
        self.assertEqual(outcome.reason_code, "clock_changed")
        client.chat.completions.create.assert_not_called()

    def test_permission_requirement_stops_batch_without_auto_approval(self):
        tools = RunSpec("Execute a command", tools=("bash", "write_file"))
        control, session, _, _ = self.controller(
            [
                reply(
                    calls=[
                        call("a", "true"),
                        call(
                            "b",
                            name="write_file",
                            arguments='{"path":"b.txt","content":"b"}',
                        ),
                    ]
                )
            ],
            tools,
        )
        self.assertEqual(control.execute().reason_code, "approval_required")
        self.assertFalse((self.root / "b.txt").exists())

    def test_pause_and_resume_preserve_task_and_do_not_reset_deadline(self):
        control, session, _, _ = self.controller(
            [self.write("a", "a.txt"), reply("done")]
        )
        original_deadline = self.store.get(control.id)["deadline_at"]
        session.hooks.register_hook(
            "PostToolUse", lambda event, output: self.store.control(control.id, "pause")
        )
        self.assertEqual(control.execute().status, "paused")
        session.close()
        self.assertTrue(session.wait_closed(5))
        control.close_heartbeat()
        self.store.ended(control.header, cleanup=True)
        self.store.control(control.id, "run")
        resumed, s2, _, _ = self.controller([reply("done")], identity=control.id)
        self.assertEqual(resumed.execute().status, "completed")
        self.assertEqual(self.store.get(control.id)["deadline_at"], original_deadline)
        self.assertEqual((self.root / "a.txt").read_text(), "ok")

    def test_heartbeat_does_not_mutate_context_or_core_run_revision(self):
        control, session, _, _ = self.controller([])
        control.prepare()
        messages = copy.deepcopy(session.messages)
        revision = self.store.get(control.id)["revision"]
        control.start_heartbeat()
        time.sleep(0.05)
        control.close_heartbeat()
        self.assertEqual(session.messages, messages)
        self.assertEqual(self.store.get(control.id)["revision"], revision)
        self.assertEqual(
            self.store.status(control.id)["heartbeat"]["owner"], control.header["owner"]
        )

    def test_live_execution_lease_prevents_supervisor_takeover(self):
        state = self.store.create(RunSpec("Work"))
        lease = ExecutionLeases(self.root)
        lease.acquire("session")
        self.addCleanup(lease.close)
        supervisor = RunSupervisor(self.store)
        self.addCleanup(supervisor.close)
        result = supervisor.supervise(state["id"])
        self.assertEqual(result["supervisor_reason"], "execution_lease_held")
        self.assertEqual(result["generation"], 0)

    def test_terminal_run_rejects_new_input_and_second_active_run_is_rejected(self):
        control, session, _, _ = self.controller([reply("done")])
        with self.assertRaises(ValueError):
            self.store.create(RunSpec("Another"))
        self.assertEqual(control.execute().status, "completed")
        with self.assertRaises(ValueError):
            self.store.submit(control.id, "More")

    def test_stale_owner_cannot_commit_checkpoint_or_operation(self):
        control, session, _, _ = self.controller([])
        control.prepare()
        self.store.claim(control.id)
        with checkpoint_scope(session._checkpoint_writer):
            with self.assertRaises(ValueError):
                session._checkpoint_writer.save()
        # Restore ownership solely to let the test close its resources normally.
        db = self.store.runtime.backend()
        with db.write() as conn:
            current = db._get(conn, "runs", control.id)
            db._put(
                conn,
                "runs",
                control.id,
                {
                    **current,
                    "owner": control.header["owner"],
                    "generation": control.header["generation"],
                },
            )

    def test_spec_rejects_future_capabilities_and_secret_values(self):
        for values in [
            {"prompt": "Work", "enabled": ["teams"]},
            {"prompt": "Work", "total_seconds": True},
            {"prompt": "Work", "tools": ["unknown"]},
        ]:
            with self.assertRaises(ValueError):
                RunSpec.from_dict(values)
