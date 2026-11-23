"""Usage is observational; durable dispatch and runtime limits remain effective."""

from homebrew_harness.runtime.capabilities import Capabilities
import argparse
import json
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
    def test_default_recording_retains_provider_categories_and_execution_identity(self):
        from types import SimpleNamespace
        gateway, client = self.gateway()
        answer = response(150, prompt_tokens=100, completion_tokens=50,
                          prompt_cache_hit_tokens=80, prompt_cache_miss_tokens=20,
                          completion_tokens_details=SimpleNamespace(reasoning_tokens=30))
        answer.id = 'provider-fixture-receipt'
        client.chat.completions.create.return_value = answer
        with gateway.scope(role='service', context_id='job_fixture', observations={
            'job_id':'job_fixture', 'work_id':'work_fixture', 'phase':'review', 'profile':'reviewer', 'attempt':2,
        }):
            gateway.request(model='fixture-reviewer', messages=[{'role':'user','content':'PRIVATE_REVIEW_INPUT'}], max_tokens=64)
        gateway._state.ledger.flush(2)
        rows = [json.loads(line) for line in (self.root/gateway.summary()['ledger']).read_text().splitlines()]
        event = next(row for row in rows if row['event']=='request_finished')
        self.assertEqual(event['usage'], {'input_tokens':100,'output_tokens':50,'total_tokens':150,
                         'reasoning_tokens':30,'cache_hit_tokens':80,'cache_miss_tokens':20})
        self.assertEqual(event['provider_id'], 'provider-fixture-receipt')
        self.assertEqual((event['job_id'],event['phase'],event['model']), ('job_fixture','review','fixture-reviewer'))
        self.assertGreater(event['serialized_request_chars'], 0)
        self.assertNotIn('PRIVATE_REVIEW_INPUT', json.dumps(rows))

    def test_session_metadata_recording_is_independent_of_verbose_output(self):
        client, _ = fake_client([])
        client.chat.completions.create = Mock(return_value=response())
        session = HarnessSession(client, 'fixture', self.root, capabilities=Capabilities(details=False))
        self.addCleanup(lambda:(session.close(),session.wait_closed(5)))
        session.run_user('Answer without verbose output.')
        session.gateway._state.ledger.flush(2)
        self.assertTrue(session.gateway.summary()['details_enabled'])
        self.assertTrue((self.root/session.gateway.summary()['ledger']).exists())

    def test_explicit_observation_opt_out_keeps_usage_counters(self):
        client, _ = fake_client([])
        client.chat.completions.create = Mock(return_value=response())
        session = HarnessSession(client, 'fixture', self.root, runtime_limits={'usage_observations':False})
        self.addCleanup(lambda:(session.close(),session.wait_closed(5)))
        session.run_user('Complete this task.')
        self.assertFalse(session.gateway.summary()['details_enabled'])
        self.assertEqual(session.gateway.summary()['known_tokens'],20)
        self.assertFalse((self.root/'.harness/usage').exists())

    def test_observation_capacity_reports_loss_without_failing_the_model(self):
        ledger = UsageLedger(self.root, max_bytes=1)
        client, _ = fake_client([])
        client.chat.completions.create = Mock(return_value=response())
        gateway = ModelGateway(client, ledger=ledger)
        self.addCleanup(gateway.close)
        self.invoke(gateway)
        ledger.flush(2)
        self.assertEqual(gateway.summary()['known_tokens'],20)
        self.assertTrue(gateway.summary()['observation_incomplete'])
        self.assertEqual(gateway.summary()['observation_error_info']['code'],'usage_ledger_limit')
        self.assertEqual(gateway.summary()['observation_error_info']['limit_bytes'],1)

    def test_usage_scope_rejects_payload_fields(self):
        gateway, _ = self.gateway()
        with self.assertRaises(ValueError), gateway.scope(observations={'prompt':'private'}):
            pass

    def test_member_checkpoint_adapter_does_not_need_root_controller_properties(self):
        from types import SimpleNamespace
        from homebrew_harness.storage.durable_runtime import CheckpointWriter, DurableStore, checkpoint_scope
        gateway, _ = self.gateway()
        writer = CheckpointWriter(DurableStore(self.root), 'member:fixture', lambda:{})
        writer.run_control = SimpleNamespace(header={'id':'run_fixture','owner':'owner','generation':1})
        with checkpoint_scope(writer):
            self.invoke(gateway)
        gateway._state.ledger.flush(2)
        rows = [json.loads(line) for line in (self.root/gateway.summary()['ledger']).read_text().splitlines()]
        self.assertEqual(next(r for r in rows if r['event']=='request_finished')['run_id'],'run_fixture')

    def test_close_keeps_late_receipt_and_wait_idle_drains_observation_writes(self):
        from homebrew_harness.execution.model_gateway import ModelCancelled
        gateway, client = self.gateway()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        errors = []
        def model(**kwargs):
            entered.set()
            release.wait(5)
            return response(7)
        client.chat.completions.create.side_effect = model
        def invoke():
            try:
                self.invoke(gateway)
            except ModelCancelled:
                errors.append('cancelled')
        worker = threading.Thread(target=invoke)
        worker.start()
        self.addCleanup(lambda:worker.join(5))
        self.assertTrue(entered.wait(5))
        gateway.close()
        release.set()
        worker.join(5)
        self.assertTrue(gateway.wait_idle(5))
        rows = [json.loads(line) for line in (self.root/gateway.summary()['ledger']).read_text().splitlines()]
        finished = [row for row in rows if row['event']=='request_finished']
        self.assertEqual(len(finished),1)
        self.assertEqual(finished[0]['usage']['total_tokens'],7)
        self.assertTrue(gateway._state.ledger.closed)

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
        with patch(
            "homebrew_harness.execution.model_retry.request",
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

    def test_saved_quota_failure_is_diagnostic_and_next_request_can_succeed(self):
        class QuotaFailure(Exception):
            status_code = 402
        gateway, client = self.gateway()
        client.chat.completions.create.side_effect = QuotaFailure('insufficient balance')
        with self.assertRaises(QuotaFailure):
            self.invoke(gateway)
        snapshot = gateway.summary()
        gateway.close()
        restored, transport = self.restored(snapshot)
        self.assertFalse(restored.blocked)
        self.invoke(restored)
        self.assertEqual(transport.chat.completions.create.call_count, 1)
