"""An explicit HTTP/SDK retry restarts review without granting publication authority."""

import unittest
from homebrew_harness.platforms.contracts import ObjectRef, SourceEvent
from homebrew_harness.security.approval_review import ApprovalReviewer, ReviewConfig
from tests.services.communication import test_flow as fixture


class ReviewRetry(unittest.TestCase):
    setUp = fixture.CommunicationFlow.setUp
    start = fixture.CommunicationFlow.start

    def test_retry_uses_native_command_transaction_and_keeps_request_pending(self):
        service = self.start()
        service.pause('all')
        store = service.runtime.maintenance.communication.store
        ref = ObjectRef('github', 'primary', 'example/project', 'issue', '1', 1)
        store.ingest('project', [SourceEvent(ref, ref, '1', {'body': 'A feature', 'actor_id': 11})], ref.scope())
        row = store.get('discussions', service.discussions()['items'][0]['id'])
        store.queue_round(row, force=True)
        job = store.ms.claim_job()
        store.ms.finish(job, {'next_step': 'wait', 'reason': 'Scope recorded.', 'alignment': 'Fits.',
                             'communication': {'kind': 'post_comment', 'intent': 'followup', 'body': 'Scope recorded.'}})
        request = next(r for r in service.requests() if r.kind == 'communication_authority')
        controller = service.runtime.approval_review
        controller.config = ReviewConfig(mode='auto_review', kinds=('communication_authority',))
        agent = service.runtime.maintenance.agent
        controller.reviewer = ApprovalReviewer(controller.config, service.runtime.store.runtime,
            agent.models.registry, agent.gateway, lambda: agent.base_binding)
        service.runtime.store.runtime.put('approval_reviews', 'failed-review', {
            'id': 'failed-review', 'request_id': request.ref.id, 'revision': request.ref.revision,
            'state': 'human_required', 'attempt': 1, 'checkpoint': 'service:'+service.id})
        result = service.retry_approval_review(request, request_id='retry-once')
        self.assertFalse(result['decision_granted'])
        self.assertEqual(result['reviews'], ['failed-review'])
        self.assertEqual(service.retry_approval_review(request, request_id='retry-once'), result)
        self.assertEqual(service.runtime.store.runtime.get('approval_reviews', 'failed-review')['state'], 'retry_requested')
        self.assertIn(request.ref.id, [r.ref.id for r in service.requests()])
        self.assertEqual(self.peer.writes, [])
