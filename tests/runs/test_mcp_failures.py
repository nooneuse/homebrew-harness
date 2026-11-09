"""Real transport faults preserve approval, identity and effect uncertainty."""

import json
import os
import subprocess
import sys
import shlex
import threading
import unittest
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import patch

from homebrew_harness.runs import RunSpec
from homebrew_harness.runs.requests import HumanRequests
from homebrew_harness.runs.supervisor import RunSupervisor
from homebrew_harness.storage.control_boundary import ProposalSuperseded
from homebrew_harness.tools.mcp.runtime import ServerSpec, ToolPolicy
from tests.runs import test_mcp as fixture
from tests.support.clients import reply, call
from tests.support.paths import PROJECT_ROOT


class RunMCPFailures(unittest.TestCase):
    setUp = fixture.RunMCPTests.setUp
    controller = fixture.RunMCPTests.controller
    close = fixture.RunMCPTests.close

    def test_transport_preparation_failure_is_reported_before_model_work(self):
        self.server = replace(
            self.server, args=(str(self.base / "missing-service.py"),)
        )
        control, session, requests = self.controller([])
        outcome = control.execute()
        self.assertEqual(outcome.status, "blocked")
        self.assertEqual(outcome.reason_code, "mcp_preparation_failed")
        self.assertIn("message", outcome.details)
        self.assertEqual(requests, [])
        self.close(control, session)

    def test_host_executable_uses_service_relative_path_and_rechecks_its_identity(self):
        from homebrew_harness.tools.mcp.runtime import MCPBindingChanged

        directory = self.base / "bin"
        directory.mkdir()
        program = directory / "fixture-server"
        program.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' "$@"\n')
        program.chmod(0o755)
        self.server = replace(
            self.server,
            command="fixture-server",
            env={**self.server.env, "PATH": {"env": "RUN_MCP_TEST_PATH"}},
        )
        with patch.dict(os.environ, {"RUN_MCP_TEST_PATH": "bin"}):
            control, session, _ = self.controller([])
        # The environment reference is frozen even after the host variable goes away.
        control.prepare()
        session.mcp.disconnect("docs")
        program.write_text(program.read_text() + "# changed executable\n")
        with self.assertRaises(MCPBindingChanged):
            session.mcp.connect("docs")
        self.close(control, session)

    def fault_server(self, *, readonly=False, transport="stdio"):
        args = (str(PROJECT_ROOT / "tests/support/run_mcp_server.py"),)
        env = {"MCP_FIXTURE_STATE": str(self.base / "service")}
        spec = ServerSpec(
            "docs",
            "stdio",
            sys.executable,
            args,
            self.base,
            env=env,
            timeout_seconds=0.15,
            connect_timeout_seconds=5,
            tools={
                "read": ToolPolicy("allow", True),
                "append": ToolPolicy("allow" if readonly else "confirm", False),
            },
        )
        if transport == "http":
            process = subprocess.Popen(
                [sys.executable, *args, "--transport", "http"],
                env={**os.environ, **env},
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )

            def stop():
                if process.poll() is None:
                    process.kill()
                process.wait(5)
                process.stdout.close()

            self.addCleanup(stop)
            ready = json.loads(process.stdout.readline())
            spec = replace(
                spec,
                transport="streamable_http",
                command=None,
                args=(),
                url=ready["url"],
                env={},
            )
        self.server = spec
        return RunSpec("Use external state", mcp={"docs": ["read", "append"]})

    def invoke(self, name, args):
        return reply(
            calls=[call(name, name="mcp__docs__" + name, arguments=json.dumps(args))]
        )

    def logs(self):
        return [
            json.loads(v)
            for v in (self.base / "service/audit.jsonl").read_text().splitlines()
        ]

    def test_http_read_timeout_is_failed_fact_and_run_can_continue(self):
        spec = self.fault_server(readonly=True, transport="http")
        control, session, _ = self.controller(
            [
                self.invoke("read", {"seconds": 30}),
                reply("Reading failed; report honestly"),
            ],
            spec=spec,
        )
        outcome = control.execute()
        self.assertEqual(outcome.status, "completed", outcome.data())
        result = next(
            v["content"]
            for v in session.messages
            if v.get("role") == "tool" and v.get("tool_call_id") == "read"
        )
        self.assertEqual(json.loads(result)["request_state"], "sent_response_unknown")
        self.assertFalse(session.recovery_operations())
        self.assertEqual(len(self.logs()), 1)
        self.close(control, session)

    def test_http_write_response_lost_is_unknown_and_not_replayed(self):
        spec = self.fault_server(transport="http")
        control, session, _ = self.controller(
            [self.invoke("append", {"text": "once", "seconds": 30})], spec=spec
        )
        self.assertEqual(control.execute().status, "waiting")
        request = HumanRequests(self.store).records(control.id)[0]
        self.close(control, session)
        HumanRequests(self.store).decide(
            control.id, request["id"], request["revision"], "approve"
        )
        resumed, worker, _ = self.controller(
            [
                reply(
                    calls=[
                        call(
                            "approved",
                            name="execute_approved",
                            arguments=json.dumps({"request_id": request["id"]}),
                        )
                    ]
                )
            ],
            identity=control.id,
        )
        outcome = resumed.execute()
        self.assertEqual(outcome.status, "blocked", outcome.data())
        self.assertEqual(
            [v for v in self.logs() if v["kind"] == "write"],
            [{"kind": "write", "value": "once"}],
        )
        self.close(resumed, worker)
        supervisor = RunSupervisor(self.store)
        self.addCleanup(supervisor.close)
        self.assertFalse(supervisor.recovery_safe(self.store.get(control.id)))
        self.assertEqual(
            HumanRequests(self.store).get(control.id, request["id"])["state"],
            "consumed",
        )

    def proposed(self):
        spec = self.fault_server()
        control, session, _ = self.controller(
            [self.invoke("append", {"text": "stale"})], spec=spec
        )
        self.assertEqual(control.execute().status, "waiting")
        request = HumanRequests(self.store).records(control.id)[0]
        self.close(control, session)
        return control, request

    def test_schema_change_invalidates_approval_before_model_work(self):
        previous, request = self.proposed()
        HumanRequests(self.store).decide(
            previous.id, request["id"], request["revision"], "approve"
        )
        (self.base / "service/schema.txt").write_text("New external schema release")
        control, session, requests = self.controller([], identity=previous.id)
        outcome = control.execute()
        self.assertEqual(outcome.status, "blocked", outcome.data())
        self.assertEqual(requests, [])
        self.assertEqual(
            HumanRequests(self.store).get(control.id, request["id"])["state"], "stale"
        )
        self.close(control, session)

    def test_host_revision_change_invalidates_approval(self):
        previous, request = self.proposed()
        self.store.runtime.update_fields(
            "runs", previous.id, {"expected_mcp": previous.state["bindings"]["mcp"]}
        )
        self.server = replace(self.server, revision="new-release")
        control, session, requests = self.controller([], identity=previous.id)
        self.assertEqual(control.execute().status, "blocked")
        self.assertEqual(requests, [])
        self.assertEqual(
            HumanRequests(self.store).get(control.id, request["id"])["state"], "stale"
        )
        self.close(control, session)

    def test_steering_cannot_approve_an_old_mcp_proposal(self):
        control, request = self.proposed()
        self.store.submit(control.id, "Change the note requirement", mode="queued")
        with self.assertRaises(ValueError):
            HumanRequests(self.store).decide(
                control.id, request["id"], request["revision"], "approve"
            )
        self.assertFalse((self.base / "service/audit.jsonl").exists())

    def test_ready_guard_failure_is_not_mistaken_for_prior_manifest_commit(self):
        control, session, _ = self.controller([reply("done")])
        self.assertEqual(control.execute().status, "completed")
        with patch(
            "homebrew_harness.storage.control_boundary.check_guard",
            side_effect=ProposalSuperseded("new input"),
        ):
            with self.assertRaises(ProposalSuperseded):
                control.prepare()
        self.close(control, session)

    def test_ready_commit_ack_loss_is_resolved_by_this_acceptance(self):
        control, session, requests = self.controller([reply("done")])
        db = self.store.runtime.backend()
        original = db.write

        @contextmanager
        def lost_ack():
            with original() as conn:
                before = db._get(conn, "runs", control.id).get("mcp_preparation")
                yield conn
                after = db._get(conn, "runs", control.id).get("mcp_preparation")
            if before != after:
                raise RuntimeError("Committed readiness acknowledgement lost")

        with patch.object(db, "write", lost_ack):
            result = control.execute()
        self.assertEqual(result.status, "completed", result.data())
        self.assertEqual(len(requests), 1)
        self.close(control, session)

    def test_selected_stdio_can_use_the_default_process_cwd(self):
        self.server = replace(self.server, cwd=None)
        control, session, _ = self.controller(
            [
                reply(
                    calls=[
                        call(
                            "read",
                            name="mcp__docs__search",
                            arguments='{"query":"loop"}',
                        )
                    ]
                ),
                reply("Read completed"),
            ]
        )
        self.assertEqual(control.execute().status, "completed")
        self.close(control, session)

    def test_idle_close_does_not_cancel_an_active_http_write(self):
        from tests.runtime.test_interaction import eventually

        spec = self.fault_server(transport="http")
        self.server = replace(self.server, timeout_seconds=3)
        control, session, _ = self.controller([], spec=spec)
        control.prepare()
        binding = session.mcp.snapshot()[1]["mcp__docs__append"]
        outputs = []
        thread = threading.Thread(
            target=lambda: outputs.append(
                session.mcp.call(binding, {"text": "once", "seconds": 0.5})
            )
        )
        thread.start()
        self.addCleanup(lambda: thread.join(4))
        eventually(lambda: (self.base / "service/audit.jsonl").exists(), 3)
        self.assertFalse(session.mcp._submit(binding.connection.close_idle(), 1))
        thread.join(4)
        self.assertFalse(thread.is_alive())
        self.assertEqual(json.loads(outputs[0])["content"][0]["text"], "stored")
        self.assertFalse(binding.connection.closed)
        self.close(control, session)

    def test_read_crash_settlement_updates_local_gate_and_durable_record(self):
        control, session, _ = self.controller([reply("done")])
        self.assertEqual(control.execute().status, "completed")
        descriptor = session.mcp.descriptor("mcp__docs__search", workdir=self.root)
        row = {
            "operation_id": "op:lost",
            "call_id": "lost",
            "name": "mcp__docs__search",
            "revision": 0,
            "state": "started",
            "mcp": descriptor,
            "managed_run": control.header,
            "checkpoint": session.checkpoint_key,
        }
        self.store.runtime.put("operations", row["operation_id"], row)
        saved = {
            "managed_run": {**control.identity(), **control.header},
            "recovery_blocked": ["lost"],
        }
        restored = [{"role": "tool", "tool_call_id": "lost", "content": "unknown"}]
        self.assertEqual(control.recovered(saved, [row], restored, ["lost"]), [])
        self.assertEqual(row["state"], "done")
        self.assertEqual(saved["recovery_blocked"], [])
        self.assertEqual(json.loads(restored[0]["content"])["outcome"], "failed")
        self.assertEqual(
            self.store.runtime.get("operations", "op:lost")["state"], "done"
        )
        saved["recovery_blocked"] = ["lost"]
        restored[0]["content"] = "unknown"
        self.assertEqual(control.recovered(saved, [row], restored, ["lost"]), [])
        self.assertEqual(saved["recovery_blocked"], [])
        self.assertEqual(json.loads(restored[0]["content"])["outcome"], "failed")
        self.close(control, session)

    def test_reused_call_id_keeps_the_other_batch_write_uncertain(self):
        from homebrew_harness.context.tracked_history import encoded_message

        control, session, _ = self.controller([reply("done")])
        self.assertEqual(control.execute().status, "completed")
        read = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                call("same", name="mcp__docs__search", arguments='{"query":"loop"}')
            ],
        }
        write = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                call("same", name="mcp__docs__record_note", arguments='{"text":"once"}')
            ],
        }
        rows = []
        for index, (batch, name) in enumerate(
            ((read, "search"), (write, "record_note"))
        ):
            row = {
                "operation_id": "op" + str(index) + ":same",
                "call_id": "same",
                "name": "mcp__docs__" + name,
                "batch_id": encoded_message(batch)[1],
                "revision": 0,
                "state": "unknown",
                "mcp": session.mcp.descriptor("mcp__docs__" + name, workdir=self.root),
                "managed_run": control.header,
                "checkpoint": session.checkpoint_key,
            }
            self.store.runtime.put("operations", row["operation_id"], row)
            rows.append(row)
        messages = [
            read,
            {"role": "tool", "tool_call_id": "same", "content": "unknown read"},
            write,
            {"role": "tool", "tool_call_id": "same", "content": "unknown write"},
        ]
        saved = {
            "managed_run": {**control.identity(), **control.header},
            "recovery_blocked": ["same"],
        }
        self.assertEqual(control.recovered(saved, rows, messages, ["same"]), ["same"])
        self.assertEqual(json.loads(messages[1]["content"])["outcome"], "failed")
        self.assertEqual(messages[3]["content"], "unknown write")
        self.assertEqual(rows[1]["state"], "unknown")
        self.close(control, session)

    def test_identical_read_batches_preserve_an_earlier_known_response(self):
        from homebrew_harness.context.tracked_history import encoded_message
        from homebrew_harness.storage.durable_runtime import recovered_output

        control, session, _ = self.controller([reply("done")])
        self.assertEqual(control.execute().status, "completed")
        batch = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                call("same", name="mcp__docs__search", arguments='{"query":"loop"}')
            ],
        }
        row = {
            "operation_id": "op:same",
            "call_id": "same",
            "name": "mcp__docs__search",
            "batch_id": encoded_message(batch)[1],
            "revision": 0,
            "state": "unknown",
            "mcp": session.mcp.descriptor("mcp__docs__search", workdir=self.root),
            "managed_run": control.header,
            "checkpoint": session.checkpoint_key,
        }
        self.store.runtime.put("operations", row["operation_id"], row)
        known = json.dumps(
            {"content": [{"type": "text", "text": "Earlier known response"}]}
        )
        messages = [
            batch,
            {"role": "tool", "tool_call_id": "same", "content": known},
            dict(batch),
            {"role": "tool", "tool_call_id": "same", "content": recovered_output(None)},
        ]
        saved = {
            "managed_run": {**control.identity(), **control.header},
            "recovery_blocked": ["same"],
        }
        self.assertEqual(control.recovered(saved, [row], messages, ["same"]), [])
        self.assertEqual(messages[1]["content"], known)
        self.assertEqual(json.loads(messages[3]["content"])["outcome"], "failed")
        self.close(control, session)
