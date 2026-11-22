"""Default CI progress remains model-led; explicit budgets and scopes are fenced."""

import copy
import json
import unittest
from unittest.mock import patch

from homebrew_harness import RemoteService
from homebrew_harness.cli import parser
from homebrew_harness.services.cli import operate
from homebrew_harness.contracts import SDKError
from homebrew_harness.services.agent import ServiceAgent
from homebrew_harness.services.job_contracts import JobContract
from homebrew_harness.services.delivery import quality
from homebrew_harness.services.delivery.store import DeliveryStore
from homebrew_harness.services.protocol import fingerprint
from tests.runtime.test_interaction import eventually
from tests.services.delivery import test_merge_flow as fixture


class CIAdvisoryTests(unittest.TestCase):
    setUp = fixture.MergeFlowTests.setUp
    start = fixture.MergeFlowTests.start
    run_git = fixture.MergeFlowTests.run_git
    ready = fixture.MergeFlowTests.ready
    manual = fixture.MergeFlowTests.manual

    def test_default_rechecks_continue_after_warning_with_model_observation(self):
        original_verify, original_propose = quality.verify, ServiceAgent.propose
        checks, observations = [], []
        def verify(delivery, candidate, work):
            value = original_verify(delivery, candidate, work)
            checks.append(candidate['commit'])
            if len(checks) <= 3:
                value = copy.deepcopy(value)
                value['quality'][0]['status'] = 'failed'
                value.update(ok=False, test_failure={'recipe': 'unit-tests',
                    'run_id': value['quality'][0]['run_id'], 'status': 'failed',
                    'source_stable': True, 'verification': {'ok': False}})
            return value
        def propose(agent, proposal, **kw):
            if agent.job['kind'] == 'ci_triage':
                observations.append(JobContract(agent.store, agent.job).input()['ci_progress'])
                proposal = json.dumps({"next_action": "recheck", 'classification': 'flaky', 'reason': 'Synthetic recoverable test outcome.',
                                       'plan': 'Recheck this same candidate once.'})
            return original_propose(agent, proposal, **kw)
        with patch.object(quality, 'verify', verify), patch.object(ServiceAgent, 'propose', propose):
            service = self.start(merge={'mode': 'manual', 'poll_seconds': .1})
            work = service.work.submit('Retain the scoped value and current verification.')
            row = eventually(lambda: self.ready(service, work), 50)
        self.assertIsNone(service.runtime.spec.delivery['ci_fix_limit'])
        self.assertEqual(row['validation_rechecks'], 3)
        self.assertEqual(len(checks), 4)
        self.assertEqual(len(set(checks)), 1)
        self.assertTrue(observations[-1]['warning_reached'])
        self.assertEqual(observations[-1]['candidate_rechecks'], 2)
        self.assertIsNone(observations[-1]['hard_limit'])
        self.assertIn('does not require approval', observations[-1]['instruction'])

    def test_paused_limits_update_preserves_legacy_frozen_receipts_and_restart(self):
        original_create = DeliveryStore.create
        def legacy_create(store, conn, work):
            row = original_create(store, conn, work)
            row['policy'] = store.legacy_policy
            store.put(conn, 'changesets', row)
            return row
        with patch.object(DeliveryStore, 'create', legacy_create):
            service = self.start(ci_fix_limit=2, merge={'mode': 'manual', 'poll_seconds': .1})
            work = service.work.submit('Prepare and retain the scoped value.')
            row = eventually(lambda: self.ready(service, work), 40)
        service.pause('all')
        store = service.runtime.maintenance.delivery.store
        before = copy.deepcopy(store.get('changesets', row['id']))
        actions = copy.deepcopy(service.runtime.maintenance.delivery.rows('actions'))
        with RemoteService.from_root(self.control, control=True) as remote:
            result = remote.configure_ci_limits(ci_fix_limit=None, ci_fix_warning=2, request_id='ci-default-soft')
            self.assertEqual(result, {'ci_fix_limit': None, 'ci_fix_warning': 2})
            args = parser().parse_args(['service', 'delivery', service.id, 'ci-limits',
                                        '--limit', 'null', '--warning', '2', '--request-id', 'ci-default-soft'])
            self.assertEqual(operate(remote, args), result)
        current = store.get('changesets', row['id'])
        for key in ('candidate', 'quality', 'review', 'frozen', 'policy', 'revision'):
            self.assertEqual(current[key], before[key])
        self.assertEqual(current['policy_scope'], store.policy)
        self.assertEqual(store.freeze(current), before['frozen'])
        self.assertEqual(service.runtime.maintenance.delivery.rows('actions'), actions)
        self.assertEqual(work.progress()['phase'], 'delivery')
        self.assertIsNone(service.store.get(service.id)['spec']['delivery']['ci_fix_limit'])
        changed = {**store.spec, 'target_branch': 'different-target'}
        other = DeliveryStore(service.runtime, service.runtime.maintenance.store, changed)
        self.assertFalse(other.policy_matches(current))
        service.stop()
        self.assertTrue(service.wait_closed(15))
        resumed = self.host.services.start(identity=service.id)
        self.addCleanup(lambda: (resumed.close(), resumed.wait_closed(15)))
        self.assertIsNone(resumed.runtime.spec.delivery['ci_fix_limit'])
        self.assertEqual(resumed.runtime.maintenance.delivery.store.get('changesets', row['id'])['frozen'], before['frozen'])

    def test_limits_reject_unpaused_or_invalid_updates_without_mutation(self):
        service, work, row = self.manual()
        before = copy.deepcopy(service.store.get(service.id))
        with self.assertRaises(SDKError):
            service.configure_ci_limits(ci_fix_limit=None, request_id='unpaused-ci-change')
        self.assertEqual(service.store.get(service.id)['spec'], before['spec'])
        service.pause('all')
        before = copy.deepcopy(service.store.get(service.id))
        with self.assertRaises(SDKError):
            service.configure_ci_limits(ci_fix_limit=True, request_id='invalid-ci-budget')
        after = service.store.get(service.id)
        self.assertEqual(after['spec'], before['spec'])
        self.assertEqual(after['binding'], before['binding'])
        self.assertNotIn('policy_scope', service.runtime.maintenance.delivery.store.get('changesets', row['id']))

    def test_default_repair_after_two_attempts_needs_no_extra_budget_flag(self):
        from tests.services.delivery.test_target_refresh import TargetRefreshTests
        service, work, old = self.manual()
        service.pause('all')
        service.configure_base_updates('automatic', request_id='explicit-ci-integration')
        service.resume()
        TargetRefreshTests.advance_target(self, 'src/value.txt', '3\n')
        request = eventually(lambda: next((r for r in service.requests() if r.kind == 'ci_repair'), None), 30)
        service.pause('all')
        with service.store.db.write() as conn:
            current = service.store.work(work.id)
            current['ci_fix_attempts'] = 2
            service.store.db._put(conn, 'service_work', work.id, current)
        observed = next(r for r in service.requests() if r.ref.id == request.ref.id)
        self.assertIsNone(observed.arguments['repair_attempts']['limit'])
        self.assertFalse(observed.arguments['repair_attempts']['extra_required'])
        service.decide(observed, 'approve', decision_id='continue-advisory-repair')
        record = service.runtime.maintenance.delivery.store.get('decisions', request.ref.id)
        self.assertEqual(record['authorized_attempt'], 3)
        self.assertFalse(record['extra_attempt'])
        service.resume()
        current = eventually(lambda: self.ready(service, work), 45)
        self.assertEqual(service.store.work(work.id)['ci_fix_attempts'], 3)
        self.assertEqual(current['change_request'], old['change_request'])
