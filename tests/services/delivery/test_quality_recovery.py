"""Failed fixed-candidate tests remain evidence and use native diagnosis/recovery."""

import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.services.delivery import test_flow as fixture
from tests.runtime.test_interaction import eventually
from homebrew_harness.services.delivery import quality
from homebrew_harness.services.agent import ServiceAgent
from homebrew_harness.services.job_contracts import JobContract


class QualityRecoveryTests(unittest.TestCase):
    setUp = fixture.FlowTests.setUp
    run_git = fixture.FlowTests.run_git
    start = fixture.FlowTests.start
    pending_target = fixture.FlowTests.pending_target

    def test_failed_fixed_test_is_diagnosed_repaired_and_delivered_as_pr(self):
        test = self.workspace / 'tests/test_value.py'
        test.write_text(test.read_text().replace('read_text()),1)', 'read_text()),2)'))
        self.run_git('add', 'tests', cwd=self.workspace)
        self.run_git('commit', '-qm', 'Require repaired value', cwd=self.workspace)
        self.run_git('push', str(self.remote), 'main', cwd=self.workspace)
        service = self.start(auto_ci_fix=True, merge={'mode': 'manual', 'poll_seconds': .1})
        work = service.work.submit('Fix the scoped value and retain its registered test.')
        eventually(lambda: self.peer.pulls, 40)
        rows = service.runtime.maintenance.delivery.rows('changesets')
        first = next(row for row in rows if row.get('test_failure'))
        latest = service.store.work(work.id)
        current = service.runtime.maintenance.store.get('changesets', latest['changeset_id'])
        self.assertEqual(first['ci_diagnosis']['classification'], 'code')
        self.assertEqual(first['quality'][0]['status'], 'failed')
        self.assertEqual(current['quality'][0]['status'], 'passed')
        self.assertEqual(latest['ci_fix_attempts'], 1)
        self.assertNotEqual(first['run_id'], latest['run_id'])
        self.assertEqual(latest['requirements_revision'], 0)
        self.assertEqual(len(self.peer.pulls), 1)
        self.assertFalse(first.get('change_request'))
        output = first['quality'][0]['outputs']['stderr']
        read = quality.read_output(first, output['id'], 0, 60)
        rest = quality.read_output(first, output['id'], read['next_offset'], 16000)
        self.assertIn('FAILED', read['text'] + rest['text'])
        historical = {**first, 'quality': [], 'validation_history': [
            {'candidate_commit': first['candidate']['commit'], 'quality': first['quality']}]}
        self.assertEqual(quality.read_output(historical, output['id'], 0, 60), read)
        historical['validation_history'][0]['candidate_commit'] = 'f' * 40
        with self.assertRaises(ValueError):
            quality.read_output(historical, output['id'], 0, 60)
        relocated = {**first, 'quality': [], 'candidate': {**first['candidate'],
            'commit': 'f' * 40, 'workspace': str(self.control)}, 'candidate_history': [{'workspace': first['candidate']['workspace'],
            'commit': first['candidate']['commit'], 'quality': first['quality']}]}
        self.assertEqual(quality.read_output(relocated, output['id'], 0, 60), read)
        agent = service.runtime.maintenance.agent
        contract = JobContract(agent.store, {'kind': 'ci_triage', 'target': first['id']})
        contract.authorize('quality_output', output['id'])
        with self.assertRaises(ValueError):
            contract.authorize('quality_output', 'unrelated:stderr')
        with self.assertRaises(ValueError):
            JobContract(agent.store, {'kind': 'triage', 'target': work.id}).authorize('quality_output', output['id'])
        path = Path(first['candidate']['workspace']) / output['path']
        path.write_text('Replaced output')
        with self.assertRaises(ValueError):
            quality.read_output(first, output['id'], 0, 60)

    def test_model_requested_recheck_keeps_candidate_and_has_a_bound(self):
        original_verify, original_propose = quality.verify, ServiceAgent.propose
        candidates = []
        def verify(delivery, candidate, work):
            result = original_verify(delivery, candidate, work)
            candidates.append(candidate['commit'])
            result = copy.deepcopy(result)
            result['quality'][0]['status'] = 'failed'
            result.update(ok=False, test_failure={'recipe': 'unit-tests',
                'run_id': result['quality'][0]['run_id'], 'status': 'failed',
                'source_stable': True, 'verification': {'ok': False}})
            return result
        def propose(agent, proposal, **kw):
            if agent.job['kind'] == 'ci_triage':
                proposal = json.dumps({"next_action": "recheck", 'classification': 'flaky', 'reason': 'Synthetic scheduling failure.',
                        'plan': 'Recheck the same fixed candidate once.'})
            return original_propose(agent, proposal, **kw)
        with patch.object(quality, 'verify', verify), patch.object(ServiceAgent, 'propose', propose):
            service = self.start(ci_fix_limit=1)
            work = service.work.submit('Fix the scoped value.')
            row = eventually(lambda: next((r for r in service.runtime.maintenance.delivery.rows('changesets')
                if r['state'] == 'blocked'), None), 30)
        self.assertEqual(row['error'], 'candidate_recheck_limit')
        self.assertEqual(row['validation_rechecks'], 1)
        self.assertEqual(len(row['validation_history']), 2)
        self.assertEqual(len(candidates), 2)
        self.assertEqual(len(set(candidates)), 1)
        self.assertFalse(self.peer.pulls)
        self.assertEqual(service.store.work(work.id).get('ci_fix_attempts', 0), 0)
        self.assertTrue(any(i['code'] == 'delivery_blocked' for i in service.health()['issues']))
        self.assertEqual(work.progress()['delivery']['error'], 'candidate_recheck_limit')

    def test_transient_diagnosis_rechecks_and_opens_pr_after_passing(self):
        original_verify, original_propose = quality.verify, ServiceAgent.propose
        attempts = []
        def verify(delivery, candidate, work):
            result = original_verify(delivery, candidate, work)
            attempts.append(candidate['commit'])
            if len(attempts) == 1:
                result = copy.deepcopy(result)
                result['quality'][0]['status'] = 'failed'
                result.update(ok=False, test_failure={'recipe': 'unit-tests',
                    'run_id': result['quality'][0]['run_id'], 'status': 'failed',
                    'source_stable': True, 'verification': {'ok': False}})
            return result
        def propose(agent, proposal, **kw):
            if agent.job['kind'] == 'ci_triage':
                proposal = json.dumps({"next_action": "recheck", 'classification': 'infrastructure',
                    'reason': 'Synthetic transient execution failure.',
                    'plan': 'Recheck the fixed candidate with the registered tests.'})
            return original_propose(agent, proposal, **kw)
        with patch.object(quality, 'verify', verify), patch.object(ServiceAgent, 'propose', propose):
            service = self.start(merge={'mode': 'manual', 'poll_seconds': .1})
            work = service.work.submit('Fix the scoped value.')
            eventually(lambda: self.peer.pulls, 30)
        row = service.runtime.maintenance.store.get('changesets', service.store.work(work.id)['changeset_id'])
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(set(attempts)), 1)
        self.assertEqual(row['quality'][0]['status'], 'passed')
        self.assertEqual(row['validation_history'][0]['quality'][0]['status'], 'failed')
        self.assertIsNone(row.get('test_failure'))
        self.assertEqual(service.store.work(work.id).get('ci_fix_attempts', 0), 0)

    def test_explicit_refresh_recovers_failed_candidate_and_health_exposes_it(self):
        original = quality.verify
        calls = 0
        def verify(delivery, candidate, work):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError('Synthetic verifier failure')
            return original(delivery, candidate, work)
        with patch.object(quality, 'verify', verify):
            service = self.start(merge={'mode': 'manual', 'poll_seconds': .1})
            work = service.work.submit('Fix the scoped value.')
            row = eventually(lambda: next((r for r in service.runtime.maintenance.delivery.rows('changesets')
                if r['state'] == 'failed'), None), 30)
            self.assertFalse(service.health()['ready'])
            issue = next(i for i in service.health()['issues'] if i['code'] == 'delivery_blocked')
            self.assertEqual(issue['changeset_id'], row['id'])
            self.assertIn('Synthetic verifier failure', issue['message'])
            run_id = service.store.work(work.id)['run_id']
            first = service.refresh_delivery(row['id'], request_id='recheck-failed')
            self.assertEqual(service.refresh_delivery(row['id'], request_id='recheck-failed'), first)
            eventually(lambda: self.peer.pulls, 30)
        current = service.runtime.maintenance.store.get('changesets', row['id'])
        self.assertEqual(current['candidate']['commit'], row['candidate']['commit'])
        self.assertEqual(service.store.work(work.id)['run_id'], run_id)
        self.assertEqual(current['validation_history'][0]['error'], 'RuntimeError')
        self.assertFalse(any(i['code'] == 'delivery_blocked' for i in service.health()['issues']))
        store = service.runtime.maintenance.store
        with store.db.write() as conn:
            historical = {**row, 'id': row['id'] + '_historical'}
            store.put(conn, 'changesets', historical)
        self.assertFalse(any(i['code'] == 'delivery_blocked' for i in service.health()['issues']))
