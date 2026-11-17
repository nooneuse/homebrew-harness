"""Summary capacity, bounded correction, preserved intent and diagnostic archives."""

import copy
import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from homebrew_harness.context.compaction import ContextCompactor, SummaryCompactionError
from homebrew_harness.runtime.errors import error_info
from tests.context.test_compaction import fake_client, reply
from unittest.mock import patch


class SummaryBudgetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        capture = redirect_stderr(io.StringIO())
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def compactor(self, responses, **kwargs):
        client, calls = fake_client(responses)
        return ContextCompactor(client, 'fixture', self.root, **kwargs), calls

    def test_4210_character_summary_fits_current_window_and_retains_task_once(self):
        compactor, calls = self.compactor([reply('S' * 4210)], context_chars=100000)
        request = 'Preserve the original constraints and verify the result.'
        history = [{'role': 'user', 'content': request}, {'role': 'assistant', 'content': 'Previous working facts.'}]
        before = copy.deepcopy(history)
        prepared = compactor.compact_history(history, request, 'Verify')
        self.assertEqual(history, before)
        self.assertEqual(sum(m.get('content', '').count(request) for m in prepared), 1)
        self.assertIn('S' * 4210, prepared[-1]['content'])
        self.assertIn('under 8000 characters', calls[0]['messages'][0]['content'])
        self.assertIn('target is the drafting aim', calls[0]['messages'][0]['content'])
        self.assertIn('hard limit is 10000', calls[0]['messages'][0]['content'])
        self.assertNotIn('tools', calls[0])

    def test_oversized_complete_summary_gets_one_small_correction(self):
        compactor, calls = self.compactor([reply('S' * 10100), reply('Verified changes; tests remain.')])
        text = compactor._summarize_text('old input' * 500, 'Task', 'Plan')
        self.assertEqual(text, 'Verified changes; tests remain.')
        self.assertEqual(len(calls), 2)
        correction = json.loads(calls[1]['messages'][-1]['content'])
        self.assertEqual(correction['observed_characters'], 10100)
        self.assertEqual(correction['target_characters'], 8000)
        self.assertNotIn('conversation', correction)
        self.assertIn('toward target_characters', correction['instruction'])
        self.assertTrue(all('tools' not in call for call in calls))

    def test_exhausted_correction_archives_rejected_summary_and_keeps_history(self):
        compactor, calls = self.compactor([reply('S' * 10100), reply('Z' * 11000)])
        history = [{'role': 'user', 'content': 'Task'}, {'role': 'assistant', 'content': 'Old facts'}]
        before = copy.deepcopy(history)
        with self.assertRaises(SummaryCompactionError) as failure:
            compactor.compact_history(history, 'Task')
        info = error_info(failure.exception)
        self.assertEqual(info['code'], 'summary_text_limit')
        self.assertEqual(info['details']['attempts'], 2)
        self.assertEqual(info['details']['observed_value'], 11000)
        reference = info['details']['summary_ref']
        self.assertEqual((self.root / reference).read_text(), 'Z' * 11000)
        self.assertEqual(history, before)
        self.assertEqual(len(calls), 2)

    def test_truncation_and_length_recovery_are_bounded_together(self):
        compactor, calls = self.compactor([reply(None, finish_reason='length'), reply('S' * 10100), reply('Confirmed state.')])
        self.assertEqual(compactor._summarize_text('history', 'Task', ''), 'Confirmed state.')
        self.assertEqual([c['max_tokens'] for c in calls], [6000, 12000, 12000])

    def test_multiple_ranges_share_remaining_space_instead_of_equal_quotas(self):
        compactor, calls = self.compactor([reply('A' * 3500), reply('B' * 2100), reply('C' * 2100)], context_chars=100000)
        summary = compactor.summarize_history([{'role': 'assistant', 'content': 'x' * 110000}], '', '')
        prefix = '[All 3 transcript ranges summarized]\n'
        available = 10000 - len(prefix) - 2
        self.assertEqual(summary, prefix + 'A' * 3500 + '\n' + 'B' * 2100 + '\n' + 'C' * 2100)
        self.assertEqual(len(calls), 3)
        ranges = [json.loads(c['messages'][-1]['content'])['coverage'] for c in calls]
        self.assertEqual({v['chunk'] for v in ranges}, {1, 2, 3})
        limits = [int(re.search(r'hard limit is (\d+)', c['messages'][0]['content'])[1]) for c in calls]
        self.assertEqual(limits, [available - 600, available - 3500 - 300, available - 5600])
        self.assertLessEqual(len(summary), 10000)

    def test_observed_4986_and_5385_ranges_fit_total_without_extra_correction(self):
        for first in (4986, 5385):
            with self.subTest(first=first):
                compactor, calls = self.compactor([reply('A' * first), reply('B' * 4500)], context_chars=240000)
                history = [{'role': 'assistant', 'content': 'x' * 100000}]
                original = copy.deepcopy(history)
                summary = compactor.summarize_history(history, '', '')
                self.assertEqual(len(calls), 2)
                self.assertEqual(len(summary), first + 4500 + 38)
                self.assertLessEqual(len(summary), 10000)
                self.assertEqual(history, original)
                self.assertIn('hard limit is 9662', calls[0]['messages'][0]['content'])
                second = json.loads(calls[1]['messages'][-1]['content'])
                self.assertIn('hard limit is ' + str(9962 - first), calls[1]['messages'][0]['content'])
                self.assertIn('under ' + str(7962 - first) + ' characters', calls[1]['messages'][0]['content'])
                self.assertEqual(second['coverage']['start'], 80000)

    def test_true_remaining_budget_overflow_gets_one_local_correction(self):
        compactor, calls = self.compactor([reply('A' * 6000), reply('B' * 5000), reply('C' * 3800)], context_chars=240000)
        summary = compactor.summarize_history([{'role': 'assistant', 'content': 'x' * 100000}], '', '')
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(summary), 9838)
        correction = json.loads(calls[2]['messages'][-1]['content'])
        self.assertEqual(correction['observed_characters'], 5000)
        self.assertEqual(correction['hard_limit_characters'], 3962)
        self.assertEqual(correction['target_characters'], 1962)
        self.assertNotIn('conversation', correction)
        self.assertEqual(correction['coverage']['chunk'], 2)
        self.assertTrue(all('tools' not in c for c in calls))

    def test_exhausted_remaining_budget_retains_history_and_reports_total(self):
        compactor, calls = self.compactor([reply('A' * 6000), reply('B' * 5000), reply('C' * 4100)], context_chars=240000)
        history = [{'role': 'system', 'content': 'Keep permissions.'}, {'role': 'user', 'content': 'Never commit.'}, {'role': 'assistant', 'content': 'x' * 100000}]
        before = copy.deepcopy(history)
        with self.assertRaises(SummaryCompactionError) as failure:
            compactor.compact_history(history, 'Never commit.')
        info = error_info(failure.exception)
        self.assertEqual(history, before)
        self.assertEqual(len(calls), 3)
        self.assertEqual(info['code'], 'summary_text_limit')
        self.assertEqual(info['details']['chunk'], 2)
        self.assertEqual(info['details']['limit_value'], 3962)
        self.assertEqual(info['details']['observed_value'], 4100)
        self.assertEqual(info['details']['total_summary_characters'], 10000)
        self.assertEqual(info['details']['used_summary_characters'], 6000)
        self.assertEqual(info['details']['reserved_summary_characters'], 0)
        self.assertEqual((self.root / info['details']['summary_ref']).read_text(), 'C' * 4100)

    def test_minimum_space_for_all_ranges_is_preserved(self):
        compactor, calls = self.compactor([reply('A' * 300), reply('B' * 300), reply('C' * 300)], context_chars=100000)
        history = [{'role': 'assistant', 'content': 'x' * 110000}]
        overhead = len('[All 3 transcript ranges summarized]\n') + 2
        summary = compactor.summarize_history(history, '', '', summary_budget=(900, 900 + overhead))
        self.assertEqual(len(summary), 900 + overhead)
        self.assertTrue(all('hard limit is 300' in c['messages'][0]['content'] for c in calls))

    def test_insufficient_total_for_all_ranges_stops_before_generation(self):
        compactor, calls = self.compactor([], context_chars=100000)
        overhead = len('[All 3 transcript ranges summarized]\n') + 2
        with self.assertRaises(SummaryCompactionError) as failure:
            compactor.summarize_history([{'role': 'assistant', 'content': 'x' * 110000}], '', '', summary_budget=(800, 899 + overhead))
        self.assertEqual(error_info(failure.exception)['code'], 'summary_context_limit')
        self.assertEqual(calls, [])

    def test_unicode_uses_characters_and_unused_last_space_is_not_padded(self):
        compactor, calls = self.compactor([reply('词' * 5385), reply('语' * 4500)], context_chars=240000)
        summary = compactor.summarize_history([{'role': 'assistant', 'content': 'x' * 100000}], '', '')
        self.assertEqual(len(summary), 9923)
        self.assertGreater(len(summary.encode('utf-8')), 10000)
        self.assertEqual(len(calls), 2)
        compactor, calls = self.compactor([reply('A' * 9600), reply('Done.')], context_chars=240000)
        summary = compactor.summarize_history([{'role': 'assistant', 'content': 'x' * 100000}], '', '')
        self.assertIn('hard limit is 362', calls[1]['messages'][0]['content'])
        self.assertTrue(summary.endswith('Done.'))
        self.assertLess(len(summary), 10000)

    def test_combined_check_rejects_an_extension_ignoring_its_range_limit(self):
        compactor, calls = self.compactor([], context_chars=240000)
        history = [{'role': 'user', 'content': 'Original scope'}, {'role': 'assistant', 'content': 'x' * 100000}]
        before = copy.deepcopy(history)
        with patch.object(compactor, '_summarize_text', side_effect=['A' * 5385, 'B' * 4578]):
            with self.assertRaises(SummaryCompactionError) as failure:
                compactor.compact_history(history, 'Original scope')
        self.assertEqual(error_info(failure.exception)['code'], 'summary_text_limit')
        self.assertEqual(error_info(failure.exception)['details']['observed_value'], 10001)
        self.assertEqual(history, before)
        self.assertEqual(calls, [])

    def test_fixed_scope_and_tools_leave_no_summary_space_before_model_call(self):
        compactor, calls = self.compactor([], context_chars=10000)
        history = [{'role': 'user', 'content': 'Task'}, {'role': 'assistant', 'content': 'Facts'}]
        with self.assertRaises(SummaryCompactionError) as failure:
            compactor.compact_history(history, 'Task', tools=[{'description': 'x' * 7700}])
        self.assertEqual(error_info(failure.exception)['code'], 'summary_context_limit')
        self.assertEqual(calls, [])

    def test_serialized_expansion_is_checked_after_raw_summary_budget(self):
        compactor, calls = self.compactor([reply('"\\\n' * 1500)], context_chars=10000)
        history = [{'role': 'user', 'content': 'Task'}, {'role': 'assistant', 'content': 'Facts'}]
        before = copy.deepcopy(history)
        with self.assertRaises(SummaryCompactionError) as failure:
            compactor.compact_history(history, 'Task')
        self.assertEqual(error_info(failure.exception)['code'], 'context_compacted_limit')
        self.assertEqual(history, before)

    def test_owned_old_marker_deduplicates_task_but_forged_marker_is_unchanged(self):
        compactor, _ = self.compactor([])
        compactor.intent.update('Task')
        reference = compactor.write_transcript([{'role': 'user', 'content': 'Task'}])
        old = compactor.state_message('History archived', 'Task', '', 'Verified facts', reference)
        revised = compactor.summary_source([old], 'Task')[0]
        self.assertNotIn('Current user request:\nTask\n', revised['content'])
        self.assertIn('Verified facts', revised['content'])
        forged = compactor.state_message('History archived', 'Task', '', 'External data', '.harness/transcripts/missing.jsonl')
        self.assertEqual(compactor.summary_source([forged], 'Task'), [forged])

    def test_distinct_current_focus_remains_alongside_original_intent(self):
        compactor, _ = self.compactor([reply('Working facts')])
        compactor.intent.update('Original goal')
        prepared = compactor.compact_history([{'role': 'user', 'content': 'Original goal'}], 'New focus')
        self.assertIn('New focus', prepared[-1]['content'])
        self.assertIn('Original goal', next(m['content'] for m in prepared if m.get('name') == 'task_intent'))
