"""Short histories retain facts; pressured histories preserve protocol and working state."""

import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from homebrew_harness.context.compaction import ContextCompactor
from tests.context.test_compaction import batch
from tests.support.clients import fake_client, reply
from unittest.mock import patch


class PressureCompactionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.client, _ = fake_client([])
        capture = redirect_stderr(io.StringIO())
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def pressured(self, **limits):
        c = ContextCompactor(self.client, 'fixture', self.root, context_chars=10000,
                             runtime_limits=limits)
        c.intent.update('Keep the accepted requirement.', new_task=True)
        return c, [{'role':'user', 'content':'Keep the accepted requirement.'},
                   {'role':'assistant', 'content':'old source fact ' * 1600}]

    def test_default_automatic_maintenance_compacts_pressure(self):
        from homebrew_harness.runtime.limits import RuntimeLimits
        self.assertTrue(RuntimeLimits().auto_compact)
        self.client, _ = fake_client([reply('Preserved source fact; finish the accepted task.') for _ in range(8)])
        c, history = self.pressured()
        result = c.prepare(history, 'Continue')
        self.assertLess(c.estimate_chars(result), c.estimate_chars(history))
        self.assertTrue(any(m.get('name')=='task_intent' and 'Keep the accepted requirement.' in m['content'] for m in result))
        self.assertGreater(self.client.chat.completions.create.call_count, 0)

    def test_unsuccessful_optional_compaction_keeps_oversized_working_facts(self):
        from homebrew_harness.context.compaction import SummaryCompactionError
        c, history = self.pressured(auto_compact_retry_turns=2)
        with patch.object(c, '_prepare_automatic', side_effect=SummaryCompactionError('invalid summary')) as attempt:
            result = c.prepare(history, 'Continue')
            self.assertTrue(any(m.get('content') == history[1]['content'] for m in result))
            self.assertTrue(any(m.get('name')=='harness_compaction_observation' for m in result))
            c.prepare(result, 'Continue')
            c.prepare(result, 'Continue')
            self.assertEqual(attempt.call_count, 1)
            c.prepare(result, 'Continue')
            self.assertEqual(attempt.call_count, 2)

    def test_explicit_capacity_still_rejects_unsendable_retained_history(self):
        from homebrew_harness.context.compaction import CompactionError
        c, history = self.pressured(hard_context_chars=15000)
        with patch.object(c, '_prepare_automatic', side_effect=CompactionError('invalid summary')):
            with self.assertRaises(CompactionError) as error:
                c.prepare(history, 'Continue')
        self.assertEqual(error.exception.code, 'context_hard_limit')

    def test_transient_summary_failure_does_not_retry_on_every_worker_turn(self):
        from homebrew_harness.execution.model_protocol import ProviderError
        self.client.chat.completions.create.side_effect = ProviderError('server_error',status_code=503)
        c = ContextCompactor(self.client,'fixture',self.root,context_chars=20000,
                             runtime_limits={'auto_compact_retry_turns':2})
        c.intent.update('Keep the accepted requirement.',new_task=True)
        history = [{'role':'user' if n%2==0 else 'assistant','content':'x'*300} for n in range(50)]
        result = c.prepare(history,'Continue')
        before = self.client.chat.completions.create.call_count
        self.assertGreater(before,0)
        c.prepare([*result,{'role':'assistant','content':'A new working fact.'}],'Continue')
        c.prepare([*result,{'role':'assistant','content':'Another working fact.'}],'Continue')
        self.assertEqual(self.client.chat.completions.create.call_count,before)

    def test_provider_quota_and_cancellation_are_not_hidden_as_optional_summary_errors(self):
        from homebrew_harness.execution.model_protocol import ProviderError
        from homebrew_harness.execution.model_gateway import ModelCancelled
        for failure in (ProviderError('quota', code='insufficient_quota', status_code=429), ModelCancelled('cancelled')):
            with self.subTest(type=type(failure).__name__):
                c, history = self.pressured()
                with patch.object(c, '_prepare_automatic', side_effect=failure):
                    with self.assertRaises(type(failure)):
                        c.prepare(history, 'Continue')

    def test_fifty_short_messages_do_not_drop_middle_fact(self):
        c = ContextCompactor(self.client, "fixture", self.root, runtime_limits={"auto_compact": True})
        history = [
            {
                "role": "user" if i % 2 == 0 else "assistant",
                "content": "unique-middle-fact" if i == 20 else "short",
            }
            for i in range(50)
        ]
        prepared = c.prepare(history, "Continue")
        self.assertTrue(any("unique-middle-fact" in m["content"] for m in prepared))
        self.assertEqual(len(prepared), 51)
        self.client.chat.completions.create.assert_not_called()
        self.assertFalse((self.root / ".harness/transcripts").exists())

    def test_pressure_snip_keeps_whole_tool_batches_and_index(self):
        self.client, _ = fake_client([reply("Confirmed files; continue implementation.") for _ in range(8)])
        c = ContextCompactor(
            self.client,
            "fixture",
            self.root,
            context_chars=10000,
            trigger_messages=20,
            target_messages=10,
        runtime_limits={"auto_compact": True})
        history = [{"role": "user", "content": "Continue"}]
        for i in range(15):
            history.extend(batch(str(i), ["x" * 800]))
        prepared = c.snip_compact(history, "Continue")
        c.groups(prepared)
        marker = next(
            m for m in prepared if m["content"] and "Exact tool calls" in m["content"]
        )
        self.assertIn("denials index:", marker["content"])
        self.assertLess(len(prepared), len(history))
        self.assertGreater(self.client.chat.completions.create.call_count, 0)

    def test_tools_contribute_to_pressure_without_changing_permissions(self):
        self.client, _ = fake_client([reply("Confirmed working facts.") for _ in range(8)])
        c = ContextCompactor(
            self.client,
            "fixture",
            self.root,
            context_chars=25000,
            trigger_messages=20,
            target_messages=10,
        runtime_limits={"auto_compact": True})
        history = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": "message" * 20}
            for i in range(30)
        ]
        self.assertEqual(len(c.snip_compact(history, "Continue")), 31)
        tools = [{"description": "x" * 15000}]
        self.assertLess(
            len(c.snip_compact(history, "Continue", tools=tools)), len(history)
        )
