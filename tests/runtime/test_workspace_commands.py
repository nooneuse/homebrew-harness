"""Interactive workspace controls preserve execution ownership and recovery."""

import json
import sys
import threading
import time
import unittest
from pathlib import Path

from homebrew_harness import HarnessSession
from homebrew_harness.context.memory import MemoryRecord, MemoryStore
from homebrew_harness.execution.model_gateway import ModelCancelled
from homebrew_harness.execution.test_runner import TestRunner, TestRecipe
from homebrew_harness.runtime.activities import Activities, activity, activity_scope
from homebrew_harness.runtime.capabilities import Capabilities
from homebrew_harness.runtime.host_commands import HostCommand, exit_command
from homebrew_harness.runtime.interaction_config import InteractionAssembler, InteractionConfig
from homebrew_harness.runtime.interaction_host import InteractionHost
from homebrew_harness.runtime.local_commands import resolve_session
from tests.runtime import test_slash_commands as helpers
eventually = helpers.eventually
from tests.support.clients import fake_client, reply


class WorkspaceCommands(unittest.TestCase):
    setUp = helpers.SlashTests.setUp
    session = helpers.SlashTests.session
    git = helpers.SlashTests.git
    repository = helpers.SlashTests.repository

    def test_command_syntax_and_exit_aliases(self):
        for text in ("/pwd", "/ps", "/hooks", "/agents", "/memory", "/resume list", "/worktree list"):
            self.assertEqual(HostCommand.parse(text).mode, "read")
        self.assertEqual(HostCommand.parse('/worktree create feature --task t_1').values, ('t_1',))
        self.assertEqual(HostCommand.parse('/exit').name, 'quit')
        for text in ('q', 'quit', 'exit', '/quit', '/exit'):
            self.assertTrue(exit_command(text))
        for text in ('/pwd other', '/quit now', '/worktree use feature', '/hooks disable permission_hook', '/agents spawn coder', '/memories'):
            with self.assertRaises(ValueError):
                HostCommand.parse(text)
        self.assertFalse(exit_command('/quit now'))

    def test_all_queries_work_without_a_git_repo_or_model_call(self):
        session, client, _ = self.session()
        for text in ('/pwd', '/ps', '/agents', '/hooks', '/memory', '/resume list'):
            json.loads(session.run_user(text))
        self.assertIsNone(json.loads(session.run_user('/pwd'))['branch'])
        self.assertEqual(client.chat.completions.create.call_count, 0)
        self.assertEqual(session.intent.original, '')

    def test_typed_controls_use_their_fields_and_never_execute_query_text(self):
        session, client, _=self.session()
        command=HostCommand('Task-looking text',name='stop',action='status',mode='lifecycle')
        self.assertEqual(json.loads(session.control(command))['state'],'stop_requested')
        self.assertTrue(session._execution_paused)
        query=HostCommand('Another task',name='pwd',action='run',mode='read')
        self.assertEqual(json.loads(session.control(query))['cwd'],str(self.root))
        self.assertEqual(client.chat.completions.create.call_count,0)

    def test_individual_model_cancel_does_not_cancel_a_sibling_request(self):
        session, client, _=self.session()
        entered=[threading.Event(),threading.Event()];release=[threading.Event(),threading.Event()]
        for event in release:self.addCleanup(event.set)
        def model(**kwargs):
            index=int(kwargs['messages'][0]['content']);entered[index].set();release[index].wait(5);return reply('done')
        client.chat.completions.create.side_effect=model
        values=[]
        def request(index):
            try:
                session.gateway.request(model='fixture',messages=[{'role':'user','content':str(index)}],role='worker'+str(index))
                values.append((index,'completed'))
            except ModelCancelled:values.append((index,'cancelled'))
        workers=[threading.Thread(target=request,args=(i,)) for i in (0,1)]
        for worker in workers:worker.start()
        self.assertTrue(all(event.wait(3) for event in entered))
        row=next(r for r in session.gateway.active_requests() if r['role']=='worker0')
        session.run_user('/stop '+row['id'])
        eventually(lambda:(0,'cancelled') in values)
        self.assertTrue(workers[1].is_alive());self.assertFalse(session._execution_paused)
        for event in release:event.set()
        for worker in workers:worker.join(5)
        self.assertIn((1,'completed'),values)
        self.assertTrue(session.gateway.wait_idle(5))

    def test_memory_off_manual_correction_clears_old_attachment(self):
        session, client, _ = self.session()
        store = MemoryStore(self.root)
        record = MemoryRecord('style', 'user', 'Use spaces', 'Use spaces', 'User preference', '2026-01-01T00:00:00+00:00')
        store.replace([record], store.snapshot().version)
        session.messages.append({'role': 'system', 'name': 'memory_context', 'content': 'Old preference'})
        self.assertEqual(json.loads(session.run_user('/memory'))['mode'], 'off')
        self.assertEqual(json.loads(session.run_user('/memory show style'))['body'], 'Use spaces')
        session.run_user('/memory correct style Use tabs')
        self.assertEqual(store.snapshot().records[0].body, 'Use tabs')
        self.assertFalse(any(m.get('name') == 'memory_context' for m in session.messages))
        session.run_user('/memory delete style')
        self.assertFalse(store.snapshot().records)
        self.assertIsNone(session.memory)
        self.assertEqual(client.chat.completions.create.call_count, 0)

    def test_hooks_report_real_order_and_failure_without_running_them(self):
        session, _, _ = self.session()
        calls = []
        def broken(*args):
            calls.append(1)
            raise RuntimeError('hidden detail')
        session.hooks.register_hook('PostToolUse', broken)
        before = session.run_user('/hooks show PreToolUse')
        self.assertEqual(json.loads(before)['events'][0]['callbacks'][0], 'permission_hook')
        self.assertFalse(calls)
        session.hooks.trigger_hooks('PostToolUse', None, '')
        after = json.loads(session.run_user('/hooks show PostToolUse'))
        self.assertTrue(any(r['callback'] == 'broken' for r in after['failures']))
        self.assertNotIn('hidden detail', json.dumps(after))

    def test_worktree_binding_actual_pwd_and_removal_boundaries(self):
        self.repository()
        (self.root/'.gitignore').write_text('.harness/\n.worktrees/\n')
        self.git('add', '.gitignore'); self.git('-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'ignore runtime')
        session, client, _ = self.session(capabilities=Capabilities({'tasks', 'worktrees'}))
        task = session.workspace.store.create('Feature', 'Implement feature')
        created = json.loads(session.run_user('/worktree create feature --task '+task.id))
        self.assertTrue(Path(created['path']).is_dir())
        session.workspace.lead_claim(task.id, workdir=self.root)
        self.assertEqual(json.loads(session.run_user('/pwd'))['worktree'], 'feature')
        with self.assertRaises(ValueError):
            session.run_user('/worktree remove feature')
        session.workspace.lead_complete(task.id, workdir=self.root, summary='Done')
        path = Path(created['path']); (path/'draft.txt').write_text('keep me')
        with self.assertRaises(ValueError):
            session.run_user('/worktree remove feature')
        (path/'draft.txt').unlink()
        self.assertTrue(json.loads(session.run_user('/worktree remove feature'))['branch_retained'])
        self.assertFalse(path.exists())
        self.assertEqual(client.chat.completions.create.call_count, 0)

    def test_worktree_creation_refuses_dirty_baseline_and_disabled_capability(self):
        self.repository()
        session, _, _ = self.session()
        self.assertTrue(json.loads(session.run_user('/worktree list'))['worktrees'])
        with self.assertRaises(ValueError):
            session.run_user('/worktree create feature --task task')

    def test_worktree_current_base_and_managed_pwd_snapshot(self):
        from homebrew_harness.orchestration.task_store import TaskStore
        from homebrew_harness.orchestration.worktrees import WorktreeManager
        from homebrew_harness.runtime.local_commands import worktrees, managed_pwd
        self.repository()
        (self.root/'.gitignore').write_text('.harness/\n.worktrees/\n')
        self.git('add','.gitignore');self.git('-c','core.hooksPath=/dev/null','commit','-qm','ignore runtime')
        store=TaskStore(self.root);self.addCleanup(store.close)
        task=store.create('Feature','Read its workspace')
        created=WorktreeManager(self.root,store).create('feature',task.id)
        rows=worktrees(self.root,store.runtime)['worktrees']
        self.assertEqual(next(r for r in rows if r['cwd']==str(self.root))['current'],True)
        class Snapshot:
            def get(inner,section,key,default=None):
                return {'lead_task':task.id} if section=='checkpoints' else store.load(task.id).to_dict()
        value=managed_pwd(self.root,Snapshot(),{'id':'run_fixture','session_id':'fixture'})
        self.assertEqual(value['cwd'],created['path']);self.assertEqual(value['source'],'checkpoint snapshot')
        self.assertEqual(value['worktree'],'feature')

    def test_live_activity_stop_is_local_and_thread_safe(self):
        registry = Activities()
        with activity_scope(registry), activity('subagent', identity='sub-fixture', model='chosen') as event:
            row = registry.snapshot()[0]
            self.assertEqual(row['model'], 'chosen')
            registry.stop(row['id'])
            self.assertTrue(event.is_set())
            self.assertEqual(registry.snapshot()[0]['state'], 'stopping')
        self.assertFalse(registry.snapshot()[0]['stoppable'])
        with self.assertRaises(ValueError):
            registry.stop('sub-fixture')

    def test_stop_slow_model_returns_before_transport_and_blocks_early_resume(self):
        session, client, _ = self.session()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def model(**kwargs):
            entered.set(); release.wait(5)
            return reply('late response')
        client.chat.completions.create.side_effect = model
        failures = []
        def run():
            try: session.run_user('Keep the original requirements')
            except Exception as error: failures.append(error)
        worker = threading.Thread(target=run);worker.start()
        self.assertTrue(entered.wait(3))
        start = time.monotonic();session.run_user('/stop')
        self.assertLess(time.monotonic()-start, 1)
        self.assertTrue(session.runtime_store.get('session_meta',session.session_id)['execution_paused'])
        with self.assertRaises(ValueError):session.run_user('/resume')
        eventually(lambda: not worker.is_alive())
        self.assertTrue(any(isinstance(e, ModelCancelled) for e in failures))
        rows = json.loads(session.run_user('/ps'))['ps']
        self.assertTrue(any(row['state'] == 'cleanup_pending' for row in rows))
        release.set();self.assertTrue(session.gateway.wait_idle(5));worker.join(5)
        self.assertEqual(session.intent.original, 'Keep the original requirements')

    def test_paused_queue_stays_durable_until_resume(self):
        session, client, _ = self.session([reply('continued')])
        session.run_user('/stop')
        receipt=session.submit_user_input('Do this after resuming', mode='queued')
        time.sleep(.25)
        self.assertEqual(client.chat.completions.create.call_count, 0)
        self.assertTrue(session.inputs.pending())
        session.run_user('/resume')
        eventually(lambda: session.runtime_store.get('input_receipts', receipt['input_id'], {}).get('state') == 'applied')
        eventually(lambda: session.last_goal_result == 'continued')
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_pause_survives_checkpoint_restore_and_history_listing(self):
        session, client, _ = self.session()
        session.run_user('/stop');sid=session.session_id
        session.close();self.assertTrue(session.wait_closed(5))
        self.assertEqual(resolve_session(session.runtime_store, 'session:'+sid), sid)
        restored=HarnessSession(client,'fixture',self.root,resume_session=sid)
        self.addCleanup(lambda:(restored.close(),restored.wait_closed(5)))
        restored._ensure_runtime()
        self.assertTrue(restored._execution_paused)
        self.assertFalse(restored.process_once())
        self.assertEqual(client.chat.completions.create.call_count, 0)

    def test_background_stop_does_not_close_shared_runner_or_sibling(self):
        session, _, _ = self.session(capabilities=Capabilities({'background'}))
        first=session.background.start('sleep 20')['bg_id']
        second=session.background.start('sleep 1; printf sibling')['bg_id']
        session.run_user('/stop '+first)
        eventually(lambda: session.background.check(first, workdir=self.root) and next(r for r in session.background.snapshot() if r['bg_id']==first)['status']=='cancelled')
        eventually(lambda: next(r for r in session.background.snapshot() if r['bg_id']==second)['status']=='completed')
        row=next(r for r in session.background.snapshot() if r['bg_id']==second)
        self.assertEqual(row['result']['stdout'], 'sibling')
        self.assertFalse(session._execution_paused)
        with self.assertRaises(ValueError):session.run_user('/stop '+first)

    def test_test_stop_keeps_runner_available_for_next_registered_test(self):
        session, _, _ = self.session()
        runner=TestRunner(self.root,[TestRecipe('slow',(sys.executable,'-c','import time; time.sleep(20)')),TestRecipe('fast',(sys.executable,'-c','print("ok")'))])
        runner.approve('slow');runner.approve('fast');old=session.tests;session.tests=runner;old.close()
        self.addCleanup(lambda:(runner.close(),runner.wait_closed(5)))
        values=[];worker=threading.Thread(target=lambda:values.append(runner.run('slow')));worker.start()
        eventually(lambda:runner.activity() is not None)
        session.run_user('/stop '+runner.activity()['id'])
        worker.join(5);self.assertFalse(worker.is_alive())
        self.assertEqual(values[0]['status'], 'cancelled')
        self.assertFalse(runner.closed.is_set())
        self.assertEqual(runner.run('fast')['status'], 'passed')

    def test_api_literal_slash_text_remains_a_task(self):
        session, client, _=self.session([reply('literal received')])
        self.assertEqual(session.run_user('/stop', literal=True),'literal received')
        self.assertFalse(session._execution_paused)
        self.assertEqual(client.chat.completions.create.call_count,1)

    def test_real_subagent_registration_and_local_cancellation(self):
        from homebrew_harness.runtime.turns import run_subagent
        session, client, _ = self.session()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def model(**kwargs):
            entered.set();release.wait(5);return reply('late child')
        client.chat.completions.create.side_effect = model
        failures=[];outputs=[]
        def child():
            try:
                with activity_scope(session.activities):
                    outputs.append(run_subagent('Local task', self.root, client=session.client, model=session.model))
            except Exception as error:failures.append(error)
        worker=threading.Thread(target=child);worker.start();self.assertTrue(entered.wait(3))
        row=next(r for r in json.loads(session.run_user('/agents'))['agents'] if r['kind']=='subagent')
        self.assertEqual(row['model'],'fixture');self.assertFalse(row['steering'])
        session.run_user('/stop '+row['id'])
        worker.join(3);self.assertFalse(worker.is_alive())
        self.assertTrue(failures or outputs and json.loads(outputs[0])["status"] != "completed")
        self.assertFalse(session._execution_paused)
        release.set();self.assertTrue(session.gateway.wait_idle(5))

    def test_workflow_stop_retains_pending_owner_and_keeps_runtime_open(self):
        import asyncio
        from homebrew_harness.orchestration.workflow_runtime import WorkflowRuntime, WorkflowRegistry, WorkflowSpec, WorkflowLimits
        from tests.orchestration.test_workflow_shutdown import Runner
        entered, release=threading.Event(),threading.Event();self.addCleanup(release.set)
        async def script(ctx,args):
            entered.set()
            try:await asyncio.sleep(30)
            except asyncio.CancelledError:
                while not release.is_set():await asyncio.sleep(.01)
            return {'late':True}
        runtime=WorkflowRuntime(self.root,Runner(),WorkflowRegistry([WorkflowSpec('slow','slow',script,{'type':'object'})]),limits=WorkflowLimits(timeout_seconds=30,cleanup_seconds=.03))
        session, _, _=self.session(workflow_runtime=runtime,capabilities=Capabilities({'workflow'}))
        results=[];worker=threading.Thread(target=lambda:results.append(runtime.run('slow',{})));worker.start()
        self.assertTrue(entered.wait(3))
        row=next(r for r in json.loads(session.run_user('/ps'))['ps'] if r['kind']=='workflow')
        session.run_user('/stop '+row['id']);worker.join(3);self.assertFalse(worker.is_alive())
        self.assertTrue(results[0]['execution_pending'])
        self.assertEqual(runtime.activities()[0]['state'],'stopping')
        self.assertFalse(runtime._closed)
        release.set();self.assertTrue(runtime.wait_closed(5))

    def test_closed_session_cannot_be_resumed_as_a_live_object(self):
        session, _, _=self.session()
        session.run_user('/stop');session.close();self.assertTrue(session.wait_closed(5))
        with self.assertRaises(ValueError):session.resume_execution()
        self.assertTrue(session.runtime_store.get('session_meta',session.session_id)['execution_paused'])

    def test_default_queries_and_history_restore_do_not_reexecute_task(self):
        env=self.root.parent/(self.root.name+'-model.env');env.write_text('OPENAI_API_KEY=fixture\nOPENAI_BASE_URL=http://model.invalid\nOPENAI_MODEL=fixture\n');self.addCleanup(lambda:env.unlink(missing_ok=True))
        config=InteractionConfig(env_file=env,model_planning=False,prefer_session=True)
        assembler=InteractionAssembler(self.root,config)
        host=InteractionHost(assembler,trusted=True);self.addCleanup(host.close)
        for text in ('/pwd','/ps','/agents','/hooks','/memory','/resume list'):
            self.assertIsNotNone(host.submit(text).get('result'))
        client,_=fake_client([reply('archived answer')])
        old=HarnessSession(client,'fixture',self.root);old.run_user('Archived requirement');sid=old.session_id;old.close();self.assertTrue(old.wait_closed(5))
        # The fixture's native adapter does not match the host HTTP adapter.
        with self.assertRaises(ValueError):host.submit('/resume session:'+sid)
        self.assertIsNone(host.state.active())

    def test_default_paused_task_resumes_after_host_restart(self):
        from tests.integration.test_native_model_protocols import ProtocolService
        from tests.integration.test_model_backends import chat
        entered, release=threading.Event(),threading.Event();self.addCleanup(release.set)
        server=ProtocolService([]);self.addCleanup(server.close)
        class Replies:
            count=0
            def popleft(self):
                self.count+=1
                if self.count==1:
                    entered.set();release.wait(8)
                return chat('before stop' if self.count==1 else 'after restart')
        server.responses=Replies()
        env=self.root.parent/(self.root.name+'-restart.env');env.write_text('OPENAI_API_KEY=fixture\nOPENAI_BASE_URL='+server.url+'\nOPENAI_MODEL=fixture\n');self.addCleanup(lambda:env.unlink(missing_ok=True))
        config=InteractionConfig(env_file=env,model_planning=False,prefer_session=True)
        first=InteractionHost(InteractionAssembler(self.root,config),trusted=True)
        self.addCleanup(first.close)
        task=first.submit('Retain the original task')['task_id']
        self.assertTrue(entered.wait(5));first.submit('/stop');release.set()
        eventually(lambda:first.session is not None and not first.session._user_waiting.is_set() and not first.session.gateway.inflight)
        first.close()
        self.assertFalse(first.leases.held('interaction-host'))
        second=InteractionHost(InteractionAssembler(self.root,config),trusted=True);self.addCleanup(second.close)
        second.submit('/resume')
        eventually(lambda:second.state.get(task)['state']=='finished')
        self.assertEqual(second.state.get(task)['result'],'after restart')
        self.assertEqual(second.state.get(task)['intent']['original'],'Retain the original task')
        self.assertEqual(len(server.requests),2)

    def test_stop_during_routing_preserves_preparation_and_resumes_once(self):
        from tests.runtime.test_interaction import LocalAssembler
        from homebrew_harness.runtime.task_routing import TaskRoute
        entered,release=threading.Event(),threading.Event();self.addCleanup(release.set)
        client,_=fake_client([])
        def model(**kwargs):
            if kwargs.get('model')=='planning':
                entered.set();release.wait(5);return reply('plan')
            return reply('done')
        client.chat.completions.create.side_effect=model
        assembler=LocalAssembler(self.root,client)
        class Router:
            def propose(self,*args,**kwargs):
                assembler.gateway.request(model='planning',messages=[{'role':'user','content':'plan'}])
                return TaskRoute()
        host=InteractionHost(assembler,trusted=True,router=Router());self.addCleanup(host.close)
        tid=host.submit('Original request')['task_id'];self.assertTrue(entered.wait(3))
        started=time.monotonic();host.submit('/stop');self.assertLess(time.monotonic()-started,1)
        eventually(lambda:not host._busy)
        self.assertEqual(host.state.get(tid)['state'],'paused');self.assertIsNone(host.state.get(tid)['target'])
        with self.assertRaises(ValueError):host.submit('/resume')
        release.set();self.assertTrue(assembler.gateway.wait_idle(5));host.submit('/resume')
        eventually(lambda:host.state.get(tid)['state']=='finished')
        self.assertEqual(host.state.get(tid)['result'],'done')

    def test_stop_while_session_is_being_prepared_does_not_start_model(self):
        from tests.runtime.test_interaction import LocalAssembler
        entered,release=threading.Event(),threading.Event();self.addCleanup(release.set)
        client,_=fake_client([reply('done')])
        class Assembler(LocalAssembler):
            def session(self,route,**kwargs):
                entered.set();release.wait(5)
                return super().session(route,**kwargs)
        host=InteractionHost(Assembler(self.root,client),trusted=True);self.addCleanup(host.close)
        tid=host.submit('Original request')['task_id'];self.assertTrue(entered.wait(3))
        host.submit('/stop');release.set();eventually(lambda:not host._busy)
        self.assertEqual(host.state.get(tid)['state'],'paused')
        self.assertEqual(client.chat.completions.create.call_count,0)
        host.submit('/resume');eventually(lambda:host.state.get(tid)['state']=='finished')
        self.assertEqual(client.chat.completions.create.call_count,1)
