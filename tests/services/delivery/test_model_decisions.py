"""Model-selected optional validation and review handling preserve real evidence."""
import json
import unittest
from unittest.mock import patch
from homebrew_harness.services.agent import ServiceAgent
from homebrew_harness.services.job_contracts import JobContract
from homebrew_harness.services.job_results import validation_plan
from tests.services.delivery import test_merge_flow as fixture
from tests.runtime.test_interaction import eventually


class ModelDecisions(unittest.TestCase):
    setUp = fixture.MergeFlowTests.setUp
    start = fixture.MergeFlowTests.start
    run_git = fixture.MergeFlowTests.run_git
    ready = fixture.MergeFlowTests.ready

    def test_static_validation_without_test_registry_records_no_fabricated_pass(self):
        from homebrew_harness import Harness, HostConfig
        self.host.close(); self.assertTrue(self.host.wait_closed(10).resources_closed)
        self.host = Harness(workdir=self.control, config=HostConfig.from_files(env_file=self.env, test_config=None, shutdown_seconds=5))
        self.addCleanup(lambda: (self.host.close(), self.host.wait_closed(10)))
        original = ServiceAgent.propose
        seen = []
        def propose(agent, proposal, **kwargs):
            value = proposal
            if agent.job['kind'] == 'validation_plan':
                seen.append(JobContract(agent.store, agent.job).input()['validation_options'])
                value = json.dumps({'tests': [], 'review': False, 'reason': 'Static file evidence is sufficient for this fixture; no runtime tests are claimed.'})
            return original(agent, value, **kwargs)
        with patch.object(ServiceAgent, 'propose', propose):
            service = self.start(required_tests=[], require_review=False, required_checks=[], check_app_ids=[], merge={'mode': 'manual', 'poll_seconds': .1})
            work = service.work.submit('Implement the bounded fixture value.')
            row = eventually(lambda: self.ready(service, work), 40)
        self.assertEqual(seen[0]['available_tests'], [])
        self.assertEqual(row['quality'], [])
        self.assertEqual(row['review']['mode'], 'not_performed')
        self.assertTrue(row['frozen'])
        self.assertEqual(len(self.peer.pulls), 1)

    def test_lead_reconsiders_a_blocking_review_with_evidence_and_gets_a_new_verdict(self):
        original = ServiceAgent.propose
        reviews, responses = [], []
        def propose(agent, proposal, **kwargs):
            value = proposal
            if agent.job['kind'] == 'review':
                reviews.append(JobContract(agent.store, agent.job).input())
                if len(reviews) == 1:
                    value = json.dumps({'text': 'Potential defect to examine.', 'findings': [{'severity': 'blocking', 'path': 'src/value.txt', 'message': 'Please check whether the accepted value is correct.'}]})
            if agent.job['kind'] == 'ci_triage':
                responses.append(JobContract(agent.store, agent.job).input())
                value = json.dumps({'classification': 'unknown', 'next_action': 'reconsider_review', 'reason': 'Accepted requirements and the passing fixed test support the existing value.', 'plan': 'Re-review the exact file with these facts.'})
            return original(agent, value, **kwargs)
        with patch.object(ServiceAgent, 'propose', propose):
            service = self.start(merge={'mode': 'manual', 'poll_seconds': .1})
            work = service.work.submit('Implement the bounded fixture value.')
            row = eventually(lambda: self.ready(service, work), 40)
        self.assertEqual(len(reviews), 2)
        self.assertEqual(len(responses), 1)
        self.assertIn('Accepted requirements', reviews[1]['review_response']['reason'])
        self.assertEqual(row['review_history'][0]['findings'][0]['severity'], 'blocking')
        self.assertEqual(row['review']['findings'], [])
        self.assertEqual(len(self.peer.pulls), 1)

    def test_validation_proposal_cannot_skip_explicit_acceptance_or_escape_recipe_roots(self):
        from types import SimpleNamespace
        options = {'available_tests': ['unit-tests'], 'required_tests': ['unit-tests'], 'required_review': True,
                   'selections': {'unit-tests': {'framework': 'unittest', 'roots': ['tests']}}}
        store = SimpleNamespace(get=lambda *args: {'work_id': 'work'}, id='service', store=SimpleNamespace(work=lambda *args: {}),
                                delivery=SimpleNamespace(validation_options=lambda work: options))
        job = {'target': 'candidate'}
        valid = {'tests': ['unit-tests'], 'review': True, 'reason': 'Run the explicit full check.'}
        self.assertEqual(validation_plan(store, job, valid)['tests'], ['unit-tests'])
        for changed in ({'tests': []}, {'review': False}, {'tests': ['not-authorized']},
                        {'selections': {'unit-tests': {'targets': ['tests.test_small']}}}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                validation_plan(store, job, {**valid, **changed})
        options['required_tests'] = []
        for targets in (['tests.test_small'], ['outside.test_small']):
            proposal = {**valid, 'selections': {'unit-tests': {'targets': targets}}}
            if targets[0].startswith('tests.'):
                self.assertEqual(validation_plan(store, job, proposal)['selections'], proposal['selections'])
            else:
                with self.assertRaises(ValueError): validation_plan(store, job, proposal)
