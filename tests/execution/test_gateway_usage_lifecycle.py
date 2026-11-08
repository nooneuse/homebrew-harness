"""Usage is observational; durable dispatch and runtime limits remain effective."""

from homebrew_harness.runtime.capabilities import Capabilities
import argparse
import threading
import time
from unittest.mock import Mock, patch

import homebrew_harness.runtime.loop
from homebrew_harness.runtime.session import HarnessSession
from homebrew_harness.cli_arguments import add_gateway_arguments
from homebrew_harness.execution.model_gateway import (
    Deadline,
    ModelDeadlineExceeded,
    ModelGateway,
    UsageLedger,
    gateway_limits_from_args,
)
from tests.support.clients import fake_client
from tests.execution.test_model_gateway import Fixture, response


class UsageLifecycleTests(Fixture):
    def restored(self, snapshot):
        gateway, client = self.gateway()
        gateway.restore_usage(snapshot)
        return gateway, client

    def test_restore_reads_events_after_checkpoint_without_double_counting(self):
        first, client = self.gateway()
        self.invoke(first)
        snapshot = first.summary()
        client.chat.completions.create.return_value = response(22)
        with first.scope(role="teammate"):
            self.invoke(first)
        client.chat.completions.create.return_value = response(None)
        self.invoke(first)
        first.close()
        restored, transport = self.restored(snapshot)
        usage = restored.summary()
        self.assertEqual(
            (usage["calls"], usage["known_tokens"], usage["unknown_usage_calls"]),
            (3, 42, 1),
        )
        self.assertEqual(usage["by_role"]["teammate"]["known_tokens"], 22)
        restored.close()
        again, _ = self.restored(usage)
        self.assertEqual(again.summary()["known_tokens"], 42)
        self.assertEqual(again.summary()["unknown_usage_calls"], 1)
        transport.chat.completions.create.assert_not_called()

    def interrupted(self, sent):
        first, _ = self.gateway()
        snapshot = first.summary()
        snapshot.update(
            attempted_calls=1,
            calls=int(sent),
            pending_requests=[
                {
                    "request_id": "fixture",
                    "sent": sent,
                    "role": "lead",
                    "purpose": "worker",
                }
            ],
        )
        first.close()
        return self.restored(snapshot)

    def test_interrupted_dispatch_is_unknown_and_finalized_once(self):
        restored, transport = self.interrupted(True)
        usage = restored.summary()
        self.assertEqual(
            (usage["calls"], usage["known_tokens"], usage["unknown_usage_calls"]),
            (1, 0, 1),
        )
        self.assertEqual(usage["pending_requests"], [])
        restored.close()
        again, _ = self.restored(usage)
        self.assertEqual(again.summary()["unknown_usage_calls"], 1)
        transport.chat.completions.create.assert_not_called()

    def test_interrupted_preparation_is_not_unknown_sent_usage(self):
        restored, transport = self.interrupted(False)
        usage = restored.summary()
        self.assertEqual(
            (usage["attempted_calls"], usage["calls"], usage["unknown_usage_calls"]),
            (1, 0, 0),
        )
        transport.chat.completions.create.assert_not_called()

    def slow_ledger(self, event_name):
        ledger = UsageLedger(self.root)
        append = ledger.append
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def record(event):
            if event["event"] == event_name:
                entered.set()
                release.wait(2)
            append(event)

        ledger.append = record
        client, _ = fake_client([])
        client.chat.completions.create = Mock(return_value=response())
        from homebrew_harness.execution.model_gateway import GatewayLimits

        gateway = ModelGateway(
            client, GatewayLimits(request_timeout=0.05), ledger=ledger
        )
        self.addCleanup(gateway.close)
        return gateway, client, entered, release

    def test_disabled_detailed_logging_does_not_delay_model(self):
        gateway, client = self.gateway()
        gateway._state.ledger.enabled = False
        before = time.monotonic()
        self.invoke(gateway)
        self.assertLess(time.monotonic() - before, 0.3)
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_missing_observation_file_preserves_core_counters(self):
        gateway, client = self.gateway()
        self.invoke(gateway)
        snapshot = gateway.summary()
        gateway.close()
        gateway._state.ledger.close(1)
        (self.root / snapshot["ledger"]).unlink()
        restored, _ = self.restored(snapshot)
        self.assertEqual(restored.summary()["calls"], 1)
        self.assertEqual(restored.summary()["known_tokens"], 20)
        self.assertTrue(restored.summary()["observation_incomplete"])

    def test_thread_start_failure_has_no_dispatch_or_unpaired_ledger_result(self):
        gateway, client = self.gateway()
        with patch.object(
            threading.Thread, "start", side_effect=RuntimeError("injected")
        ):
            with self.assertRaises(RuntimeError):
                self.invoke(gateway)
        self.assertEqual(gateway.summary()["inflight"], 0)
        self.assertEqual(gateway.summary()["calls"], 0)
        gateway.close()
        restored, _ = self.restored(gateway.summary())
        self.assertEqual(restored.summary()["unknown_usage_calls"], 0)
        client.chat.completions.create.assert_not_called()

    def test_goal_failed_pre_dispatch_round_is_not_unknown_response_usage(self):
        client, _ = fake_client([])
        session = HarnessSession(
            client,
            "fixture",
            self.root,
            capabilities=Capabilities.full(memory_mode="off"),
        )
        self.addCleanup(lambda: session.wait_closed(5))
        self.addCleanup(session.close)
        with patch.object(
            homebrew_harness.runtime.loop,
            "model_request",
            side_effect=ModelDeadlineExceeded("fixture deadline"),
        ):
            session.run_user("/goal Provide an answer")
        usage = session.goal.status()["progress"]
        self.assertEqual(usage["worker_rounds"], 1)
        self.assertEqual(usage["worker_responses"], 0)
        self.assertNotIn("worker_unknown_responses", usage)
        self.assertEqual(session.gateway.summary()["calls"], 0)

    def test_removed_gateway_cli_flags_and_environment_do_not_set_budgets(self):
        parser = argparse.ArgumentParser()
        add_gateway_arguments(parser)
        for flag in ("--model-token-budget", "--model-call-budget"):
            self.assertNotIn(flag, parser.format_help())
            with self.assertRaises(SystemExit):
                parser.parse_args([flag, "1"])
        with patch.dict(
            "os.environ",
            {
                "HARNESS_MODEL_TOKEN_BUDGET": "invalid",
                "HARNESS_MODEL_CALL_BUDGET": "invalid",
            },
        ):
            limits = gateway_limits_from_args(
                parser.parse_args(["--model-concurrency", "2"])
            )
        self.assertEqual(limits.concurrency, 2)

    def test_provider_quota_failure_stops_until_explicit_host_reset(self):
        class QuotaFailure(Exception):
            status_code = 402

        gateway, client = self.gateway()
        client.chat.completions.create.side_effect = QuotaFailure(
            "insufficient balance"
        )
        with self.assertRaises(QuotaFailure):
            self.invoke(gateway)
        from homebrew_harness.execution.model_gateway import ModelGatewayError

        with self.assertRaises(ModelGatewayError):
            self.invoke(gateway)
        self.assertEqual(client.chat.completions.create.call_count, 1)
        snapshot = gateway.summary()
        gateway.close()
        restored, transport = self.restored(snapshot)
        self.assertTrue(restored.blocked)
        restored.reset_failures()
        self.invoke(restored)
        self.assertEqual(transport.chat.completions.create.call_count, 1)
