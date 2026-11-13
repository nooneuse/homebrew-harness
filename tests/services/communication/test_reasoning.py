"""Question ownership, versioned knowledge and real two-model job preparation."""

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from homebrew_harness.services.project_context import build
from homebrew_harness.services.communication.spec import validate_proposal
from homebrew_harness.platforms.contracts import ObjectRef, SourceEvent
from tests.services.communication import test_flow as fixture
from tests.runtime.test_interaction import eventually


class ProjectContextTests(unittest.TestCase):
    def test_context_finds_existing_commands_and_changes_with_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / 'src').mkdir()
            (root / 'src/cli.py').write_text("commands.add_parser('session')\n")
            (root / 'pyproject.toml').write_text('[project.scripts]\nforge = "forge.cli:main"\n')
            (root / 'README.md').write_text('Current session command and platform support.\n' * 1000)
            one = build(root, {})
            self.assertIn({'name': 'session', 'path': 'src/cli.py'}, one['subcommands'])
            self.assertIn('[project.scripts]', next(d['excerpt'] for d in one['documents'] if d['path'] == 'pyproject.toml'))
            self.assertLessEqual(len(json.dumps(one, ensure_ascii=False)), 8100)
            (root / 'README.md').write_text('Updated supported behavior.')
            self.assertNotEqual(one['version'], build(root, {})['version'])
            limited = build(root, {'max_bytes': 1})
            self.assertFalse(limited['complete'])
            self.assertIsNone(limited['version'])

    def test_missing_metadata_and_private_files_do_not_break_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / '.env').write_text('private contents')
            value = build(root, {})
            self.assertTrue(value['complete'])
            self.assertEqual(value['documents'], [])
            self.assertNotIn('private contents', json.dumps(value))


class QuestionOwnership(unittest.TestCase):
    def proposal(self):
        return {'next_step': 'operator', 'reason': 'Need maintainer decision.', 'alignment': 'Fits goals.',
                'questions': [{'id': 'ci', 'text': 'Choose CI infrastructure.', 'recipient': 'maintainer', 'status': 'open'}]}

    def test_internal_questions_route_to_operator_and_never_public_question_ids(self):
        value = self.proposal()
        validate_proposal(value, {'scopes': {}})
        for step in ('clarify', 'develop', 'wait'):
            invalid = {**value, 'next_step': step}
            with self.assertRaisesRegex(ValueError, 'maintainer questions'):
                validate_proposal(invalid, {'scopes': {}})
        invalid = copy.deepcopy(value)
        invalid['communication'] = {'kind': 'post_comment', 'intent': 'followup', 'body': 'Choose a runner?', 'question_ids': ['ci']}
        with self.assertRaisesRegex(ValueError, 'open requester'):
            validate_proposal(invalid, {'scopes': {}})

    def test_closed_questions_and_invalid_recipients_are_rejected(self):
        for recipient, status in [('everyone', 'open'), ('requester', 'resolved')]:
            value = self.proposal()
            value['questions'][0].update(recipient=recipient, status=status)
            value['communication'] = {'kind': 'post_comment', 'intent': 'clarify', 'body': 'Again?', 'question_ids': ['ci']}
            with self.assertRaises(ValueError):
                validate_proposal(value, {'scopes': {}})


class DiscussionReasoning(unittest.TestCase):
    setUp = fixture.CommunicationFlow.setUp
    start = fixture.CommunicationFlow.start

    def models(self):
        env = self.root / 'model.env'
        env.write_text(env.read_text() + '\nLEAD_MODEL=planner\nREPLY_MODEL=writer\n')
        catalog = self.root / 'models.json'
        catalog.write_text(json.dumps({'version': 2, 'connections': {'peer': {
            'protocol': 'chat_completions', 'api_key_env': 'OPENAI_API_KEY', 'base_url_env': 'OPENAI_BASE_URL', 'transport': 'http'}},
            'profiles': {'lead': {'connection': 'peer', 'model_env': 'LEAD_MODEL'}, 'reply': {'connection': 'peer', 'model_env': 'REPLY_MODEL'}},
            'defaults': {'lead': 'lead'}}))
        self.host.config = replace(self.host.config, settings=replace(self.host.config.settings, models_config=catalog, lead_model='lead'))

    def active(self, service, body='Please support the existing session workflow.', identity=1, kind='issue'):
        ref = ObjectRef('github', 'primary', 'example/project', 'issue', '1', 1)
        obj = ref if kind == 'issue' else ObjectRef('github', 'primary', 'example/project', 'comment', str(identity))
        item = SourceEvent(obj, ref, str(identity), {'title': 'Feature', 'body': body, 'actor_id': 11, 'state': 'open'})
        store = service.runtime.maintenance.communication.store
        store.ingest('project', [item], ref.scope())
        row = store.get('discussions', service.discussions()['items'][0]['id'])
        job = store.queue_round(row, force=True)
        self.assertIsNotNone(job)
        job = store.ms.claim_job()
        return store, job

    def plan(self, internal=False, reply=True):
        return {'next_step': 'operator' if internal else 'wait', 'reason': 'The requested behavior is clear.',
                'alignment': 'Fits project goals.', 'reply_required': reply,
                'questions': [{'id': 'placement', 'text': 'Choose documentation placement.', 'recipient': 'maintainer', 'status': 'open'}] if internal else []}

    def test_service_model_timeout_reaches_transport_and_keeps_job_deadline(self):
        from homebrew_harness.execution.model_gateway import Deadline, ModelDeadlineExceeded

        self.models()
        self.host.config = replace(self.host.config, settings=replace(
            self.host.config.settings, request_seconds=500))
        service = self.start(model_profile='reply')
        service.pause('all')
        agent = service.runtime.maintenance.agent
        self.assertEqual(set(agent.model_control.contexts), {'lead', 'reply'})
        for alias, (binding, gateway) in agent.model_control.contexts.items():
            with self.subTest(profile=alias):
                arguments = {'model': binding.model, 'messages': [
                    {'role': 'user', 'content': 'Synthetic assessment.'}], 'max_tokens': 32}
                with gateway.scope(deadline=Deadline.after(1500)):
                    prepared, _, deadline, _ = gateway._prepare_request(
                        'worker', None, None, None, None, arguments)
                self.assertEqual(prepared[2], 500)
                self.assertGreater(deadline.remaining(), 490)
                self.assertLessEqual(deadline.remaining(), 500)
                with gateway.scope(deadline=Deadline.after(100)):
                    _, _, deadline, _ = gateway._prepare_request(
                        'worker', None, None, None, None, arguments)
                self.assertGreater(deadline.remaining(), 90)
                self.assertLessEqual(deadline.remaining(), 100)
                with gateway.scope(deadline=Deadline.after(0)):
                    with self.assertRaises(ModelDeadlineExceeded):
                        gateway.request(**arguments)
        self.assertEqual(self.peer.models, [])

    def test_assessment_and_drafting_share_the_1500_second_job_deadline(self):
        from unittest.mock import patch

        self.models()
        self.peer.proposals = [self.plan(), {'communication': {
            'kind': 'post_comment', 'intent': 'followup',
            'body': 'Thanks, the scope is clear.', 'question_ids': []}}]
        start = self.host.services.start
        def configured(spec):
            return start(replace(spec, maintenance={**spec.maintenance, 'job_seconds': 1500}))
        with patch.object(self.host.services, 'start', side_effect=configured):
            service = self.start(model_profile='reply')
        service.pause('all')
        store, job = self.active(service)
        agent = service.runtime.maintenance.agent
        with patch.object(agent, '_execute', wraps=agent._execute) as execute:
            agent.run(job)
        self.assertEqual([call.kwargs['stage'] for call in execute.call_args_list],
                         ['assessment', 'reply'])
        first, second = [call.kwargs['deadline'] for call in execute.call_args_list]
        self.assertIs(first, second)
        self.assertGreater(second.remaining(), 1490)
        self.assertLessEqual(second.remaining(), 1500)
        self.assertEqual(self.peer.writes, [])

    def test_distinct_models_assess_then_draft_without_granting_publication(self):
        self.models()
        self.peer.proposals = [self.plan(internal=True), {'communication': {'kind': 'post_comment', 'intent': 'followup', 'body': 'Thanks, the scope is clear.', 'question_ids': []}}]
        service = self.start(model_profile='reply')
        service.pause('all')
        store, job = self.active(service)
        result = service.runtime.maintenance.agent.run(job)
        self.assertEqual([request['model'] for request in self.peer.models], ['planner', 'planner', 'writer', 'writer'])
        self.assertEqual(result['questions'][0]['recipient'], 'maintainer')
        saved = store.ms.job_get(job['id'])
        self.assertEqual(saved['discussion_assessment']['plan']['proposal']['next_step'], 'operator')
        store.ms.finish(job, result)
        kinds = {request.kind for request in service.requests()}
        self.assertIn('discussion_information', kinds)
        self.assertIn('communication_authority', kinds)
        self.assertEqual(self.peer.writes, [])

    def test_native_publication_still_waits_for_approval_after_two_model_preparation(self):
        self.models()
        self.peer.issue()
        self.peer.proposals = [self.plan(internal=True), {'communication': {
            'kind': 'post_comment', 'intent': 'followup', 'body': 'Thanks, the scope is clear.', 'question_ids': []}}]
        service = self.start(model_profile='reply')
        request = eventually(lambda: next((r for r in service.requests() if r.kind == 'communication_authority'), None), 10)
        self.assertEqual(self.peer.writes, [])
        service.decide(request, 'approve', decision_id='publish-prepared-reply')
        eventually(lambda: len(self.peer.writes) == 1, 10)
        self.assertEqual(self.peer.writes[0][1]['body'].split('\n\n<!--')[0], 'Thanks, the scope is clear.')
        self.assertEqual([r['model'] for r in self.peer.models[:4]], ['planner', 'planner', 'writer', 'writer'])

    def test_model_tool_descriptions_expose_actual_ids_without_weakening_dispatch(self):
        from homebrew_harness.tools.catalog import SERVICE_TOOL_DEFINITIONS, TOOL_SCHEMAS
        self.models()
        service = self.start(model_profile='reply')
        service.pause('all')
        store, job = self.active(service)
        agent = service.runtime.maintenance.agent
        agent.job = job
        definitions = agent.describe_tools(SERVICE_TOOL_DEFINITIONS)
        properties = definitions[0]['function']['parameters']['properties']
        self.assertIn('events', properties['kind']['enum'])
        self.assertIn(job['target'], properties['identity']['enum'])
        self.assertNotIn('T', properties['identity']['enum'])
        self.assertIn('pattern', TOOL_SCHEMAS['service_read']['properties']['kind'])
        self.assertNotIn('pattern', properties['kind'])

    def test_retry_reuses_lead_and_drafting_cannot_change_assessed_scope(self):
        self.models()
        self.peer.proposals = [self.plan(), {'draft': {'text': 'unapproved change'}}]
        service = self.start(model_profile='reply')
        service.pause('all')
        store, job = self.active(service)
        agent = service.runtime.maintenance.agent
        with self.assertRaises(ValueError):
            agent.run(job)
        self.assertIsNotNone(store.ms.job_get(job['id']).get('discussion_assessment'))
        self.peer.proposals = [{'communication': None}]
        result = agent.run(job)
        self.assertEqual(result['next_step'], 'wait')
        self.assertEqual(sum(r['model'] == 'planner' for r in self.peer.models), 2)
        self.assertEqual(self.peer.writes, [])

    def test_acknowledgement_reuses_plan_but_code_change_invalidates_it(self):
        self.models()
        self.peer.proposals = [self.plan(reply=False)]
        service = self.start(model_profile='reply')
        service.pause('all')
        store, job = self.active(service)
        agent = service.runtime.maintenance.agent
        store.ms.finish(job, agent.run(job))
        before = len(self.peer.models)
        store, ack = self.active(service, 'Thanks!', identity=2, kind='comment')
        store.ms.finish(ack, agent.run(ack))
        self.assertEqual(len(self.peer.models), before)
        (self.project / 'README.md').write_text('Changed project behavior.')
        self.peer.proposals = [self.plan(reply=False)]
        store, later = self.active(service, 'Thanks!', identity=3, kind='comment')
        store.ms.finish(later, agent.run(later))
        self.assertEqual(len(self.peer.models), before + 2)

    def test_prompt_edits_are_frozen_within_job_and_adopted_by_new_jobs(self):
        self.models()
        prompt = self.root / 'draft.md'
        prompt.write_text('Old drafting instructions. Submit communication only.')
        service = self.start(model_profile='reply', prompt_files={'draft': str(prompt)})
        service.pause('all')
        def lead(request):
            prompt.write_text('New drafting instructions. Submit communication only.')
            return self.plan()
        self.peer.proposals = [lead, {'communication': None}]
        store, job = self.active(service)
        agent = service.runtime.maintenance.agent
        store.ms.finish(job, agent.run(job))
        first = next(r for r in self.peer.models if r['model'] == 'writer')
        self.assertIn('Old drafting instructions', first['messages'][0]['content'])
        self.assertNotIn('New drafting instructions', first['messages'][0]['content'])
        self.peer.proposals = [self.plan(), {'communication': None}]
        store, next_job = self.active(service, 'Also support spaces.', identity=2, kind='comment')
        store.ms.finish(next_job, agent.run(next_job))
        writers = [r for r in self.peer.models if r['model'] == 'writer']
        self.assertIn('New drafting instructions', writers[-1]['messages'][0]['content'])
