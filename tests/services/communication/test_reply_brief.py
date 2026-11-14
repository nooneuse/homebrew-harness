"""A concise model-selected public outline preserves the complete work contract."""

import copy
import json
import unittest

from homebrew_harness.services.communication.reasoning import public, reply
from homebrew_harness.services.communication.spec import validate_proposal
from tests.services.communication import test_contribution_questions as fixture


class ReplyBriefTests(unittest.TestCase):
    def proposal(self):
        return {
            'next_step':'operator','reason':'Private staffing and provisioning decision',
            'alignment':'Fits project goals','questions':[
                {'id':'runner','text':'An internal runner access decision',
                 'recipient':'maintainer','status':'open'}],
            'draft':{'text':'Prepare a matrix and smoke checks; usable Session is later',
                     'scope':'code','acceptance':'UNIQUE_PRIVATE_ACCEPTANCE: preserve every test and boundary',
                     'out_of_scope':'UNIQUE_PRIVATE_EXCLUSION: full execution port is later'},
            'reply_brief':{'scope':'Preparatory matrix/checks precede the functional Session milestone.',
                'next_step':'Prepare the initial scope for review; no implementation date is set.',
                'verification':'Windows Server results apply there; Windows 11-specific behaviour remains unverified.'},
        }

    def test_brief_structure_and_bounds(self):
        good=self.proposal();validate_proposal(good,{'scopes':{'code':{}}})
        for value in (None, '', {}, {'scope':'ok'}, {'scope':'ok','next_step':False},
                      {'scope':'x'*2001,'next_step':'ok'},
                      {'scope':'ok','next_step':'x'*1001},
                      {'scope':'ok','next_step':'ok','verification':'x'*1001},
                      {'scope':'ok','next_step':'ok','authorization':True}):
            with self.subTest(value_type=type(value).__name__),self.assertRaises(ValueError):
                validate_proposal({**good,'reply_brief':value},{'scopes':{'code':{}}})

    def test_public_projection_uses_outline_and_does_not_mutate_plan(self):
        plan={'proposal':self.proposal()};original=copy.deepcopy(plan)
        projected=public(plan)
        self.assertEqual(set(projected),{'next_step','reply_brief','public_questions'})
        self.assertEqual(projected['public_questions'],[])
        self.assertNotIn('UNIQUE_PRIVATE_ACCEPTANCE',json.dumps(projected))
        self.assertNotIn('staffing',json.dumps(projected))
        projected['reply_brief']['scope']='Changed public copy'
        self.assertEqual(plan,original)

    def test_old_plan_without_brief_retains_complete_context(self):
        plan={'proposal':self.proposal()};del plan['proposal']['reply_brief']
        projected=public(plan)
        self.assertEqual(projected['draft'],plan['proposal']['draft'])
        self.assertIn('UNIQUE_PRIVATE_ACCEPTANCE',json.dumps(projected))


class ReplyBriefFlow(unittest.TestCase):
    setUp=fixture.ContributionQuestions.setUp
    start=fixture.ContributionQuestions.start
    models=fixture.ContributionQuestions.models
    active=fixture.ContributionQuestions.active
    proposal=fixture.ContributionQuestions.proposal
    communication=fixture.ContributionQuestions.communication
    setup_round=fixture.ContributionQuestions.setup_round

    def test_drafting_outline_does_not_replace_accepted_work_details(self):
        service,store,job=self.setup_round()
        lead=self.peer.proposals[0]
        lead['draft']['acceptance']='UNIQUE_PRIVATE_ACCEPTANCE: all owned checks and constraints'
        lead['draft']['out_of_scope']='UNIQUE_PRIVATE_EXCLUSION: native execution is later'
        lead['reply_brief']={'scope':'Prepare the platform matrix and import/help checks first.',
            'next_step':'Invite a contribution; the maintenance plan can arrange follow-up.',
            'verification':'Report actual environment coverage without claiming Windows 11 validation.'}
        result=service.runtime.maintenance.agent.run(job)
        self.assertNotIn('UNIQUE_PRIVATE_ACCEPTANCE',str(self.peer.models[1]['messages']))
        self.assertEqual(result['draft'],lead['draft'])
        self.assertEqual(result['reply_brief'],lead['reply_brief'])
        saved={'proposal':lead}
        changed={**self.communication(),'reply_brief':{**lead['reply_brief'],'scope':'Altered requirement'}}
        with self.assertRaisesRegex(ValueError,'cannot change'):
            reply(saved,changed,store.ms,job)
        store.ms.finish(job,result)
        request=next(r for r in service.requests() if r.kind=='discussion_requirements')
        self.assertEqual(request.arguments['candidate']['acceptance'],lead['draft']['acceptance'])
        self.assertEqual(request.arguments['candidate']['out_of_scope'],lead['draft']['out_of_scope'])
        self.assertEqual(service.work.list()['items'],[])
        self.assertEqual(self.peer.writes,[])
