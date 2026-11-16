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

    def test_multiple_ranges_share_one_limit_and_correct_an_oversized_piece(self):
        compactor, calls = self.compactor([reply('S' * 3500), reply('A' * 2100), reply('B' * 2100), reply('C' * 2100)], context_chars=100000)
        summary = compactor.summarize_history([{'role': 'assistant', 'content': 'x' * 110000}], '', '')
        self.assertLessEqual(len(summary), 10000)
        self.assertIn('[All 3 transcript ranges summarized]', summary)
        self.assertEqual(len(calls), 4)
        ranges = [json.loads(c['messages'][-1]['content']) for c in calls]
        self.assertEqual({v['coverage']['chunk'] for v in ranges}, {1, 2, 3})
        limits = [int(re.search(r'hard limit is (\d+)', c['messages'][0]['content'])[1]) for c in (calls[0], calls[2], calls[3])]
        self.assertLessEqual(sum(limits), 10000)
        self.assertTrue(all(limit < 4000 for limit in limits))

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
