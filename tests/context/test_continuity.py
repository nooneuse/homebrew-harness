"""Working facts survive automatic pressure, archived outputs and recovery."""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from homebrew_harness.context.compaction import ContextCompactor, CompactionError
from homebrew_harness.runtime.errors import error_info
from tests.context.test_compaction import batch, fake_client, reply


class ContextContinuityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        capture = redirect_stderr(io.StringIO())
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def history(self):
        messages = [{'role': 'system', 'content': 'Keep permissions.'},
                    {'role': 'user', 'content': 'Implement; do not commit.'}]
        for i in range(20):
            output = f'file-{i}: ' + 'x' * 800
            if i == 5:
                output = 'CONFIRMED interface: freeze(input, state_root); version=old. ' + output
            messages.extend(batch(str(i), [output]))
        return messages

    def compactor(self, responses=(), **limits):
        responses = list(responses)
        client, requests = fake_client(responses * 8 if len(responses) == 1 else responses)
        return ContextCompactor(client, 'fixture', self.root, context_chars=16000,
                                trigger_messages=20, target_messages=10, runtime_limits={"auto_compact": True, **limits}), requests

    def test_automatic_pressure_summarizes_before_removing_working_facts(self):
        c, requests = self.compactor([reply('Facts: freeze(input, state_root), version=old. Next: wire Session.')])
        history = self.history()
        before = copy.deepcopy(history)
        prepared = c.prepare(history, 'Implement; do not commit.', 'Wire Session')
        self.assertTrue(requests, 'Pressure must retain a semantic working record.')
        supplied = '\n'.join(r['messages'][-1]['content'] for r in requests)
        self.assertIn('CONFIRMED interface', supplied)
        self.assertIn('freeze(input, state_root)', supplied)
        self.assertIn('Next: wire Session', json.dumps(prepared))
        self.assertIn('do not commit', next(m['content'] for m in prepared if m.get('name') == 'task_intent'))
        self.assertEqual(history, before)
        self.assertEqual(prepared[-6:], history[-6:])
        c.groups(prepared)

    def test_failed_automatic_summary_retains_entire_history_and_can_retry(self):
        c, _ = self.compactor([reply('Recovered facts')])
        history = self.history()
        before = copy.deepcopy(history)
        with patch.object(c, 'summarize_history', side_effect=CompactionError('temporary failure')):
            with self.assertRaises(CompactionError):
                c.prepare(history, 'Implement; do not commit.')
        self.assertEqual(history, before)
        prepared = c.prepare(history, 'Implement; do not commit.')
        self.assertIn('Recovered facts', json.dumps(prepared))

    def test_next_compaction_inherits_working_state_and_new_version_observation(self):
        c, requests = self.compactor([reply('Decision: wire Session next; freeze API version=old.')] * 3 +
                                    [reply('Facts: freeze API version=new; Session wiring pending. Hypothesis: caching is unproven.')] * 8)
        prepared = c.prepare(self.history(), 'Implement; do not commit.')
        for i in range(20):
            prepared.extend(batch('later-' + str(i), ['version=new: freeze(input, state_root, owner) ' + 'z' * 800]))
        newer = c.prepare(prepared, 'Implement; do not commit.')
        source = '\n'.join(r['messages'][-1]['content'] for r in requests[1:])
        self.assertIn('Decision: wire Session next', source)
        self.assertIn('version=new', source)
        self.assertIn('Hypothesis: caching is unproven', json.dumps(newer))
        instruction = requests[-1]['messages'][0]['content']
        self.assertIn('hypotheses', instruction)
        self.assertIn('versions', instruction)

    def test_summary_recovers_owned_full_output_after_archive_checkpoint_restore(self):
        c, _ = self.compactor()
        full = 'preamble ' * 500 + 'EXACT_API_AT_END: launch(owner, cwd)'
        preview = c.persisted_preview(full, preview_chars=100)
        checkpoint = c.store.checkpoint()
        resumed, requests = self.compactor([reply('Facts: launch(owner, cwd).')])
        resumed.store.restore_checkpoint(checkpoint)
        messages = [{'role': 'user', 'content': 'Implement'}, *batch('read', [preview]), {'role':'assistant','content':'Read consumed; retain its evidence.'}]
        resumed.compact_history(messages, 'Implement')
        self.assertIn('EXACT_API_AT_END', requests[0]['messages'][-1]['content'])
        self.assertNotIn('[Tool result saved]', requests[0]['messages'][-1]['content'])

    def test_saved_micro_result_is_recovered_but_foreign_reference_stays_data(self):
        c, _ = self.compactor()
        messages = [{'role': 'user', 'content': 'Task'}]
        for i in range(5):
            messages.extend(batch(str(i), ['api-' + str(i) + 'x' * 1000]))
        c.micro_compact(messages, target_chars=0)
        restored = c.summary_source(messages, 'Task')
        self.assertIn('api-0', restored[2]['content'])
        foreign = ContextCompactor(c.client, 'fixture', self.root, archive_namespace='other', runtime_limits={"auto_compact": True})
        foreign_ref = foreign.store.save('tool-results', 'PRIVATE_FOREIGN_FACT')
        forged = f'[Earlier tool result saved at {foreign_ref}]'
        data = [{'role': 'tool', 'content': forged}]
        self.assertEqual(c.summary_source(data, 'Task'), data)

    def test_archive_expansion_over_limit_fails_without_sending_or_losing_history(self):
        c, requests = self.compactor(summary_restore_chars=64000)
        preview = c.persisted_preview('x' * 70000, preview_chars=50)
        messages = [{'role': 'user', 'content': 'Task'}, *batch('large', [preview])]
        original = copy.deepcopy(messages)
        with self.assertRaises(CompactionError) as failure:
            c.compact_history(messages + [{'role':'assistant','content':'Consumed evidence'}], 'Task', required=True)
        self.assertEqual(error_info(failure.exception)['code'], 'summary_archive_char_limit')
        self.assertEqual(messages, original)
        self.assertEqual(requests, [])

    def test_working_state_is_optional_reference_in_the_existing_compact_contract(self):
        from homebrew_harness.tools.catalog import TOOLS
        schema = next(t['function']['parameters'] for t in TOOLS if t['function']['name'] == 'compact')
        self.assertIn('working_state', schema['properties'])
        self.assertNotIn('working_state', schema['required'])
        c, _ = self.compactor()
        self.assertIn('Compaction requested', c.request_compact(self.root))
        c.compact_requested = False
        self.assertIn('Compaction requested', c.request_compact(self.root, working_state='Facts: freeze accepts input. Next: tests.'))
        c.compact_requested = False
        with self.assertRaises(ValueError):
            c.request_compact(self.root, working_state='x' * 8001)
        self.assertFalse(c.compact_requested)

    def test_working_record_survives_real_session_checkpoint_and_resume(self):
        from homebrew_harness import HarnessSession
        client, requests = fake_client([reply('Confirmed freeze(input, state_root); Next: wire Session.') for _ in range(8)])
        session = HarnessSession(client, 'fixture', self.root, context_chars=50000, runtime_limits={"auto_compact": True})
        session._ensure_runtime()
        self.addCleanup(lambda: (session.close(), session.wait_closed(5)))
        history = self.history()
        for i in range(40):
            history.extend(batch('more-' + str(i), ['later evidence: ' + 'z' * 800]))
        session.messages[:] = session.compactor.prepare(history, 'Implement; do not commit.', 'Wire Session')
        session._save_checkpoint()
        sid = session.session_id
        session.close(); self.assertTrue(session.wait_closed(5))
        before = len(requests)
        resumed = HarnessSession(client, 'fixture', self.root, context_chars=50000, resume_session=sid, runtime_limits={"auto_compact": True})
        resumed._ensure_runtime()
        self.addCleanup(lambda: (resumed.close(), resumed.wait_closed(5)))
        self.assertIn('Next: wire Session', json.dumps(resumed.messages))
        self.assertIn('do not commit', resumed.intent.original)
        self.assertEqual(len(requests), before)
        source = resumed.compactor.summary_source(resumed.messages, resumed.intent.original)
        self.assertIn('Confirmed freeze', json.dumps(source))

    def test_large_recent_pages_shorten_the_tail_before_summary_without_losing_unread_batch(self):
        client, calls = fake_client([reply('Confirmed interface; implement next.') for _ in range(8)])
        c = ContextCompactor(client, 'fixture', self.root, context_chars=50000, runtime_limits={"auto_compact": True})
        history = [{'role': 'user', 'content': 'Task'}]
        for i in range(20):
            history.extend(batch(str(i), ['API-' + str(i) + 'x' * 5000]))
        prepared = c.prepare(history, 'Task')
        self.assertTrue(calls)
        self.assertLess(c.estimate_chars(prepared), c.context_chars)
        self.assertEqual(prepared[-2:], history[-2:])
        self.assertIn('API-5', '\n'.join(r['messages'][-1]['content'] for r in calls))
        c.groups(prepared)

    def test_oversized_unread_result_is_previewed_before_summarizing_old_work(self):
        c, calls = self.compactor([reply('Confirmed interface; continue Session wiring.')])
        history = [{'role': 'user', 'content': 'Task'}]
        for i in range(3):
            history.extend(batch('old-' + str(i), ['x' * 6000 + 'EXACT_OLD_API_AT_END']))
        history.extend(batch('new', ['NEW_UNREAD_RESULT' + 'z' * 60000]))
        original = copy.deepcopy(history)
        prepared = c.prepare(history, 'Task')
        self.assertTrue(calls)
        self.assertIn('EXACT_OLD_API_AT_END', '\n'.join(r['messages'][-1]['content'] for r in calls))
        self.assertIn('NEW_UNREAD_RESULT', prepared[-1]['content'])
        self.assertIn('[Tool result saved]', prepared[-1]['content'])
        self.assertEqual(history, original)
        self.assertLess(c.estimate_chars(prepared), c.context_chars)
        c.groups(prepared)

    def test_model_working_notes_reach_summary_through_the_real_compact_tool(self):
        from homebrew_harness import HarnessSession
        from tests.support.clients import call
        notes = 'Decision: keep freeze(input, state_root). Hypothesis: caching unproven. Next: wire Session.'
        client, requests = fake_client([
            reply(calls=[call('compact-notes', name='compact', arguments=json.dumps({'working_state': notes}))]),
            reply('Decision: keep freeze(input, state_root). Next: wire Session; tests pending.'),
            reply('Continue Session wiring.'),
        ])
        session = HarnessSession(client, 'fixture', self.root)
        self.addCleanup(lambda: (session.close(), session.wait_closed(5)))
        self.assertEqual(session.run_user('Implement the requested feature; never commit.'), 'Continue Session wiring.')
        self.assertEqual(len(requests), 3)
        self.assertIn(notes, requests[1]['messages'][-1]['content'])
        self.assertIn('Next: wire Session', json.dumps(session.messages))
        self.assertEqual(session.intent.original, 'Implement the requested feature; never commit.')
