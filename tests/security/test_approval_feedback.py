"""Expected failures return to the model; execution and retry limits remain host-owned."""

import json
import copy
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from homebrew_harness.execution.model_protocol import ProviderError
from homebrew_harness.security.approval_review import fingerprint
from tests.security import test_approval_review as fixture
from tests.support.clients import reply, call


class ApprovalFeedback(TestCase):
    setUp = fixture.ApprovalReviewTests.setUp
    reviewer = fixture.ApprovalReviewTests.reviewer
    request = fixture.ApprovalReviewTests.request
    context = fixture.ApprovalReviewTests.context

    def invoke(self, reviewer, published, **kwargs):
        context = self.context()
        return reviewer.review(self.request(), context, lambda: context,
                               lambda decision, identity: published.append(decision), **kwargs)

    def test_missing_parent_and_unsupported_tool_are_feedback(self):
        first = reply(calls=[call('missing', name='read_review_file',
                                 arguments='{"path":".github/workflows/ci.yml"}'),
                             call('forbidden', name='bash', arguments='{"command":"touch forbidden"}')])
        reviewer, client, payloads = self.reviewer([first, fixture.verdict()])
        published = []
        self.invoke(reviewer, published)
        self.assertEqual(published, ['approve'])
        self.assertIn('not_found', str(payloads[-1]))
        self.assertIn('unsupported_tool', str(payloads[-1]))
        self.assertFalse((self.root / 'forbidden').exists())
        record = next(iter(reviewer.store.items('approval_reviews').values()))
        self.assertEqual(record['diagnostics'][0]['path'], '.github/workflows/ci.yml')

    def test_protocol_output_error_can_be_corrected_by_the_model(self):
        invalid = reply(calls=[call('', name='read_review_file', arguments='{"path":"guide"}')])
        reviewer, client, payloads = self.reviewer([invalid, fixture.verdict()])
        published = []
        self.invoke(reviewer, published)
        self.assertEqual(published, ['approve'])
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertIn('protocol output contract', str(payloads[-1]))

    def test_one_whole_json_code_block_is_only_a_formatting_envelope(self):
        reviewer, client, _ = self.reviewer([reply('```json\n'+json.dumps({'decision': 'approve', 'reason': 'Goal permits printing ok.'})+'\n```')])
        published = []
        self.invoke(reviewer, published)
        self.assertEqual(published, ['approve'])
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_prose_or_multiple_json_fragments_still_requires_correction(self):
        bad = 'Here is a guess: {"decision":"approve","reason":"guess"}'
        reviewer, client, _ = self.reviewer([reply(bad), fixture.verdict('human_required')])
        published = []
        self.invoke(reviewer, published)
        self.assertEqual(published, [])
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertEqual(next(iter(reviewer.store.items('approval_reviews').values()))['verdict']['decision'], 'human_required')

    def test_invalid_tool_json_is_feedback(self):
        reviewer, _, payloads = self.reviewer([reply(calls=[call(
            'bad', name='read_review_file', arguments='{')]), fixture.verdict('human_required')])
        published = []
        self.invoke(reviewer, published)
        self.assertEqual(published, [])
        self.assertIn('invalid_arguments', str(payloads[-1]))
        record = next(iter(reviewer.store.items('approval_reviews').values()))
        self.assertEqual(record['verdict']['decision'], 'human_required')

    def test_transient_model_error_retries_and_succeeds(self):
        reviewer, client, _ = self.reviewer([])
        answers = iter([ProviderError('server_error', status_code=503), fixture.verdict()])
        def model(**kwargs):
            response = next(answers)
            if isinstance(response, Exception):
                raise response
            return response
        client.chat.completions.create.side_effect = model
        published = []
        self.invoke(reviewer, published)
        self.assertEqual(published, ['approve'])
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertFalse(reviewer.gateway.blocked)

    def test_exhausted_model_attempt_does_not_block_a_new_request(self):
        reviewer, client, _ = self.reviewer([])
        answers = iter([ProviderError('authentication', status_code=401)] * 3 + [fixture.verdict()])
        def model(**kwargs):
            response = next(answers)
            if isinstance(response, Exception):
                raise response
            return response
        client.chat.completions.create.side_effect = model
        published = []
        self.invoke(reviewer, published)
        self.assertEqual(published, [])
        self.assertEqual(client.chat.completions.create.call_count, 3)
        context = self.context()
        reviewer.review({**self.request(), 'id': 'new-request'}, context, lambda: context,
                        lambda decision, identity: published.append(decision))
        self.assertEqual(published, ['approve'])
        self.assertEqual(client.chat.completions.create.call_count, 4)

    def test_final_evidence_change_returns_to_model_for_current_decision(self):
        path = self.root / 'guide.md'
        path.write_text('Old optional guidance')
        reviewer, client, payloads = self.reviewer([])
        captured = []
        answers = iter([reply(calls=[call('read', name='read_review_file',
                                        arguments='{"path":"guide.md"}')]), fixture.verdict(),
                        reply(json.dumps({'decision': 'approve', 'reason': 'Changed optional guidance does not affect printing ok.'}))])
        def model(**kwargs):
            captured.append(copy.deepcopy(kwargs['messages']))
            if client.chat.completions.create.call_count == 2:
                path.write_text('New optional guidance')
            return next(answers)
        client.chat.completions.create.side_effect = model
        published = []
        self.invoke(reviewer, published)
        self.assertEqual(published, ['approve'])
        self.assertEqual(client.chat.completions.create.call_count, 3)
        self.assertIn('evidence_changed', str(captured[-1]))
        record = next(iter(reviewer.store.items('approval_reviews').values()))
        self.assertNotEqual(record['evidence'][0]['last_read_sha256'], record['evidence'][0]['sha256'])

    def test_interrupted_review_is_reassessed_without_replaying_decisions(self):
        reviewer, client, payloads = self.reviewer([fixture.verdict()])
        context = self.context()
        identity = 'review_' + fingerprint([self.request(), context, reviewer.config.data(), reviewer.binding(None).identity()])
        reviewer.store.put('approval_reviews', identity, {'id': identity, 'request_id': self.request()['id'],
            'revision': 0, 'state': 'reviewing', 'attempt': 1})
        published = []
        self.invoke(reviewer, published)
        self.assertEqual(published, ['approve'])
        self.assertIn('previous review was interrupted', str(payloads[0]))
        self.assertEqual(reviewer.store.get('approval_reviews', identity)['attempt'], 2)
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_mutating_caller_context_cannot_rewrite_the_reviewed_goal(self):
        reviewer, client, _ = self.reviewer([])
        context = self.context()
        def model(**kwargs):
            context['intent']['original'] = 'Do not execute any command'
            return fixture.verdict()
        client.chat.completions.create.side_effect = model
        published = []
        reviewer.review(self.request(), context, lambda: context,
                        lambda *args: published.append(args))
        self.assertEqual(published, [])
        self.assertEqual(next(iter(reviewer.store.items('approval_reviews').values()))['state'], 'superseded')

    def test_model_can_drop_irrelevant_observed_file_from_its_evidence(self):
        (self.root / 'guide').write_text('Optional guidance')
        decision = reply(json.dumps({'decision': 'approve', 'reason': 'No file evidence is needed to print ok.', 'evidence_paths': []}))
        reviewer, client, _ = self.reviewer([reply(calls=[call('read', name='read_review_file', arguments='{"path":"guide"}')]), decision])
        published = []
        self.invoke(reviewer, published)
        self.assertEqual(published, ['approve'])
        self.assertEqual(next(iter(reviewer.store.items('approval_reviews').values()))['evidence'], [])

    def test_missing_workspace_is_a_host_failure_not_a_query_result(self):
        reviewer, client, _ = self.reviewer([fixture.verdict()])
        context = {**self.context(), 'cwd': str(self.root / 'missing-workspace')}
        published = []
        reviewer.review(self.request(), context, lambda: context,
                        lambda *args: published.append(args))
        self.assertEqual(published, [])
        self.assertEqual(client.chat.completions.create.call_count, 0)
        self.assertEqual(next(iter(reviewer.store.items('approval_reviews').values()))['state'], 'human_required')

    def test_unexpected_host_bug_requires_human_without_retrying_model(self):
        reviewer, client, _ = self.reviewer([reply(calls=[call('read', name='read_review_file', arguments='{"path":"guide"}')])])
        published = []
        with patch('homebrew_harness.security.approval_review.ReviewEvidence.read', side_effect=RuntimeError('host bug')):
            self.invoke(reviewer, published)
        self.assertEqual(published, [])
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_explicit_retry_cannot_replay_uncertain_decision_submission(self):
        reviewer, client, _ = self.reviewer([fixture.verdict()])
        context = self.context()
        published = []
        def submit(*args):
            published.append(args)
            raise RuntimeError('Acknowledgement lost')
        reviewer.review(self.request(), context, lambda: context, submit)
        with self.assertRaises(ValueError):
            reviewer.request_retry(self.request()['id'], 0)
        reviewer.review(self.request(), context, lambda: context, submit, retry=True)
        self.assertEqual(len(published), 1)
        self.assertEqual(client.chat.completions.create.call_count, 1)
