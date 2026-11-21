"""Fixed development bases survive target movement; integration is explicit."""

import copy
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from homebrew_harness import RemoteService
from homebrew_harness.cli import parser
from homebrew_harness.contracts import SDKError
from homebrew_harness.services.cli import operate
from tests.runtime.test_interaction import eventually
from tests.services.delivery import test_git, test_merge_flow, test_target_refresh


class FixedGitBaseTests(unittest.TestCase):
    setUp = test_git.GitTests.setUp
    run_git = test_git.GitTests.run_git
    candidate = test_git.GitTests.candidate
    advance_target = test_git.GitTests.advance_target

    def test_default_candidate_uses_frozen_input_despite_conflicting_target(self):
        original = self.baseline['head']
        later = self.advance_target('module.py', 'value=3\n')
        candidate = self.candidate()
        self.assertEqual(self.spec['base_update'], 'manual')
        self.assertEqual(candidate['target_base'], original)
        self.assertEqual(candidate['parent'], original)
        self.assertFalse(candidate['conflicts'])
        self.assertEqual((Path(candidate['workspace']) / 'module.py').read_text(), 'value=2\n')
        self.backend.publish(candidate, 'harness/fixed-base', None)
        self.assertEqual(self.backend.ref('main'), later)
        self.assertEqual(self.backend.ref('harness/fixed-base'), candidate['commit'])


class FixedBaseFlowTests(unittest.TestCase):
    setUp = test_merge_flow.MergeFlowTests.setUp
    start = test_merge_flow.MergeFlowTests.start
    run_git = test_merge_flow.MergeFlowTests.run_git
    ready = test_merge_flow.MergeFlowTests.ready
    manual = test_merge_flow.MergeFlowTests.manual
    row = test_target_refresh.TargetRefreshTests.row
    advance_target = test_target_refresh.TargetRefreshTests.advance_target
    conflict_request = test_target_refresh.TargetRefreshTests.conflict_request

    def assert_fixed(self, before, after):
        for key in ('candidate', 'quality', 'review', 'frozen', 'policy', 'publication'):
            self.assertEqual(after.get(key), before.get(key), key)

    def test_target_change_retains_published_candidate_until_explicit_refresh(self):
        service, work, old = self.manual()
        old = copy.deepcopy(old)
        calls = len(self.peer.model_requests)
        later = self.advance_target('src/value.txt', '3\n')
        self.peer.pulls[0].update(mergeable=False, mergeable_state='dirty')
        waiting = eventually(lambda: self.row(service, work) if self.row(service, work)['state'] == 'integration_required' else None, 15)
        self.assert_fixed(old, waiting)
        self.assertEqual(waiting['error'], 'target_base_changed')
        self.assertIn('explicitly refresh', waiting['error_detail'])
        time.sleep(.3)
        self.assertEqual(len(self.peer.model_requests), calls)
        self.assertIsNone(self.conflict_request(service))
        self.assertEqual(service.store.work(work.id).get('ci_fix_attempts', 0), 0)
        self.assertEqual(service.runtime.maintenance.delivery.git.ref(old['branch']), old['candidate']['commit'])
        self.assertEqual(len(self.peer.pulls), 1)
        self.assertNotEqual(later, waiting['candidate']['target_base'])
        service.stop()
        self.assertTrue(service.wait_closed(15))
        service = self.host.services.start(identity=service.id)
        self.addCleanup(lambda: (service.close(), service.wait_closed(15)))
        self.assert_fixed(old, self.row(service, work))
        time.sleep(.3)
        self.assertEqual(len(self.peer.model_requests), calls)
        service.refresh_delivery(old['id'], request_id='explicitly-authorized-integration')
        request = eventually(lambda: self.conflict_request(service), 30)
        self.assertEqual(request.kind, 'ci_repair')
        refreshed = self.row(service, work)
        self.assertEqual(refreshed['candidate']['target_base'], later)
        self.assertEqual(refreshed['change_request'], old['change_request'])
        self.assertEqual(refreshed['candidate_history'][-1]['frozen'], old['frozen'])
        self.assertIsNone(refreshed['quality'])
        self.assertIsNone(refreshed['review'])

    def test_paused_configuration_preserves_receipts_and_survives_restart(self):
        service, work, old = self.manual(base_update='automatic')
        service.pause('all')
        before = copy.deepcopy(self.row(service, work))
        work_before = copy.deepcopy(service.store.work(work.id))
        with RemoteService.from_root(self.control, control=True) as remote:
            changed = remote.configure_base_updates('manual', request_id='fixed-base-config')
            self.assertEqual(changed, {'base_update': 'manual'})
            args = parser().parse_args(['service', 'delivery', service.id, 'base-updates', 'manual', '--request-id', 'fixed-base-config'])
            self.assertEqual(operate(remote, args), changed)
        self.assert_fixed(before, self.row(service, work))
        self.assertEqual(service.store.work(work.id), work_before)
        self.advance_target('src/value.txt', '3\n')
        service.stop()
        self.assertTrue(service.wait_closed(15))
        resumed = self.host.services.start(identity=service.id)
        self.addCleanup(lambda: (resumed.close(), resumed.wait_closed(15)))
        self.assertEqual(resumed.runtime.spec.delivery['base_update'], 'manual')
        eventually(lambda: self.row(resumed, work)['state'] == 'integration_required', 15)
        self.assert_fixed(before, self.row(resumed, work))
        self.assertIsNone(self.conflict_request(resumed))

    def test_deferred_integration_still_diagnoses_published_head_ci_failure(self):
        service, work, old = self.manual()
        self.advance_target('src/value.txt', '3\n')
        eventually(lambda: self.row(service, work)['state'] == 'integration_required', 15)
        self.peer.ci_failure = True
        request = eventually(lambda: self.conflict_request(service), 25)
        current = self.row(service, work)
        self.assertEqual(request.kind, 'ci_repair')
        self.assertEqual(current['ci']['head'], old['candidate']['commit'])
        self.assertFalse(current['candidate'].get('conflicts'))
        self.assert_fixed(old, current)
        self.assertFalse(current.get('candidate_history'))

    def test_maintainer_merge_verifies_actual_result_without_auto_refresh(self):
        service, work, old = self.manual()
        later = self.advance_target('upstream.txt', 'independent target change\n')
        eventually(lambda: self.row(service, work)['state'] == 'integration_required', 15)
        self.assertEqual(self.row(service, work)['candidate']['target_base'], old['candidate']['target_base'])
        merged = self.peer.merge_pull(1)
        eventually(lambda: work.snapshot().state == 'delivered', 40)
        current = self.row(service, work)
        self.assertEqual(current['merge']['result_commit'], merged)
        self.assertEqual(current['candidate']['target_base'], old['candidate']['target_base'])
        self.assertEqual(current['integration']['target_base'], later)
        self.assertTrue(current['integration_quality']['ok'])
        self.assertFalse(current.get('candidate_history'))
        self.assertFalse(any(kind == 'merge' for kind, _ in self.peer.writes))

    def test_invalid_or_live_configuration_does_not_change_state(self):
        service, work, old = self.manual()
        before = copy.deepcopy(service.store.get(service.id))
        with self.assertRaises(SDKError):
            service.configure_base_updates('automatic', request_id='live-base-change')
        after = service.store.get(service.id)
        self.assertEqual(after['spec'], before['spec'])
        self.assertEqual(after['binding'], before['binding'])

        service.pause('all')
        before = copy.deepcopy(service.store.get(service.id))
        with self.assertRaises(SDKError):
            service.configure_base_updates(True, request_id='invalid-base-change')
        after = service.store.get(service.id)
        self.assertEqual(after['spec'], before['spec'])
        self.assertEqual(after['binding'], before['binding'])

    def test_feedback_cannot_overwrite_a_concurrent_native_refresh(self):
        from homebrew_harness.services.delivery.merge_flow import refresh
        service, work, old = self.manual()
        service.pause('all')
        delivery = service.runtime.maintenance.delivery
        row = delivery.store.update('changesets', old['id'], state='ci_waiting', next_ci=0, next_merge=0)
        original = delivery.adapter.feedback
        prepared = []
        def feedback(subject):
            value = original(subject)
            self.advance_target('src/value.txt', '3\n')
            prepared.append(refresh(delivery, old['id']))
            return value
        with patch.object(delivery.adapter, 'feedback', feedback):
            delivery.advance(row)
        current = self.row(service, work)
        self.assertEqual(current['candidate'], prepared[0]['candidate'])
        self.assertEqual(current['revision'], prepared[0]['revision'])
        self.assertEqual(current['state'], 'ci_diagnosing')
        self.assertEqual(current['ci']['kind'], 'candidate_merge_conflict')
        jobs = [j for j in service.store.runtime.items('service_jobs_model').values()
                if j['kind'] == 'ci_triage' and j['target'] == old['id']]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['revision'], current['revision'])

    def test_feedback_cannot_overwrite_a_confirmed_maintainer_merge(self):
        from homebrew_harness.platforms.merges import receipt
        service, work, old = self.manual()
        service.pause('all')
        delivery = service.runtime.maintenance.delivery
        row = delivery.store.update('changesets', old['id'], state='ci_waiting', next_ci=0, next_merge=0)
        original = delivery.adapter.feedback
        merged = []
        def feedback(subject):
            value = original(subject)
            self.peer.merge_pull(1)
            observed = delivery.adapter.metadata(subject)
            value_receipt = receipt(subject, observed, old['candidate']['commit'])
            merged.append(delivery.store.record_merge(old['id'], value_receipt))
            return value
        with patch.object(delivery.adapter, 'feedback', feedback):
            delivery.advance(row)
        current = self.row(service, work)
        self.assertEqual(current['merge'], merged[0]['merge'])
        self.assertEqual(current['revision'], merged[0]['revision'])
        self.assertEqual(current['state'], 'merged_verifying')
