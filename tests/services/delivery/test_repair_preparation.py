"""Accepted repair preparation can resume without replaying quality evidence."""
import unittest
from unittest.mock import patch

from homebrew_harness.runtime.errors import boundary_error
from homebrew_harness.services.delivery.runtime import Delivery
from tests.services.delivery import test_flow as fixture
from tests.runtime.test_interaction import eventually

class RepairPreparation(unittest.TestCase):
    setUp = fixture.FlowTests.setUp
    start = fixture.FlowTests.start
    run_git = fixture.FlowTests.run_git
    pending_target = fixture.FlowTests.pending_target

    def test_unapproved_or_started_repair_cannot_use_preparation_retry(self):
        self.peer.ci_failure = True
        service = self.start(auto_ci_fix=False)
        work = service.work.submit('Repair within the original scope.')
        request = eventually(lambda: next((q for q in service.requests() if q.kind == 'ci_repair'), None), 40)
        service.pause('all')
        store = service.runtime.maintenance.delivery.store
        identity = service.store.work(work.id)['changeset_id']
        row = store.update('changesets', identity, state='failed')
        with self.assertRaisesRegex(ValueError, 'accepted decision'):
            service.refresh_delivery(identity, request_id='reject-unapproved-retry')
        self.assertEqual(store.get('changesets', identity)['state'], 'failed')
        store.update('changesets', identity, state='repair_waiting')
        service.decide(request, 'approve', decision_id='accept-current-repair')
        row = store.update('changesets', identity, state='failed')
        with service.store.db.write() as conn:
            current = service.store.work(work.id)
            current['repair_of'] = identity
            service.store.db._put(conn, 'service_work', work.id, current)
        with self.assertRaisesRegex(ValueError, 'has not created a Run'):
            service.refresh_delivery(identity, request_id='reject-started-retry')
        self.assertEqual(store.get('changesets', identity)['quality'], row['quality'])

    def test_failed_accepted_preparation_retries_without_retesting_parent(self):
        self.peer.ci_failure = True
        original = Delivery.repair
        fail = True
        def prepare(delivery, row, work):
            if fail:
                raise boundary_error(ValueError, 'run_prompt_limit', 'Synthetic long Run input.',
                    limit_name='run.prompt_characters', limit_value=128000, observed_value=159160)
            return original(delivery, row, work)
        with patch.object(Delivery, 'repair', prepare):
            service = self.start(auto_ci_fix=False)
            work = service.work.submit('Repair the fixed value.')
            request = eventually(lambda: next((q for q in service.requests() if q.kind == 'ci_repair'), None), 40)
            service.decide(request, 'approve', decision_id='accept-repair')
            row = eventually(lambda: next((c for c in service.records('changesets')['items']
                if c['state'] == 'failed'), None), 30)
            self.assertEqual(row['error_info']['code'], 'run_prompt_limit')
            self.assertEqual(row['error_info']['details']['observed_value'], 159160)
            before = service.runtime.maintenance.store.get('changesets', row['id'])
            old_run = service.store.work(work.id)['run_id']
            service.pause('all')
            fail = False
            retry = service.refresh_delivery(row['id'], request_id='retry-prepare')
            self.assertEqual(retry['state'], 'repair_approved')
            current = service.runtime.maintenance.store.get('changesets', row['id'])
            self.assertEqual(current['quality'], before['quality'])
            self.assertEqual(current.get('validation_history'), before.get('validation_history'))
            self.assertEqual(service.store.work(work.id)['run_id'], old_run)
            self.assertEqual(current['repair_retry_history'][0]['error_info']['code'], 'run_prompt_limit')
            service.resume()
            eventually(lambda: self.pending_target(service), 40)
            self.assertEqual(service.store.work(work.id)['ci_fix_attempts'], 1)
            self.assertNotEqual(service.store.work(work.id)['run_id'], old_run)
            workspace = service.store.work(work.id)['run_workspace']
            self.assertIn(b'src/value.txt', self.run_git('ls-files', cwd=workspace))
            self.run_git('diff', '--cached', '--quiet', 'HEAD', cwd=workspace)
