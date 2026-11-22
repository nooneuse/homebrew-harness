from homebrew_harness.runtime.limits import limits_scope
from dataclasses import replace
"""Reviewer navigation stays bounded, readonly and distinct from approval proof."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from homebrew_harness.security.review_evidence import ReviewEvidence, EvidenceLimitReached
from homebrew_harness.security.data_boundary import register_private_values
from tests.security import test_approval_review as helpers
from tests.security.test_approval_review import verdict
from tests.support.clients import reply, call


class NavigationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve(); self.evidence = ReviewEvidence(self.root)

    def test_file_pages_can_find_the_actual_nested_module(self):
        directory = self.root / 'src' / 'nested'; directory.mkdir(parents=True)
        for name in ('a.py', 'b.py', '中文.py'):
            (directory / name).write_text('value = 1')
        first = self.evidence.navigate({'pattern': 'src/**/*.py', 'limit': 2})
        second = self.evidence.navigate({'pattern': 'src/**/*.py', 'cursor': first['next_cursor'], 'limit': 2})
        self.assertEqual(first['paths'] + second['paths'], ['src/nested/a.py', 'src/nested/b.py', 'src/nested/中文.py'])
        self.assertIsNone(second['next_cursor'])
        self.assertEqual(self.evidence.files, {})

    def test_literal_search_returns_locations_but_requires_read_for_evidence(self):
        (self.root / 'process.py').write_text('first\ncall = os.killpg(pid, sig)\n')
        result = self.evidence.navigate({'text': 'killpg', 'pattern': '*.py'}, search=True)
        self.assertEqual(result['matches'][0]['line'], 2)
        self.assertEqual(result['matches'][0]['path'], 'process.py')
        self.assertEqual(self.evidence.files, {})
        (self.root / 'literal.py').write_text('a.*b\n')
        (self.root / 'other.py').write_text('axxb\n')
        matches = self.evidence.navigate({'text': 'a.*b', 'pattern': '*.py'}, search=True)['matches']
        self.assertEqual([m['path'] for m in matches], ['literal.py'])

    def test_navigation_keeps_sensitive_runtime_and_linked_files_hidden(self):
        (self.root / '.env').write_text('private')
        (self.root / '.harness').mkdir(); (self.root / '.harness/state.txt').write_text('private')
        (self.root / 'public.txt').write_text('public')
        (self.root / 'linked').symlink_to(self.root / 'public.txt')
        result = self.evidence.navigate({'pattern': '**/*'})
        self.assertEqual(result['paths'], ['public.txt'])
        for pattern in ('../*', '.harness/**', str(self.root / '*')):
            self.assertFalse(self.evidence.navigate({'pattern': pattern})['ok'])
        self.assertEqual(self.evidence.catalog([{'path': '.env'}, {'path': '.harness/state.txt'}, {'path': '../outside'}, {'path': 'public.txt'}]), [{'path': 'public.txt', 'observed_sha256': None}])

    def test_search_and_observations_do_not_retain_credentials_or_file_bodies(self):
        secret = 'synthetic-private-query-content'; register_private_values(secret)
        (self.root / 'file').write_text('match ' + secret)
        result = self.evidence.navigate({'text': 'match'}, search=True)
        self.assertNotIn(secret, json.dumps(result))
        record = self.evidence.observation(1, 'search_review_text', {'text': 'match'}, result)
        self.assertEqual(record['result_paths'], ['file'])
        self.assertNotIn('match ', json.dumps(record))
        self.assertNotIn('content', record)

    def test_navigation_has_inventory_shared_byte_and_page_bounds(self):
        for name in ('a', 'b', 'c'):
            (self.root / name).write_text('four')
        with limits_scope({'review_navigation_paths': 2}):
            self.evidence = ReviewEvidence(self.root)
            result = self.evidence.navigate({})
            self.assertTrue(result['truncated']); self.assertEqual(result['stop_reason'], 'inventory_limit')
        with limits_scope({'review_navigation_bytes': 8}):
            self.evidence = ReviewEvidence(self.root)
            result = self.evidence.navigate({'text': 'absent'}, search=True)
            self.assertEqual(result['stop_reason'], 'byte_limit')
            self.assertEqual(self.evidence.navigation_bytes, 8)
        other = ReviewEvidence(self.root)
        with limits_scope({'review_navigation_bytes': 7}):
            other = ReviewEvidence(self.root)
            result = other.navigate({'text': 'absent'}, search=True)
            self.assertEqual(result['stop_reason'], 'byte_limit')
            self.assertEqual(result['navigation_byte_limit'], 7)
        with limits_scope({'review_pages': self.evidence.pages}):
            self.evidence.limits = replace(self.evidence.limits, review_pages=self.evidence.pages)
            with self.assertRaises(EvidenceLimitReached): self.evidence.navigate({})

    def test_old_hint_is_an_observation_not_an_approval_result(self):
        (self.root / 'guide').write_text('current')
        previous = hashlib.sha256(b'old').hexdigest()
        self.evidence.catalog([{'path': 'guide', 'observed_sha256': previous}])
        result = self.evidence.read({'path': 'guide'})
        self.assertTrue(result['differs_from_navigation_hint'])
        self.assertTrue(result['ok'])
        self.assertEqual(self.evidence.verify()['changes'], [])

    def test_invalid_queries_do_not_search(self):
        for args in ({'limit': True}, {'cursor': -1}, {'pattern': ''}, {'other': 'field'}):
            self.assertFalse(self.evidence.navigate(args)['ok'])
        for args in ({}, {'text': ''}, {'text': 1}, {'text': 'x', 'cursor': '0'}):
            self.assertFalse(self.evidence.navigate(args, search=True)['ok'])


class ReviewerNavigation(unittest.TestCase):
    setUp = helpers.ApprovalReviewTests.setUp
    reviewer = helpers.ApprovalReviewTests.reviewer
    request = helpers.ApprovalReviewTests.request
    context = helpers.ApprovalReviewTests.context

    def test_configured_schema_and_evidence_windows_allow_larger_queries(self):
        (self.root/'evidence.txt').write_text('x' * 12000)
        for index in range(120):
            (self.root / ('source-' + str(index) + '.py')).write_text('source')
        answers = [reply(calls=[call('find', name='find_review_files', arguments='{"pattern":"*.py","limit":120}')]),
                   reply(calls=[call('read', name='read_review_file', arguments='{"path":"evidence.txt","limit":12000}')]), verdict()]
        with limits_scope({'review_page_chars': 15000, 'query_results': 150}):
            reviewer, client, payloads = self.reviewer(answers)
            context = self.context()
            self.assertEqual(reviewer.review(self.request(), context, lambda: context, lambda decision, _: decision), 'approve')
        tools = {t['function']['name']: t['function']['parameters']['properties'] for t in client.chat.completions.create.call_args.kwargs['tools']}
        self.assertEqual(tools['read_review_file']['limit']['maximum'], 15000)
        self.assertEqual(tools['find_review_files']['limit']['maximum'], 150)
        find_result = json.loads(next(m['content'] for m in payloads[1] if m.get('role') == 'tool'))
        self.assertEqual(len(find_result['paths']), 120)
        read_result = json.loads(next(m['content'] for m in payloads[2] if m.get('role') == 'tool' and m.get('tool_call_id') == 'read'))
        self.assertTrue(read_result['read_complete'])
        self.assertEqual(len(read_result['content']), 12000)

    def test_find_search_and_read_can_complete_one_native_review(self):
        (self.root / 'process.py').write_text('print("ok")\n')
        answers = [reply(calls=[call('find', name='find_review_files', arguments='{"pattern":"*.py"}')]),
                   reply(calls=[call('search', name='search_review_text', arguments='{"pattern":"*.py","text":"print"}')]),
                   reply(calls=[call('read', name='read_review_file', arguments='{"path":"process.py"}')]), verdict()]
        reviewer, client, payloads = self.reviewer(answers, {'mode': 'auto_review', 'max_turns': 4})
        context = self.context()
        self.assertEqual(reviewer.review(self.request(), context, lambda: context, lambda decision, _: decision), 'approve')
        record = next(iter(reviewer.store.items('approval_reviews').values()))
        self.assertEqual([o['tool'] for o in record['observations']], ['find_review_files', 'search_review_text', 'read_review_file'])
        self.assertEqual([e['path'] for e in record['evidence']], ['process.py'])
        progress = [json.loads(m['content']) for m in payloads[-1] if m.get('name') == 'review_progress']
        self.assertEqual(len(progress), 1); self.assertEqual(progress[0]['remaining_model_turns_including_this'], 1)
        self.assertTrue(reviewer.execution_evidence_current(record['id'], self.root))
        (self.root / 'process.py').write_text('changed')
        self.assertFalse(reviewer.execution_evidence_current(record['id'], self.root))

    def test_failed_review_keeps_successful_and_failed_read_observations(self):
        (self.root / 'guide').write_text('public evidence')
        answers = [reply(calls=[call('good', name='read_review_file', arguments='{"path":"guide"}')]),
                   reply(calls=[call('missing', name='read_review_file', arguments='{"path":"missing"}')])]
        reviewer, _, _ = self.reviewer(answers, {'mode': 'auto_review', 'max_turns': 2})
        context = self.context(); published = []
        reviewer.review(self.request(), context, lambda: context, lambda *args: published.append(args))
        record = next(iter(reviewer.store.items('approval_reviews').values()))
        self.assertEqual(record['state'], 'human_required'); self.assertFalse(published)
        self.assertEqual([o['ok'] for o in record['observations']], [True, False])
        self.assertEqual(record['observations'][1]['error_code'], 'not_found')
        self.assertEqual([e['path'] for e in record['observed_evidence']], ['guide', 'missing'])
        self.assertNotIn('evidence', record)
        self.assertNotIn('public evidence', json.dumps(record))

    def test_unread_search_hit_gets_specific_feedback_and_can_be_read_next(self):
        (self.root / 'script.py').write_text('print("ok")')
        proposed = reply(json.dumps({'decision': 'approve', 'reason': 'Supports the action.', 'evidence_paths': ['script.py']}))
        answers = [reply(calls=[call('search', name='search_review_text', arguments='{"text":"print","pattern":"*.py"}')]),
                   proposed,
                   reply(calls=[call('read', name='read_review_file', arguments='{"path":"script.py"}')]), proposed]
        reviewer, _, payloads = self.reviewer(answers, {'mode': 'auto_review', 'max_turns': 4})
        context = self.context()
        self.assertEqual(reviewer.review(self.request(), context, lambda: context, lambda decision, _: decision), 'approve')
        record = next(iter(reviewer.store.items('approval_reviews').values()))
        correction = next(item['validation'] for item in record['diagnostics'] if item['kind'] == 'result_correction')
        self.assertEqual(correction['code'], 'unread_evidence_paths')
        self.assertEqual(correction['paths'], ['script.py'])
        self.assertIn('Search matches and path hints are navigation', str(payloads[2]))
        self.assertEqual(record['evidence'][0]['path'], 'script.py')

    def test_completed_invalid_verdict_is_not_mislabeled_as_incomplete_output(self):
        reviewer, _, _ = self.reviewer([reply('{"decision":"approve","reason":"ok","authorization":"explicit"}')],
                                       {'mode': 'auto_review', 'max_turns': 1})
        context = self.context()
        reviewer.review(self.request(), context, lambda: context, lambda *args: self.fail('No valid verdict'))
        record = next(iter(reviewer.store.items('approval_reviews').values()))
        correction = next(item for item in record['diagnostics'] if item['kind'] == 'result_correction')
        self.assertEqual(correction['validation']['code'], 'unexpected_fields')
        self.assertNotIn('error_info', correction)


    def test_bad_verdict_field_types_are_corrections_not_runtime_failures(self):
        from homebrew_harness.security.approval_review import review_verdict
        evidence = ReviewEvidence(self.root)
        for value, code in [({'decision': [], 'reason': 'x'}, 'invalid_decision'),
                            ({'decision': 'approve', 'reason': 'x', 'risk': {}}, 'invalid_risk'),
                            ({'decision': 'approve', 'reason': 'x', 'evidence_paths': None}, 'invalid_evidence_paths')]:
            with self.subTest(code=code):
                verdict_value, error = review_verdict(json.dumps(value), evidence)
                self.assertIsNone(verdict_value)
                self.assertEqual(error['code'], code)
