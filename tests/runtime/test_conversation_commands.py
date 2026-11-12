"""Project conventions, planning permissions and forked context contracts."""

import copy
import json
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from homebrew_harness import HarnessSession
from homebrew_harness.context.project_guidance import ProjectGuidance
from homebrew_harness.execution.test_runner import TestRunner, TestRecipe
from homebrew_harness.runtime.capabilities import Capabilities
from homebrew_harness.runtime.conversation_commands import fork_check
from homebrew_harness.runtime.host_commands import HostCommand
from tests.runtime import test_slash_commands as helpers
from tests.support.clients import fake_client, reply, call


class ConversationCommands(unittest.TestCase):
    setUp = helpers.SlashTests.setUp
    session = helpers.SlashTests.session
    repository = helpers.SlashTests.repository
    git = helpers.SlashTests.git

    def test_syntax_keeps_branch_and_planning_text(self):
        self.assertEqual(HostCommand.parse('/plan').action,'on')
        self.assertEqual(HostCommand.parse('/plan apply 12').values,(12,))
        self.assertEqual(HostCommand.parse('/fork Explore "option B"').argument,'Explore "option B"')
        self.assertEqual(HostCommand.parse('/init preview').action,'preview')
        for query in ('/plan apply 0','/plan apply','/init other','/init apply -1'):
            with self.assertRaises(ValueError):HostCommand.parse(query)

    def test_guidance_is_loaded_once_per_context_and_updated_before_next_model(self):
        (self.root/'AGENTS.md').write_text('Use functions, not global state.')
        session,client,payloads=self.session([reply('one'),reply('two')])
        session.run_user('Read project')
        self.assertTrue(any('Use functions' in m.get('content','') for m in payloads[0]))
        (self.root/'AGENTS.md').write_text('Use explicit arguments.')
        session.run_user('Continue')
        current=[m for m in payloads[1] if m.get('name')=='project_guidance']
        self.assertEqual(len(current),1);self.assertIn('explicit arguments',current[0]['content'])
        self.assertEqual(session.intent.original,'Read project')

    def test_guidance_change_invalidates_already_proposed_write(self):
        (self.root/'AGENTS.md').write_text('Old conventions')
        session,client,payloads=self.session()
        answers=iter([reply(calls=[call('write',name='write_file',arguments=json.dumps({'path':'forbidden.py','content':'bad'}))]),reply('Reconsidered')])
        def model(**kwargs):
            payloads.append(copy.deepcopy(kwargs['messages']))
            if len(payloads)==1:(self.root/'AGENTS.md').write_text('New conventions')
            return next(answers)
        client.chat.completions.create.side_effect=model
        self.assertEqual(session.run_user('Inspect changes'),'Reconsidered')
        self.assertFalse((self.root/'forbidden.py').exists())
        self.assertIn('not executed',payloads[-1][-1]['content'])

    def test_guidance_symlink_and_oversized_file_are_refused(self):
        outside=self.root.parent/(self.root.name+'-outside.md');outside.write_text('hidden');self.addCleanup(lambda:outside.unlink(missing_ok=True))
        (self.root/'AGENTS.md').symlink_to(outside)
        with self.assertRaises((ValueError,OSError)):ProjectGuidance(self.root).snapshot()
        (self.root/'AGENTS.md').unlink();(self.root/'AGENTS.md').write_text('x'*17000)
        with self.assertRaises(ValueError):ProjectGuidance(self.root).snapshot()

    def test_init_creates_and_immediately_loads_project_guidance(self):
        session,client,payloads=self.session([reply('# Project guide\nUse small functions.'),reply('done')])
        value=json.loads(session.run_user('/init'))
        self.assertTrue(value['published']);self.assertTrue((self.root/'AGENTS.md').exists())
        session.run_user('Explain project')
        self.assertTrue(any('small functions' in m.get('content','') for m in payloads[-1] if m.get('name')=='project_guidance'))

    def test_init_existing_file_is_previewed_and_apply_checks_version(self):
        (self.root/'AGENTS.md').write_text('Manual conventions')
        session,client,_=self.session([reply('Manual conventions\nAdd testing commands.')])
        value=json.loads(session.run_user('/init'))
        self.assertEqual((self.root/'AGENTS.md').read_text(),'Manual conventions')
        revision=value['draft']['revision']
        (self.root/'AGENTS.md').write_text('New manual change')
        with self.assertRaises(ValueError):session.run_user('/init apply '+str(revision))
        self.assertEqual((self.root/'AGENTS.md').read_text(),'New manual change')

    def test_init_preview_and_plan_mode_never_publish(self):
        session,client,_=self.session([reply('# Draft one'),reply('# Draft two')])
        session.run_user('/init preview');self.assertFalse((self.root/'AGENTS.md').exists())
        session.run_user('/plan');value=json.loads(session.run_user('/init'))
        self.assertFalse((self.root/'AGENTS.md').exists())
        with self.assertRaises(ValueError):session.run_user('/init apply '+str(value['draft']['revision']))
        session.run_user('/plan off');session.run_user('/init apply '+str(value['draft']['revision']))
        self.assertEqual((self.root/'AGENTS.md').read_text(),'# Draft two\n')

    def test_init_matching_content_is_idempotent_and_machine_paths_are_refused(self):
        session,client,_=self.session([reply('# Project guide')])
        session.run_user('/init preview')
        (self.root/'AGENTS.md').write_text('# Project guide\n')
        value=json.loads(session.run_user('/init apply 1'))
        self.assertTrue(value['already_present'])
        self.assertTrue(json.loads(session.run_user('/init apply 1'))['already_present'])
        with self.assertRaises(ValueError):session.project_guidance.validate_text('Build in /home/example/private-project')

    def test_plan_denies_writes_and_bash_in_real_dispatch(self):
        attempts=reply(calls=[call('write',name='write_file',arguments=json.dumps({'path':'bad.py','content':'bad'})),call('shell',name='bash',arguments=json.dumps({'command':'touch bad2'}))])
        session,client,payloads=self.session([attempts,reply('Use a focused implementation and tests.')],capabilities=Capabilities.full(memory_mode='off'))
        session.run_user('/plan');session.run_user('Implement feature')
        self.assertFalse((self.root/'bad.py').exists());self.assertFalse((self.root/'bad2').exists())
        self.assertNotIn('write_file',session.tool_pool().names);self.assertNotIn('bash',session.tool_pool().names)
        self.assertEqual(session.intent.original,'Implement feature')
        self.assertEqual(session.conversation['draft']['revision'],1)

    def test_readonly_subagent_cannot_widen_planning_access(self):
        answers=[reply(calls=[call('child',name='task',arguments=json.dumps({'prompt':'Investigate'}))]),reply(calls=[call('write',name='write_file',arguments=json.dumps({'path':'bad.py','content':'bad'}))]),reply('read result'),reply('Plan with validation')]
        session,client,payloads=self.session(answers,capabilities=Capabilities({'subagents'}))
        session.run_user('/plan');session.run_user('Design feature')
        self.assertFalse((self.root/'bad.py').exists())
        self.assertTrue(any(m.get('role')=='tool' for m in session.messages))

    def test_plan_apply_validates_observed_source_and_revision(self):
        (self.root/'code.py').write_text('x=1')
        session,client,_=self.session([reply(calls=[call('read',name='read_file',arguments=json.dumps({'path':'code.py'}))]),reply('Change the function and test it.')])
        session.run_user('/plan');session.run_user('Implement change')
        with self.assertRaises(ValueError):session.run_user('/plan apply 2')
        (self.root/'code.py').write_text('x=2')
        with self.assertRaises(ValueError):session.run_user('/plan apply 1')
        self.assertEqual(client.chat.completions.create.call_count,2)
        self.assertEqual(session.conversation['mode'],'plan')

    def test_plan_apply_executes_without_rewriting_original_intent(self):
        session,client,_=self.session([reply('Write output.py and verify it.'),reply(calls=[call('write',name='write_file',arguments=json.dumps({'path':'output.py','content':'x=1'}))]),reply('done')])
        session.run_user('/plan');session.run_user('Create output.py')
        self.assertEqual(session.run_user('/plan apply 1'),'done')
        self.assertTrue((self.root/'output.py').exists())
        self.assertEqual(session.intent.original,'Create output.py')
        with self.assertRaises(ValueError):session.run_user('/plan apply 1')

    def test_plan_acceptance_failure_keeps_readonly_mode_and_original_intent(self):
        session,client,_=self.session([reply('Draft plan')])
        session.run_user('/plan');session.run_user('Original requirement')
        before=session.intent.to_dict()
        with patch.object(session,'_save_checkpoint',side_effect=OSError('before publication')),self.assertRaises(OSError):session.run_user('/plan apply 1')
        self.assertEqual(session.intent.to_dict(),before)
        self.assertEqual(session.conversation['mode'],'plan')
        self.assertFalse(session.conversation['draft']['accepted'])
        self.assertNotIn('write_file',session.tool_pool().names)

    def test_source_changing_during_plan_read_requires_refresh(self):
        (self.root/'code.py').write_text('old')
        session,client,_=self.session([reply(calls=[call('read',name='read_file',arguments=json.dumps({'path':'code.py'}))]),reply('Plan from observation')])
        def change(event):
            if event.name=='read_file':(self.root/'code.py').write_text('new')
        session.hooks.register_hook('PreToolUse',change)
        session.run_user('/plan');session.run_user('Inspect code')
        with self.assertRaises(ValueError):session.run_user('/plan apply 1')
        self.assertEqual(client.chat.completions.create.call_count,2)

    def test_plan_keeps_goal_pending_without_calling_judge(self):
        session,client,_=self.session([reply('A plan; execution is still needed.')])
        session.goal.set_goal({'condition':'Create output','judge':False,'predicates':[{'id':'output','type':'file_exists','path':'output.py'}]})
        before=copy.deepcopy(session.goal.state['spec'])
        session.run_user('/plan');session.run_user('Discuss implementation')
        self.assertTrue(session.goal.pending);self.assertFalse(session.goal.enabled)
        self.assertEqual(session.goal.state['spec'],before)
        self.assertEqual(client.chat.completions.create.call_count,1)

    def test_goal_command_cannot_activate_execution_inside_plan_mode(self):
        session,client,_=self.session()
        session.goal.set_goal('Unmet goal');session.run_user('/plan')
        with self.assertRaises(ValueError):session.run_user('/goal resume')
        self.assertFalse(session.goal.enabled)
        self.assertEqual(client.chat.completions.create.call_count,0)

    def test_plan_acceptance_continues_the_same_goal_and_hard_conditions(self):
        session,client,_=self.session([reply('Plan: create output.py'),reply(calls=[call('write',name='write_file',arguments=json.dumps({'path':'output.py','content':'value=1'}))]),reply('Verified')])
        session.intent.update('Create the required output')
        session.goal.set_goal({'condition':'Output exists','judge':False,'predicates':[{'id':'output','type':'file_exists','path':'output.py'}]})
        identity=session.goal.state['goal_id'];spec=copy.deepcopy(session.goal.state['spec'])
        session.run_user('/plan');session.run_user('Discuss the implementation')
        session.run_user('/plan apply 1')
        self.assertEqual(session.goal.state['goal_id'],identity)
        self.assertEqual(session.goal.state['spec'],spec)
        self.assertFalse(session.goal.pending)
        self.assertEqual(session.intent.original,'Create the required output')
        self.assertTrue((self.root/'output.py').exists())

    def test_plan_recall_remains_but_auto_memory_is_not_maintained(self):
        session,client,_=self.session([reply('Design only')],capabilities=Capabilities(memory_mode='auto'))
        with patch.object(session.memory,'finish_turn') as finished:
            session.run_user('/plan');session.run_user('Design feature')
            finished.assert_not_called()

    def test_plan_off_does_not_start_execution(self):
        session,client,_=self.session([reply('Design')])
        session.run_user('/plan');session.run_user('Design feature')
        session.run_user('/plan off')
        self.assertEqual(client.chat.completions.create.call_count,1)
        self.assertNotIn('planning_mode',[m.get('name') for m in session.messages])
        self.assertIn('write_file',session.tool_pool().names)

    def test_planning_checkpoint_restores_mode_and_draft(self):
        session,client,_=self.session([reply('Design')])
        session.run_user('/plan');session.run_user('Design feature');sid=session.session_id
        session.close();self.assertTrue(session.wait_closed(5))
        restored=HarnessSession(client,'fixture',self.root,resume_session=sid);self.addCleanup(lambda:(restored.close(),restored.wait_closed(5)))
        restored._ensure_runtime()
        self.assertEqual(restored.conversation['draft']['content'],'Design')
        self.assertNotIn('write_file',restored.tool_pool().names)

    def test_mode_checkpoint_failure_does_not_expose_write_tools(self):
        session,client,_=self.session()
        session.run_user('/plan')
        with patch.object(session,'_save_checkpoint',side_effect=OSError('not published')),self.assertRaises(OSError):session.run_user('/plan off')
        self.assertNotIn('write_file',session.tool_pool().names)

    def test_managed_run_cannot_change_planning_contract(self):
        session,client,_=self.session();session.run_control=SimpleNamespace(id='run_fixture')
        with self.assertRaises(ValueError):session.run_user('/plan')
        session.run_control=None
        self.assertEqual(client.chat.completions.create.call_count,0)

    def test_fork_has_independent_id_visible_history_and_no_grants(self):
        session,client,_=self.session([reply('Parent answer')])
        session.run_user('Keep original requirement')
        session.messages[-1]['reasoning_content']='private reasoning replay'
        source=fork_check(session);parent=session.session_id
        before=copy.deepcopy(session.messages)
        session.close();self.assertTrue(session.wait_closed(5))
        child=HarnessSession(client,'fixture',self.root,fork_seed=source);self.addCleanup(lambda:(child.close(),child.wait_closed(5)))
        child._ensure_runtime()
        self.assertNotEqual(child.session_id,parent)
        self.assertEqual(child.intent.original,'Keep original requirement')
        self.assertTrue(any(m.get('content')=='Parent answer' for m in child.messages))
        self.assertFalse(any('reasoning_content' in m for m in child.messages))
        self.assertEqual(session.messages,before)
        self.assertFalse(child.tests.grants)

    def test_fork_refuses_changed_source_and_pending_goal(self):
        session,client,_=self.session()
        session.goal.set_goal('Incomplete goal')
        with self.assertRaises(ValueError):fork_check(session)
        session.goal.clear();source=fork_check(session)
        session.messages.append({'role':'user','content':'Changed after fork request'})
        session.close();self.assertTrue(session.wait_closed(5))
        child=HarnessSession(client,'fixture',self.root,fork_seed=source);self.addCleanup(lambda:(child.close(),child.wait_closed(5)))
        with self.assertRaises(ValueError):child._ensure_runtime()

    def test_api_literal_plan_text_does_not_change_mode(self):
        session,client,_=self.session([reply('literal')])
        self.assertEqual(session.run_user('/plan',literal=True),'literal')
        self.assertEqual(session.conversation['mode'],'normal')

    def host(self, answers):
        from tests.runtime.test_interaction import LocalAssembler
        from homebrew_harness.runtime.interaction_host import InteractionHost
        client,payloads=fake_client(answers)
        host=InteractionHost(LocalAssembler(self.root,client),trusted=True)
        self.addCleanup(host.close)
        return host,client,payloads

    def test_default_plan_keeps_context_and_apply_routes_execution(self):
        host,client,payloads=self.host([reply('Plan: create output.py'),reply(calls=[call('write',name='write_file',arguments=json.dumps({'path':'output.py','content':'x=1'}))]),reply('Executed')])
        host.submit('/plan')
        record=host.submit('Create output.py')
        helpers.eventually(lambda:host.session is not None and host.session.conversation['draft'] is not None)
        self.assertFalse((self.root/'output.py').exists())
        self.assertEqual(host.session.conversation['mode'],'plan')
        self.assertIn('Plan:',host.submit('/plan status')['result'])
        receipt=host.submit('/plan apply 1')
        helpers.eventually(lambda:host.state.get(receipt['task_id'])['state']=='finished')
        self.assertTrue((self.root/'output.py').exists())
        self.assertEqual(host.state.get(receipt['task_id'])['result'],'Executed')

    def test_default_init_status_can_publish_a_retained_preview(self):
        host,client,_=self.host([reply('# Project guide\nUse unittest.')])
        receipt=host.submit('/init preview')
        helpers.eventually(lambda:host.session is None and not host._busy)
        status=json.loads(host.submit('/init status')['result'])
        self.assertEqual(status['draft']['revision'],1)
        host.submit('/init apply 1')
        helpers.eventually(lambda:(self.root/'AGENTS.md').exists())
        self.assertEqual((self.root/'AGENTS.md').read_text(),'# Project guide\nUse unittest.\n')
        self.assertEqual(client.chat.completions.create.call_count,1)

    def test_default_fork_switches_to_new_identity_without_parent_change(self):
        host,client,_=self.host([reply('Parent'),reply('Child')])
        host.submit('/plan')
        record=host.submit('Explain module')
        helpers.eventually(lambda:host.session is not None and host.session.conversation['draft'] is not None)
        parent=host.state.get(record['task_id'])['target']['id']
        value=host.submit('/fork Explore another approach')
        self.assertNotEqual(value['session_id'],parent)
        helpers.eventually(lambda:host.session.conversation['draft'] is not None)
        saved=host.state.runtime.get('checkpoints','lead:'+value['session_id'])
        self.assertEqual(saved['intent']['original'],'Explain module')
        self.assertTrue(any(m.get('content')=='Parent' for m in saved['messages']))

    def test_default_planning_mode_survives_host_reopening(self):
        host,client,_=self.host([])
        host.submit('/plan');host.close()
        from tests.runtime.test_interaction import LocalAssembler
        from homebrew_harness.runtime.interaction_host import InteractionHost
        reopened=InteractionHost(LocalAssembler(self.root,client),trusted=True);self.addCleanup(reopened.close)
        self.assertEqual(json.loads(reopened.submit('/plan status')['result'])['mode'],'plan')
