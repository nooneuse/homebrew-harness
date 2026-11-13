"""A validated phase ends once, survives retry and treats knowledge changes as feedback."""

import unittest
from unittest.mock import patch

from tests.services.communication import test_reasoning as fixture


class PhaseOutputs(unittest.TestCase):
    setUp = fixture.DiscussionReasoning.setUp
    start = fixture.DiscussionReasoning.start
    models = fixture.DiscussionReasoning.models
    active = fixture.DiscussionReasoning.active
    plan = fixture.DiscussionReasoning.plan

    def setup_job(self):
        self.models()
        (self.project/'src').mkdir()
        path=self.project/'src/logic.py'
        path.write_text('old implementation')
        self.peer.proposals=[self.plan(), {'communication':{'kind':'post_comment','intent':'followup','body':'Scope understood.','question_ids':[]}}]
        service=self.start(model_profile='reply')
        service.pause('all')
        store,job=self.active(service)
        return service,store,job,path

    def test_prepared_phases_reuse_without_extra_completion_calls(self):
        service,store,job,path=self.setup_job()
        agent=service.runtime.maintenance.agent
        first=agent.run(job)
        self.assertEqual([r['model'] for r in self.peer.models], ['planner','writer'])
        saved=store.ms.job_get(job['id'])['phase_outputs']
        self.assertEqual(set(saved), {'assessment','reply'})
        self.assertEqual(agent.run(job),first)
        self.assertEqual(len(self.peer.models),2)
        path.write_text('unrelated implementation change')
        self.assertEqual(agent.run(job),first)
        self.assertEqual(len(self.peer.models),2)
        self.assertEqual(self.peer.writes,[])

    def test_a_new_agent_can_reuse_prepared_stages(self):
        import threading
        from homebrew_harness.services.agent import ServiceAgent
        service,store,job,path=self.setup_job()
        first=service.runtime.maintenance.agent.run(job)
        before=len(self.peer.models)
        restored=ServiceAgent(service.runtime,store.ms,threading.Event())
        self.addCleanup(restored.close)
        self.assertEqual(restored.run(job),first)
        self.assertEqual(len(self.peer.models),before)

    def test_single_model_combined_phase_reuses_its_complete_candidate(self):
        self.peer.proposals=[{'next_step':'wait','reason':'Scope understood.',
            'alignment':'Fits project goals.','questions':[],
            'communication':{'kind':'post_comment','intent':'followup',
                'body':'Scope understood.','question_ids':[]}}]
        service=self.start()
        service.pause('all')
        store,job=self.active(service)
        agent=service.runtime.maintenance.agent
        result=agent.run(job)
        self.assertEqual(agent.run(job),result)
        self.assertEqual(len(self.peer.models),1)
        self.assertEqual(set(store.ms.job_get(job['id'])['phase_outputs']),{'combined'})

    def test_mixed_batch_cannot_finish_a_phase_before_model_sees_results(self):
        from types import SimpleNamespace
        service,store,job,path=self.setup_job()
        agent=service.runtime.maintenance.agent
        agent.proposal=self.plan()
        messages=[]
        self.assertFalse(agent.complete_phase([SimpleNamespace(name='service_read'),
                                               SimpleNamespace(name='service_propose')],messages))
        self.assertIsNone(agent.proposal)
        self.assertTrue(messages)
        self.assertNotIn('phase_outputs',store.ms.job_get(job['id']))

    def test_read_code_change_is_model_feedback_not_whole_job_failure(self):
        service,store,job,path=self.setup_job()
        agent=service.runtime.maintenance.agent
        self.peer.proposals=[self.plan(),self.plan(),{'communication':{'kind':'post_comment','intent':'followup','body':'Current scope understood.','question_ids':[]}}]
        original=agent.propose
        changed=False
        def propose(*args,**kwargs):
            nonlocal changed
            if agent.stage=='assessment' and not changed:
                agent.read_code('src/logic.py')
                path.write_text('new implementation')
                changed=True
            return original(*args,**kwargs)
        with patch.object(agent,'propose',propose):
            agent.run(job)
        self.assertEqual([r['model'] for r in self.peer.models],['planner','planner','writer'])
        self.assertIn('changed_files',str(self.peer.models[1]['messages']))
        self.assertEqual(self.peer.writes,[])

    def test_model_can_exclude_irrelevant_read_file(self):
        service,store,job,path=self.setup_job()
        agent=service.runtime.maintenance.agent
        self.peer.proposals[0]={**self.plan(),'evidence_paths':[]}
        original=agent.propose
        def propose(*args,**kwargs):
            if agent.stage=='assessment':
                agent.read_code('src/logic.py')
                path.write_text('irrelevant new implementation')
            return original(*args,**kwargs)
        with patch.object(agent,'propose',propose):
            agent.run(job)
        self.assertEqual(len(self.peer.models),2)
        self.assertEqual(store.ms.job_get(job['id'])['phase_outputs']['assessment']['evidence'],{})

    def test_assessment_evidence_changed_during_drafting_is_reassessed(self):
        service,store,job,path=self.setup_job()
        agent=service.runtime.maintenance.agent
        revised={**self.plan(), 'reason':'Current implementation assessed'}
        draft={'communication':{'kind':'post_comment','intent':'followup',
            'body':'Scope understood.','question_ids':[]}}
        self.peer.proposals=[self.plan(),draft,revised,draft]
        original=agent.propose
        changed=False
        def propose(*args,**kwargs):
            nonlocal changed
            if agent.stage=='assessment':
                agent.read_code('src/logic.py')
            elif agent.stage=='reply' and not changed:
                path.write_text('changed while drafting')
                changed=True
            return original(*args,**kwargs)
        with patch.object(agent,'propose',propose):
            result=agent.run(job)
        self.assertEqual([r['model'] for r in self.peer.models],
                         ['planner','writer','planner','writer'])
        self.assertIn('changed_phases',str(self.peer.models[2]['messages']))
        self.assertEqual(result['reason'],'Current implementation assessed')
        self.assertEqual(self.peer.writes,[])
