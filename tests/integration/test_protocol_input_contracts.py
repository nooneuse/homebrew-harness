"""Regression coverage for revision commits and normalized model contracts."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlunsplit

import httpx
from homebrew_harness import HarnessSession, Capabilities
from homebrew_harness.context.compaction import ContextCompactor, CompactionError
from homebrew_harness.context.skill_loader import SkillLoader
from homebrew_harness.execution.model_gateway import ModelGateway, ModelGatewayError
from homebrew_harness.execution.model_protocol import (
    ModelRequest,
    ModelProtocolError,
    ProviderError,
    save_replay,
)
from homebrew_harness.execution.providers.native import NativeAdapter
from homebrew_harness.execution.providers.common import binding_key
from homebrew_harness.execution.providers import anthropic
from homebrew_harness.orchestration.goal_loop import GoalController, GoalEvaluation
from homebrew_harness.orchestration.teams import TeamManager, TeammateRuntime
from homebrew_harness.runtime.hooks import create_hooks
from homebrew_harness.runtime.turns import run_member_loop
from tests.support.clients import fake_client, reply, call
from tests.integration.test_native_model_protocols import ProtocolService, responses


def local_endpoint():
    return urlunsplit(("http", "127.0.0.1", "/", "", ""))


class ProtocolInputContracts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for capture in (redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            capture.__enter__()
            self.addCleanup(capture.__exit__, None, None, None)

    def session(self, client, **kwargs):
        session = HarnessSession(
            client, "fixture", self.root, shutdown_timeout=0.02, **kwargs
        )
        self.addCleanup(lambda: self.assertTrue(session.wait_closed(5)))
        self.addCleanup(session.close)
        return session

    def adapter(self, protocol="openai_responses", **kwargs):
        adapter = NativeAdapter(
            protocol,
            api_key="synthetic",
            base_url=kwargs.pop("base_url", local_endpoint()),
            **kwargs,
        )
        self.addCleanup(adapter.close)
        return adapter

    def gateway(self, adapter):
        gateway = ModelGateway(adapter)
        self.addCleanup(lambda: self.assertTrue(gateway.wait_idle(3)))
        self.addCleanup(gateway.close)
        return gateway

    def test_stop_hook_input_prevents_stale_return(self):
        client, _ = fake_client([reply("old"), reply("updated")])
        session = self.session(client)
        entered = []

        def steer(messages):
            if not entered:
                entered.append(True)
                session.submit_user_input("Require an updated answer")

        session.hooks.register_hook("Stop", steer)
        self.assertEqual(session.run_user("Answer"), "updated")
        self.assertFalse(session.inputs.pending())

    def test_goal_candidate_is_invalidated_without_publishing_achieved(self):
        client, _ = fake_client([reply("old"), reply("updated")])
        seen = []

        def evaluate(packet):
            seen.append(goal.store.load(goal.state["goal_id"])["status"])
            if len(seen) == 1:
                session.submit_user_input("Also explain the missing case")
            return GoalEvaluation(True, "Answer present", evidence_ids=("final",))

        goal = GoalController(self.root, SimpleNamespace(evaluate=evaluate))
        session = self.session(client, goal_controller=goal)
        result = session.run_user("/goal Answer")
        self.assertEqual(seen, ["active", "active"])
        self.assertIn("updated", result)
        self.assertEqual(goal.status()["status"], "achieved")
        self.assertFalse(session.inputs.pending())
        self.assertFalse(list((self.root / ".harness/goals").glob("goal_*.json")))

    def test_completion_commit_result_loss_is_read_back(self):
        client, _ = fake_client([reply("done")])
        session = self.session(client)
        session.run_user("/usage")
        original = session.runtime_store.checkpoint
        injected = []

        def checkpoint(*args, **kwargs):
            result = original(*args, **kwargs)
            if (kwargs.get("boundary") or {}).get("end_turn") and not injected:
                injected.append(True)
                raise OSError("lost commit response")
            return result

        with patch.object(session.runtime_store, "checkpoint", side_effect=checkpoint):
            self.assertEqual(session.run_user("Answer"), "done")
        self.assertEqual(len(injected), 1)
        self.assertIsNone(session.inputs.active_id)

    def test_member_skips_remainder_of_superseded_batch(self):
        manager = TeamManager(self.root, Mock(return_value="done"))
        self.addCleanup(manager.close)
        member = TeammateRuntime(manager, "worker", "coder", "write files", False)
        manager.members["worker"] = member
        task = manager.store.create("Write", "Write two files")
        member.bind(member.store.claim(task.id))
        hooks = create_hooks()

        def steer(event, output):
            if event.call_id == "first":
                manager.bus.send(
                    "lead",
                    "worker",
                    "Do not write second.txt",
                    "user_steer",
                    {"origin": "host_user", "input_id": "input-1", "revision": 1},
                )

        hooks.register_hook("PostToolUse", steer)
        client, _ = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "first",
                            name="write_file",
                            arguments='{"path":"first.txt","content":"yes"}',
                        ),
                        call(
                            "second",
                            name="write_file",
                            arguments='{"path":"second.txt","content":"obsolete"}',
                        ),
                    ]
                ),
                reply("done"),
            ]
        )
        run_member_loop(
            member,
            client=client,
            model="fixture",
            hooks=hooks,
            skill_loader=SkillLoader(self.root / "skills"),
        )
        self.assertTrue((self.root / "first.txt").exists())
        self.assertFalse((self.root / "second.txt").exists())
        self.assertEqual(member.user_revision, 1)

    def test_input_between_last_check_and_operation_registration_wins(self):
        client, _ = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "obsolete",
                            name="write_file",
                            arguments='{"path":"old.txt","content":"no"}',
                        )
                    ]
                ),
                reply("updated"),
            ]
        )
        session = self.session(client)
        session.run_user("/usage")
        original = session.runtime_store.accept_operation

        def accept(*args, **kwargs):
            session.submit_user_input("Stop that write")
            return original(*args, **kwargs)

        with patch.object(
            session.runtime_store, "accept_operation", side_effect=accept
        ):
            self.assertEqual(session.run_user("Write output"), "updated")
        self.assertFalse((self.root / "old.txt").exists())
        self.assertFalse(session.recovery_operations())

    def test_target_delivery_rolls_back_with_input_ack(self):
        client, _ = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "first",
                            name="write_file",
                            arguments='{"path":"first.txt","content":"ok"}',
                        )
                    ]
                ),
                reply("done"),
            ]
        )
        session = self.session(client, capabilities=Capabilities({"teams"}))
        session.run_user("/usage")
        member = TeammateRuntime(session.team, "reader", "reader", "Read", False)
        session.team.members["reader"] = member

        def steer(event, output):
            if event.call_id == "first":
                session.submit_user_input("Use revised algorithm", target="reader")

        session.hooks.register_hook("PostToolUse", steer)
        db = session.runtime_store.backend()
        original = db._send

        def send(conn, target, record):
            if record["type"] == "user_steer":
                raise OSError("delivery write failed")
            return original(conn, target, record)

        with (
            patch.object(session, "start"),
            patch.object(db, "_send", side_effect=send),
        ):
            with self.assertRaises(OSError):
                session.run_user("Perform work")
        self.assertEqual(len(session.inputs.pending()), 1)
        self.assertFalse(session.inputs.applied)
        with patch.object(session, "start"):
            session.run_user("continue")
        self.assertFalse(session.inputs.pending())
        records = session.team.bus.claim("reader")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["content"], "Use revised algorithm")
        session.team.bus.acknowledge("reader", records)

    def test_invalid_queued_command_does_not_starve_next_input(self):
        client, _ = fake_client([reply("next request done")])
        session = self.session(client)
        session.run_user("/usage")
        with patch.object(session, "start"):
            failed = session.submit_user_input(
                "/tests approve nonexistent", mode="queued"
            )
            session.submit_user_input("Do next request", mode="queued")
            self.assertTrue(session.process_once())
            self.assertTrue(session.process_once())
        self.assertFalse(session.inputs.pending())
        receipt = session.runtime_store.get("input_receipts", failed["input_id"])
        self.assertEqual(receipt["state"], "failed")
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_cancel_pending_input_does_not_call_model(self):
        client, _ = fake_client([])
        session = self.session(client)
        session.run_user("/usage")
        with patch.object(session, "start"):
            receipt = session.submit_user_input("Do later", mode="queued")
        result = json.loads(session.run_user("/inputs cancel " + receipt["input_id"]))
        self.assertEqual(result["state"], "cancelled")
        self.assertFalse(session.inputs.pending())
        client.chat.completions.create.assert_not_called()

    def test_incomplete_or_refused_summary_keeps_history(self):
        for reason in ("content_filter", None):
            with self.subTest(reason=reason):
                value = {
                    "choices": [
                        {
                            "finish_reason": reason,
                            "message": {"content": "partial summary"},
                        }
                    ]
                }
                client, _ = fake_client([value])
                compactor = ContextCompactor(client, "fixture", self.root)
                history = [
                    {"role": "user", "content": "Keep facts"},
                    {"role": "assistant", "content": "important fact"},
                ]
                with self.assertRaises(CompactionError):
                    compactor.compact_history(history, "Keep facts")
                self.assertEqual(history[1]["content"], "important fact")

    def test_private_replay_is_included_in_input_size(self):
        adapter = self.adapter("anthropic_messages")
        gateway = self.gateway(adapter)
        # Use the same actual context owner as the gateway's preparation.
        owner = gateway._state.ledger.session_id + ":"
        state = save_replay(
            self.root,
            owner,
            binding_key(adapter, "fixture"),
            {
                "blocks": [
                    {"type": "thinking", "thinking": "x" * 90000, "signature": "sig"},
                    {"type": "text", "text": "short"},
                ]
            },
        )
        compactor = ContextCompactor(gateway, "fixture", self.root, context_chars=10000)
        messages = [
            {"role": "user", "content": "continue"},
            {"role": "assistant", "content": "short", "_protocol_state": state},
        ]
        self.assertGreater(compactor.estimate_chars(messages), 90000)
        self.assertEqual(gateway.summary()["calls"], 0)
        with patch.object(
            compactor, "summarize_history", return_value="all facts retained"
        ):
            prepared = compactor.prepare(messages, "continue")
        self.assertLess(compactor.estimate_chars(prepared), 10000)
        (self.root / state["reference"]).write_text("changed")
        with self.assertRaises(ModelProtocolError):
            compactor.estimate_chars(messages)

    def test_native_parse_failure_preserves_known_usage(self):
        server = ProtocolService(
            [
                {
                    "status": "completed",
                    "output": [{"type": "web_search_call"}],
                    "usage": {"input_tokens": 40, "output_tokens": 10},
                }
            ]
        )
        self.addCleanup(server.close)
        gateway = self.gateway(self.adapter(base_url=server.url))
        with self.assertRaises(ModelProtocolError):
            gateway.request(
                model="fixture", messages=[{"role": "user", "content": "Answer"}]
            )
        self.assertEqual(gateway.summary()["known_tokens"], 50)
        self.assertEqual(gateway.summary()["unknown_usage_calls"], 0)

    def test_replay_save_failure_preserves_known_usage(self):
        server = ProtocolService([responses("done")])
        self.addCleanup(server.close)
        gateway = self.gateway(self.adapter(base_url=server.url))
        with patch(
            "homebrew_harness.execution.model_protocol.save_replay",
            side_effect=OSError("disk write failed"),
        ):
            with self.assertRaises(OSError):
                gateway.request(
                    model="fixture",
                    messages=[{"role": "user", "content": "Answer"}],
                    protocol_root=self.root,
                )
        self.assertEqual(gateway.summary()["known_tokens"], 50)
        self.assertEqual(gateway.summary()["unknown_usage_calls"], 0)

    def test_native_transport_errors_block_one_connection(self):
        adapter = self.adapter()
        adapter.client.close()

        def fail(request):
            raise httpx.ReadTimeout("synthetic timeout", request=request)

        adapter.backend.client = httpx.Client(
            base_url=local_endpoint(), transport=httpx.MockTransport(fail)
        )
        gateway = self.gateway(adapter)
        for _ in range(3):
            with self.assertRaises(ProviderError):
                gateway.request(
                    model="fixture", messages=[{"role": "user", "content": "Answer"}]
                )
        with self.assertRaises(ModelGatewayError):
            gateway.request(
                model="fixture", messages=[{"role": "user", "content": "Answer"}]
            )
        self.assertEqual(gateway.summary()["calls"], 3)
        self.assertEqual(gateway.summary()["consecutive_failures"], 3)

    def test_native_parameter_placement_and_thinking_limits(self):
        from homebrew_harness.execution.model_protocol import ModelCapabilities

        small = ModelRequest(
            "fixture", ({"role": "user", "content": "check"},), max_output_tokens=1024
        )
        with self.assertRaises(ModelProtocolError):
            anthropic.prepare(
                small, list(small.messages), {"thinking": True}, ModelCapabilities()
            )
        request = ModelRequest("fixture", small.messages, max_output_tokens=2048)
        body = anthropic.prepare(
            request, list(request.messages), {"thinking": True}, ModelCapabilities()
        )
        self.assertEqual(body["thinking"]["budget_tokens"], 1024)
        adapter = self.adapter(
            "dashscope_generation", options_json='{"temperature":0.2,"top_p":0.8}'
        )
        body = adapter.prepare(request)
        self.assertNotIn("temperature", body)
        self.assertEqual(body["parameters"]["temperature"], 0.2)
        self.assertEqual(body["parameters"]["top_p"], 0.8)

    def test_missing_completion_proof_never_writes(self):
        value = {
            "choices": [
                {
                    "finish_reason": None,
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            call(
                                "unproven",
                                name="write_file",
                                arguments='{"path":"unproven.txt","content":"yes"}',
                            )
                        ],
                    },
                }
            ]
        }
        client, _ = fake_client([value])
        session = self.session(client)
        with self.assertRaises(ModelProtocolError):
            session.run_user("Write output")
        self.assertFalse((self.root / "unproven.txt").exists())

    def test_unsupported_tool_kind_is_not_rewritten_to_function(self):
        value = call(
            "unsupported",
            name="write_file",
            arguments='{"path":"bad.txt","content":"no"}',
        )
        value["type"] = "custom"
        response = {
            "choices": [
                {"finish_reason": "tool_calls", "message": {"tool_calls": [value]}}
            ]
        }
        client, _ = fake_client([response])
        session = self.session(client)
        with self.assertRaises(ModelProtocolError):
            session.run_user("Write output")
        self.assertFalse((self.root / "bad.txt").exists())

    def test_prepared_payload_is_reused_and_unknown_parameters_rejected(self):
        client, _ = fake_client([reply("done")])
        gateway = self.gateway(client)
        compactor = ContextCompactor(gateway, "fixture", self.root)
        messages = [{"role": "user", "content": "Answer"}]
        adapter = gateway._transport()
        with patch.object(adapter, "prepare", wraps=adapter.prepare) as prepare:
            compactor.estimate_chars(messages)
            gateway.request(model="fixture", messages=messages, protocol_root=self.root)
            self.assertEqual(prepare.call_count, 1)
        with self.assertRaises(ValueError):
            gateway.request(
                model="fixture", messages=messages, unsupported_control=True
            )
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_stream_protocol_failure_retains_final_usage(self):
        value = {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [{"type": "web_search_call"}],
                "usage": {"input_tokens": 40, "output_tokens": 10},
            },
        }
        server = ProtocolService([("sse", [value])])
        self.addCleanup(server.close)
        gateway = self.gateway(
            self.adapter(base_url=server.url, options_json='{"stream":true}')
        )
        with self.assertRaises(ModelProtocolError):
            gateway.request(
                model="fixture", messages=[{"role": "user", "content": "Answer"}]
            )
        self.assertEqual(gateway.summary()["known_tokens"], 50)

    def test_partial_stream_usage_is_not_final(self):
        server = ProtocolService(
            [
                (
                    "sse",
                    [
                        {
                            "type": "message_start",
                            "message": {
                                "usage": {"input_tokens": 40, "output_tokens": 1}
                            },
                        },
                        {"type": "error", "error": {"type": "overloaded_error"}},
                    ],
                )
            ]
        )
        self.addCleanup(server.close)
        gateway = self.gateway(
            self.adapter(
                "anthropic_messages",
                base_url=server.url,
                options_json='{"stream":true}',
            )
        )
        with self.assertRaises(ModelProtocolError) as caught:
            gateway.request(
                model="fixture", messages=[{"role": "user", "content": "Answer"}]
            )
        self.assertFalse(caught.exception.model_usage.final)
        self.assertEqual(caught.exception.model_usage.input_tokens, 40)
        self.assertEqual(gateway.summary()["unknown_usage_calls"], 1)

    def test_idle_targeted_input_keeps_original_and_records_member_application(self):
        client, snapshots = fake_client([reply("Coordinated")])
        session = self.session(client, capabilities=Capabilities({"teams"}))
        session.run_user("/usage")
        member = TeammateRuntime(session.team, "reader", "reader", "Read", False)
        session.team.members["reader"] = member
        with patch.object(session, "start"):
            receipt = session.submit_user_input("Review this part", target="reader")
            self.assertTrue(session.process_once())
        self.assertEqual(session.intent.original, "Review this part")
        self.assertTrue(
            any(
                m.get("content") == "For teammate reader: Review this part"
                for m in snapshots[-1]
            )
        )
        saved = session.runtime_store.get("input_receipts", receipt["input_id"])
        self.assertEqual(saved["state"], "delivered")
        member.handle_messages(session.team.bus.claim("reader"))
        saved = session.runtime_store.get("input_receipts", receipt["input_id"])
        self.assertEqual(saved["state"], "applied")
        self.assertEqual(saved["applied_by"], "reader")
