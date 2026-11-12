"""Deterministic interactive controls, real model payloads and readonly review."""

import copy
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from homebrew_harness import HarnessSession, ModelRegistry, ModelSpec
from homebrew_harness.runtime.host_commands import HostCommand
from homebrew_harness.runtime.interaction_config import InteractionAssembler, InteractionConfig
from homebrew_harness.runtime.interaction_host import InteractionHost
from homebrew_harness.tools.queries import diff_snapshot
from tests.integration.test_native_model_protocols import ProtocolService, responses, anthropic
from tests.integration.test_model_backends import chat
from tests.support.clients import fake_client, reply, call


def eventually(check, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return
        time.sleep(.02)
    raise AssertionError("Expected command state did not arrive")


class SlashTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        for scope in (redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            scope.__enter__()
            self.addCleanup(scope.__exit__, None, None, None)

    def session(self, values=(), **kwargs):
        client, payloads = fake_client(values)
        session = HarnessSession(client, "fixture", self.root, shutdown_timeout=.2, **kwargs)
        session._ensure_runtime()
        self.addCleanup(lambda: (session.close(), session.wait_closed(5)))
        return session, client, payloads

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, stderr=subprocess.DEVNULL).decode().strip()

    def repository(self):
        self.git("init", "-q")
        self.git("config", "user.name", "fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.root / "code.py").write_text("x = 1\n")
        self.git("add", "code.py")
        self.git("-c", "core.hooksPath=/dev/null", "commit", "-qm", "base")

    def test_parser_preserves_skill_task_and_rejects_unknown_commands(self):
        self.assertEqual(HostCommand.parse('/skills use code-review Keep "quotes"').values, ('Keep "quotes"',))
        self.assertEqual(HostCommand.parse('/diff --base HEAD "a b.py"').values, ('HEAD', ('a b.py',)))
        with self.assertRaises(ValueError):
            HostCommand.parse('/unknown')
        with self.assertRaises(ValueError):
            HostCommand.parse('/compact wrong')

    def test_help_model_and_skills_queries_do_not_call_model(self):
        session, client, _ = self.session()
        for query in ('/help', '/model', '/reasoning', '/skills'):
            self.assertTrue(session.run_user(query))
        self.assertEqual(client.chat.completions.create.call_count, 0)
        self.assertEqual(session.intent.original, '')

    def test_model_and_reasoning_change_real_payload_preserving_intent(self):
        session, client, _ = self.session([reply('first'), reply('second')])
        session.run_user('Keep exact constraint')
        before = session.intent.to_dict()
        session.run_user('/model other')
        session.run_user('/reasoning high')
        self.assertEqual(session.intent.to_dict(), before)
        self.assertEqual(client.chat.completions.create.call_count, 1)
        session.run_user('continue')
        payload = client.chat.completions.create.call_args.kwargs
        self.assertEqual(payload['model'], 'other')
        self.assertEqual(payload['reasoning_effort'], 'high')
        self.assertEqual(session.intent.to_dict(), before)

    def test_saved_model_choice_restores_without_changing_catalog(self):
        session, client, _ = self.session()
        session.run_user('/model other')
        session.run_user('/reasoning high')
        sid = session.session_id
        session.close(); self.assertTrue(session.wait_closed(5))
        restored = HarnessSession(client, 'fixture', self.root, resume_session=sid)
        self.addCleanup(lambda: (restored.close(), restored.wait_closed(5)))
        restored._ensure_runtime()
        self.assertEqual(restored.model, 'other')
        self.assertEqual(restored.model_selection['reasoning'], 'high')

    def test_failed_model_checkpoint_keeps_old_binding(self):
        session, _, _ = self.session()
        binding, messages = session.lead_binding, copy.deepcopy(session.messages)
        with patch.object(session, '_save_checkpoint', side_effect=OSError('failure')), self.assertRaises(OSError):
            session.run_user('/model other')
        self.assertEqual(session.lead_binding.identity(), binding.identity())
        self.assertEqual(session.messages, messages)

    def test_readonly_review_cannot_write_or_change_goal_intent(self):
        self.repository()
        (self.root/'code.py').write_text('x = 2\n')
        attempted = reply(calls=[call('write', name='write_file', arguments=json.dumps({'path': 'forbidden.py', 'content': 'bad'}))], finish_reason='tool_calls')
        session, client, _ = self.session([attempted, reply('finding')])
        output = json.loads(session.run_user('/review'))
        self.assertFalse((self.root/'forbidden.py').exists())
        self.assertFalse(output['stale'])
        self.assertEqual(session.intent.original, '')
        self.assertTrue(any(m.get('name') == 'review_result' for m in session.messages))
        names = {t['function']['name'] for t in client.chat.completions.create.call_args.kwargs['tools']}
        self.assertNotIn('write_file', names)
        self.assertNotIn('bash', names)

    def test_diff_base_untracked_and_symlink_boundary(self):
        self.repository()
        base = self.git('rev-parse', 'HEAD')
        (self.root/'code.py').write_text('x = 2\n')
        self.git('add', 'code.py'); self.git('-c','core.hooksPath=/dev/null','commit','-qm','change')
        (self.root/'new.py').write_text('new = True\n')
        snapshot = diff_snapshot(workdir=self.root, base=base)
        self.assertIn('+x = 2', snapshot['diff'])
        self.assertIn('new = True', snapshot['diff'])
        (self.root/'linked.py').symlink_to(self.root/'code.py')
        with self.assertRaises(ValueError):
            diff_snapshot(workdir=self.root, paths=['linked.py'])

    def test_review_reports_changes_to_its_snapshot(self):
        self.repository()
        (self.root/'code.py').write_text('x = 2\n')
        session,client,_=self.session()
        def external_update(**kwargs):
            (self.root/'code.py').write_text('x = 3\n')
            return reply('finding')
        client.chat.completions.create.side_effect=external_update
        result=json.loads(session.run_user('/review'))
        self.assertTrue(result['stale'])

    def test_small_target_keeps_current_model_and_history(self):
        endpoint='http://'+'model.invalid'
        registry=ModelRegistry([ModelSpec('small','small-model','synthetic-key',endpoint,context_chars=10000)])
        session,_,_=self.session(model_registry=registry)
        original=session.lead_binding.identity()
        session.messages.append({'role':'user','content':'Keep these facts. '*2000})
        with self.assertRaises(ValueError):
            session.run_user('/model small')
        self.assertEqual(session.lead_binding.identity(),original)

    def test_compaction_failure_preserves_history(self):
        session, _, _ = self.session([reply('answer')])
        session.run_user('Keep requirement')
        before = copy.deepcopy(session.messages)
        with patch.object(session.compactor, 'compact_history', side_effect=RuntimeError('failure')), self.assertRaises(RuntimeError):
            session.run_user('/compact')
        self.assertEqual(session.messages, before)

    def test_explicit_skill_loads_as_attachment_not_permission(self):
        session, _, _ = self.session([reply('done')])
        skill = next(iter(session.skills.skills))
        session.run_user('/skills use '+skill+' Describe this project')
        self.assertEqual(session.intent.original, 'Describe this project')
        self.assertTrue(any(m.get('name') == 'selected_skill' for m in session.messages))


class HostSlashTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve(); self.work = self.root/'work';self.work.mkdir()
        self.env = self.root/'model.env'

    def host(self, values):
        server=ProtocolService(values);self.addCleanup(server.close)
        self.env.write_text('OPENAI_API_KEY=synthetic-credential\nOPENAI_BASE_URL='+server.url+'\nOPENAI_MODEL=fixture\n');self.env.chmod(0o600)
        host=InteractionHost(InteractionAssembler(self.work,InteractionConfig(env_file=self.env,model_planning=False,shutdown_seconds=.2)),trusted=True)
        self.addCleanup(host.close)
        return host,server

    def test_idle_queries_and_model_preference_survive_reopen(self):
        host,server=self.host([])
        for text in ('/help','/model','/skills'):
            self.assertTrue(host.submit(text)['result'])
        host.submit('/model other');host.submit('/reasoning high')
        self.assertEqual(len(server.requests),0)
        host.close()
        resumed=InteractionHost(InteractionAssembler(self.work,InteractionConfig(env_file=self.env,model_planning=False)),trusted=True)
        self.addCleanup(resumed.close)
        self.assertEqual(json.loads(resumed.submit('/model')['result'])['model'],'other')

    def test_busy_model_command_is_queued_and_next_task_uses_it(self):
        host,server=self.host([chat('first'),chat('second')])
        entered, release = threading.Event(), threading.Event()
        original = server.responses
        class Gate:
            first = True
            def popleft(self):
                value = original.popleft()
                if self.first:
                    self.first = False
                    entered.set(); release.wait(5)
                return value
        server.responses = Gate()
        receipt=host.submit('First task')
        self.assertTrue(entered.wait(5))
        result=host.submit('/model other')
        self.assertIn('command_id', result)
        release.set()
        eventually(lambda:host.state.get(receipt['task_id'])['state']=='finished')
        second=host.submit('Second task')
        eventually(lambda:host.state.get(second['task_id'])['state']=='finished')
        self.assertEqual(server.requests[-1]['model'],'other')

    def test_idle_compact_restores_the_completed_conversation(self):
        host,server=self.host([chat('answer'),chat('summary')])
        task=host.submit('Keep the requirement')
        eventually(lambda:host.state.get(task['task_id'])['state']=='finished')
        result=host.submit('/compact')
        self.assertIn('command_id',result)
        eventually(lambda:not host._busy and host.session is None)
        sid=host.state.get(task['task_id'])['target']['id']
        snapshot=host.state.runtime.get('checkpoints','lead:'+sid)
        self.assertEqual(snapshot['intent']['original'],'Keep the requirement')
        self.assertTrue(any(m.get('content','').startswith('[Reactive compact]') for m in snapshot['messages']))
        self.assertEqual(len(server.requests),2)
        self.assertEqual(host.state.runtime.get('input_receipts',result['command_id'])['state'],'applied')

    def test_preference_failure_restores_frontend_binding(self):
        host,server=self.host([])
        original=host.assembler.binding.identity()
        with patch.object(host.state.runtime,'put',side_effect=OSError('failure')),self.assertRaises(OSError):
            host.submit('/model other')
        self.assertEqual(host.assembler.binding.identity(),original)
        self.assertEqual(len(server.requests),0)

    def test_new_chat_removes_previous_reference(self):
        host,server=self.host([chat('first'),chat('second')])
        first=host.submit('First');eventually(lambda:host.state.get(first['task_id'])['state']=='finished')
        host.submit('/new')
        second=host.submit('Second');eventually(lambda:host.state.get(second['task_id'])['state']=='finished')
        self.assertFalse(any(m.get('name')=='previous_task_reference' for m in server.requests[-1]['messages']))


class NativeChoiceTests(unittest.TestCase):
    def test_cross_protocol_choice_removes_replay_and_preserves_goal_and_todo(self):
        from homebrew_harness.runtime.capabilities import Capabilities
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve()
            first=ProtocolService([chat('first')]);second=ProtocolService([anthropic('second')])
            try:
                registry=ModelRegistry([
                    ModelSpec('first','first-model','synthetic-key',first.url),
                    ModelSpec('second','second-model','synthetic-key',second.url,protocol='anthropic_messages'),
                ],lead_default='first')
                session=HarnessSession(None,'unused',root,model_registry=registry,capabilities=Capabilities.default())
                try:
                    session.run_user('Preserve exact requirements')
                    session.todo.update([{'content':'Keep plan','status':'pending'}])
                    intent=session.intent.to_dict()
                    session.run_user('/model second')
                    self.assertEqual(session.intent.to_dict(),intent)
                    self.assertEqual(session.todo.items[0].content,'Keep plan')
                    self.assertFalse(any('_protocol_state' in m for m in session.messages))
                    self.assertEqual(session.goal.evaluator.model,'second-model')
                    session.run_user('continue')
                    self.assertEqual(second.requests[0]['model'],'second-model')
                finally:
                    session.close();session.wait_closed(5)
            finally:first.close();second.close()

    def test_compact_uses_existing_summary_and_keeps_original_intent(self):
        client,_=fake_client([reply('answer'),reply('summary')])
        with tempfile.TemporaryDirectory() as temporary:
            session=HarnessSession(client,'fixture',Path(temporary))
            try:
                session.run_user('Keep original constraint')
                before=session.intent.to_dict()
                session.run_user('/compact')
                self.assertEqual(session.intent.to_dict(),before)
                self.assertTrue(session.compactor.store.checkpoint()['saved'])
            finally:session.close();session.wait_closed(5)

    def test_model_choice_and_reasoning_are_carried_to_worker_arguments(self):
        config=InteractionConfig(model_selection={'profile':None,'model':'chosen','reasoning':'high'})
        args=config.worker_arguments(Path('/private/tmp'))
        index=args.index('--model-selection')
        self.assertEqual(json.loads(args[index+1])['model'],'chosen')
