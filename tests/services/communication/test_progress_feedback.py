"""Discussion history informs the model without replacing its chosen next step."""

import unittest
from homebrew_harness.platforms.contracts import ObjectRef, SourceEvent
from tests.services.communication import test_flow as fixture


class DiscussionProgress(unittest.TestCase):
    setUp = fixture.CommunicationFlow.setUp
    start = fixture.CommunicationFlow.start

    def test_repeated_no_progress_does_not_force_operator_or_hide_new_input(self):
        service = self.start(no_progress_rounds=2)
        service.pause('all')
        service.runtime.maintenance.stop.set()
        self.assertTrue(service.runtime.maintenance.finished.wait(5))
        store = service.runtime.maintenance.communication.store
        ref = ObjectRef('github', 'primary', 'example/project', 'issue', '1', 1)
        for iteration in range(4):
            item = SourceEvent(ObjectRef('github', 'primary', 'example/project', 'comment', str(iteration)),
                ref, str(iteration), {'body': 'Clarification round '+str(iteration), 'actor_id': 11})
            store.ingest('project', [item], ref.scope())
            row = store.get('discussions', service.discussions()['items'][0]['id'])
            store.queue_round(row, force=True)
            job = store.ms.claim_job()
            self.assertEqual(job['input']['no_progress'], iteration)
            store.ms.finish(job, {'next_step': 'wait', 'reason': 'Await another useful clarification.',
                                 'alignment': 'Fits policy.', 'progress': False})
            row = store.get('discussions', row['id'])
            self.assertEqual(row['state'], 'waiting_participant')
            self.assertEqual(row['no_progress'], iteration + 1)
        self.assertEqual(service.requests(), ())
