"""The model, rather than a wallclock heuristic, controls owned test execution."""

import asyncio
import io
import json
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from homebrew_harness.runtime.session import HarnessSession
from homebrew_harness.runtime.turns import run_subagent
from homebrew_harness.runtime.hooks import create_hooks
from homebrew_harness.runtime.loop_components import LoopComponents
from homebrew_harness.runtime.assembly import _prepare_loop
from homebrew_harness.execution.model_gateway import ensure_gateway
from homebrew_harness.execution.test_runner import TestRecipe, TestRunner
from homebrew_harness.execution.test_selection import LAUNCHER
from homebrew_harness.execution.background import ShellRunner
from homebrew_harness.execution.operations import CONTROL_TOOLS, Operations
from homebrew_harness.security.permissions import noninteractive_permissions, PermissionDecision
from tests.support.clients import fake_client, reply, call


class ModelControlledTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        (self.root/'tests').mkdir()
        (self.root/'tests/__init__.py').write_text('')
        (self.root/'tests/test_slow.py').write_text('import time,unittest\nclass Work(unittest.TestCase):\n def test_wait(self):\n  print("working",flush=True)\n  time.sleep(.25)\n def test_failure(self): self.fail("not selected")\n')
        for redirect in (redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            redirect.__enter__()
            self.addCleanup(redirect.__exit__, None, None, None)
        recipe = TestRecipe('quality', (sys.executable, '-I', '-c', LAUNCHER), selector='unittest', roles=('lead','subagent','teammate'))
        self.tests = TestRunner(self.root, [recipe])
        self.tests.approve('quality')
        self.addCleanup(self.tests.close)

    def client(self, mode='interactive', stop=False):
        client, _ = fake_client([])
        seen = []
        def respond(**request):
            names = {tool['function']['name'] for tool in request['tools']}
            self.assertTrue(CONTROL_TOOLS <= names)
            index = len(seen)
            seen.append(request)
            if index == 0:
                execution = {'mode': mode}
                if mode == 'interactive': execution['wait_seconds'] = 0
                return reply(calls=[call('start', name='run_tests', arguments=json.dumps({'name': 'quality', 'selection': {'targets': ['tests.test_slow.Work.test_wait']}, 'execution': execution}))])
            if index == 1:
                result = json.loads(next(m['content'] for m in reversed(request['messages']) if m['role']=='tool'))
                if mode == 'one_shot':
                    self.assertEqual(result['status'], 'passed')
                    self.assertEqual(result['test_scope'], 'selected')
                    return reply('done')
                self.assertTrue(result['execution_pending'])
                identity = result['execution_id']
                name = 'stop_execution' if stop else 'wait_execution'
                args = {'execution_id': identity} if stop else {'execution_id': identity, 'wait_seconds': 3}
                return reply(calls=[call('control', name=name, arguments=json.dumps(args))])
            return reply('done')
        client.chat.completions.create.side_effect = respond
        return client, seen

    def test_session_model_selects_observes_and_waits_for_selected_test(self):
        client, seen = self.client()
        with noninteractive_permissions(), HarnessSession(client, 'fixture', self.root, test_runner=self.tests) as session:
            self.assertEqual(session.run_user('Run the selected test'), 'done')
            rows = session.operations.snapshots()
            test = next(r for r in rows if r['name'] == 'run_tests')
            self.assertEqual(test['result']['status'], 'passed')
            self.assertFalse(self.tests.verify('quality', self.root)['ok'])
            self.assertTrue(any(m.get('name') == 'execution_result' for m in session.messages))
            self.assertFalse(session.operations.pending())
        self.assertGreaterEqual(len(seen), 3)

    def test_one_shot_model_receives_final_result(self):
        client, seen = self.client('one_shot')
        with noninteractive_permissions(), HarnessSession(client, 'fixture', self.root, test_runner=self.tests) as session:
            self.assertEqual(session.run_user('Wait for selected test'), 'done')
        self.assertEqual(len(seen), 2)

    def test_exact_terminal_approval_preserves_nested_test_selection(self):
        self.tests.revoke('quality')
        client, _ = self.client('one_shot')
        with patch('homebrew_harness.security.permissions.sys.stdin.isatty', return_value=True), patch('builtins.input', return_value='y') as approve:
            with HarnessSession(client, 'fixture', self.root, test_runner=self.tests) as session:
                self.assertEqual(session.run_user('Approve only the selected test'), 'done')
                receipt = next(row['result'] for row in session.operations.snapshots() if row['name'] == 'run_tests')
                self.assertEqual(receipt['selection'], {'targets': ['tests.test_slow.Work.test_wait']})
        approve.assert_called_once()

    def test_subagent_has_its_own_interactive_execution_controls(self):
        client, seen = self.client()
        with noninteractive_permissions():
            result = json.loads(run_subagent('Run a selected test', self.root, client=client, model='fixture', test_runner=self.tests))
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(result['cleanup_complete'])
        self.assertGreaterEqual(len(seen), 3)

    def test_run_one_shot_survives_episode_deadline_without_relaunch(self):
        from homebrew_harness.runs import RunStore, RunSpec, RunController
        from homebrew_harness.runtime.capabilities import Capabilities
        path = self.root / 'tests/test_slow.py'
        path.write_text(path.read_text().replace('time.sleep(.25)', 'time.sleep(2)'))
        store = RunStore(self.root)
        self.addCleanup(store.close)
        spec = RunSpec('Run the selected test', tools=('run_tests',), allow_tests=('quality',),
                       total_seconds=15, episode_seconds=1, episode_model_calls=4,
                       request_seconds=5, shutdown_seconds=3)
        state = store.claim(store.create(spec)['id'])
        control = RunController(store, state['id'], state['owner'], state['generation'])
        self.addCleanup(control.close_heartbeat)
        client, _ = self.client('one_shot')
        with HarnessSession(client, 'fixture', self.root, test_runner=self.tests,
                            capabilities=Capabilities(control.spec.enabled), run_control=control) as session:
            outcome = control.execute()
            self.assertEqual(outcome.status, 'completed', outcome)
            attempts = [row for row in session.operations.snapshots() if row['name'] == 'run_tests']
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0]['result']['status'], 'passed')
            self.assertTrue(attempts[0]['cleanup']['complete'])
        self.assertEqual(self.tests.runs, 1)

    def test_model_can_cancel_without_claiming_test_success(self):
        client, _ = self.client(stop=True)
        with noninteractive_permissions(), HarnessSession(client, 'fixture', self.root, test_runner=self.tests) as session:
            self.assertEqual(session.run_user('Start then cancel'), 'done')
            test = next(r for r in session.operations.snapshots() if r['name']=='run_tests')
            self.assertEqual(test['state'], 'cancelled')
            self.assertFalse(self.tests.verify('quality', self.root)['ok'])

    def test_parent_steering_wakes_subagent_waiting_on_a_long_test(self):
        from homebrew_harness.runtime.capabilities import Capabilities
        path = self.root / 'tests/test_slow.py'
        path.write_text(path.read_text().replace('time.sleep(.25)', 'time.sleep(20)'))
        client, _ = fake_client([])
        steps = {'parent': 0, 'child': 0}
        lock = threading.Lock()
        waiting = threading.Event()
        direction = 'Stop the long test now and report its cancellation.'
        child_execution = None
        def respond(**request):
            nonlocal child_execution
            child = any('focused subagent' in (m.get('content') or '') for m in request['messages'] if m['role'] == 'system')
            actor = 'child' if child else 'parent'
            with lock:
                steps[actor] += 1
                index = steps[actor]
            if child:
                if index == 1:
                    return reply(calls=[call('long', name='run_tests', arguments=json.dumps({
                        'name': 'quality', 'selection': {'targets': ['tests.test_slow.Work.test_wait']},
                        'execution': {'wait_seconds': 0}
                    }))])
                if index == 2:
                    result = json.loads(next(m['content'] for m in reversed(request['messages']) if m['role'] == 'tool'))
                    child_execution = result['execution_id']
                    waiting.set()
                    return reply('Waiting for the long test.')
                if index == 3:
                    self.assertTrue(any(m.get('name') == 'parent_steering' and m.get('content') == direction for m in request['messages']))
                    return reply(calls=[call('stop', name='stop_execution', arguments=json.dumps({'execution_id': child_execution}))])
                return reply('Cancelled the test.')
            if index == 1:
                return reply(calls=[call('child', name='task', arguments=json.dumps({
                    'prompt': 'Run the selected test', 'execution': {'wait_seconds': 0}
                }))])
            if index == 2:
                self.assertTrue(waiting.wait(3))
                result = json.loads(next(m['content'] for m in reversed(request['messages']) if m['role'] == 'tool'))
                return reply(calls=[call('steer', name='send_execution_input', arguments=json.dumps({
                    'execution_id': result['execution_id'], 'input': direction, 'request_id': 'stop-test'
                }))])
            return reply('Done.')
        client.chat.completions.create.side_effect = respond
        with noninteractive_permissions(), HarnessSession(client, 'fixture', self.root,
                test_runner=self.tests, capabilities=Capabilities(('subagents',)), turn_timeout=8) as session:
            self.assertEqual(session.run_user('Delegate and stop the long test'), 'Done.')
            self.assertFalse(session.operations.pending())
            task = next(row for row in session.operations.snapshots() if row['name'] == 'task')
            self.assertEqual(task['controls']['input:stop-test']['state'], 'applied')
        self.assertGreaterEqual(steps['child'], 4)

    def test_slow_permission_hook_yields_without_starting_business_effect(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        hooks = create_hooks()
        def gate(event):
            if event.name == 'read_file':
                entered.set()
                release.wait(3)
        hooks.register_hook('PreToolUse', gate)
        client, _ = fake_client([])
        calls = []
        def respond(**request):
            calls.append(request)
            if len(calls)==1:
                return reply(calls=[call('slow',name='invoke_tool',arguments=json.dumps({'tool':'read_file','arguments':{'path':'source.txt'},'execution':{'wait_seconds':0}}))])
            if len(calls)==2:
                self.assertTrue(entered.wait(1))
                result=json.loads(next(m['content'] for m in reversed(request['messages']) if m['role']=='tool'))
                self.assertTrue(result['execution_pending'])
                release.set()
                return reply(calls=[call('wait',name='wait_execution',arguments=json.dumps({'execution_id':result['execution_id'],'wait_seconds':3}))])
            return reply('done')
        client.chat.completions.create.side_effect=respond
        (self.root/'source.txt').write_text('evidence')
        with HarnessSession(client,'fixture',self.root,hooks=hooks) as session:
            self.assertEqual(session.run_user('Read evidence'),'done')

    def test_readonly_api_view_cannot_stop_execution(self):
        from homebrew_harness.api.executions import Executions
        from types import SimpleNamespace
        manager=Operations(self.root,'lead:owner')
        self.addCleanup(manager.close)
        handle=manager.start('fixture',{},lambda:'ok')
        handle.wait(2,until='settled')
        view=Executions(SimpleNamespace(changed=threading.Event()),manager.store,manager.owner,control=False)
        with self.assertRaisesRegex(ValueError,'read-only'):
            view.get(handle.id).stop()

    def test_terminal_approval_arriving_after_model_reply_is_serviced(self):
        from homebrew_harness.runtime.hooks import HookRegistry, permission_hook
        gate, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        hooks = HookRegistry()
        def hold(event):
            if event.name == 'bash':
                gate.set()
                release.wait(3)
        hooks.register_hook('PreToolUse', hold)
        hooks.register_hook('PreToolUse', permission_hook)
        client, _ = fake_client([])
        seen = []
        def respond(**request):
            seen.append(request)
            if len(seen) == 1:
                return reply(calls=[call('pending', name='bash', arguments=json.dumps({
                    'command': 'printf approved', 'execution': {'wait_seconds': 0}
                }))])
            if len(seen) == 2:
                self.assertTrue(gate.wait(1))
                release.set()
                end = time.monotonic() + 1
                while not session.operations._terminal_ready and time.monotonic() < end:
                    time.sleep(.01)
                self.assertTrue(session.operations._terminal_ready, session.operations.snapshots())
            return reply('done')
        client.chat.completions.create.side_effect = respond
        with patch('homebrew_harness.security.permissions.sys.stdin.isatty', return_value=True), patch('builtins.input', return_value='y') as approve:
            with HarnessSession(client, 'fixture', self.root, hooks=hooks, turn_timeout=3) as session:
                self.assertEqual(session.run_user('Run the command'), 'done')
                self.assertFalse(session.operations.pending())
        approve.assert_called_once()

    def test_team_member_selects_and_waits_for_its_test(self):
        from homebrew_harness.orchestration.teams import TeamManager, TeammateRuntime
        from homebrew_harness.runtime.turns import run_member_loop
        from homebrew_harness.context.skill_loader import SkillLoader
        manager=TeamManager(self.root,lambda member:'done')
        self.addCleanup(manager.close)
        manager.test_runner=self.tests
        member=TeammateRuntime(manager,'coder','implementer','Test the module',False)
        manager.members['coder']=member
        task=manager.store.create('Test module')
        member.bind(member.store.claim(task.id))
        client,_=self.client()
        with noninteractive_permissions():
            result=run_member_loop(member,client=client,model='fixture',hooks=create_hooks(),skill_loader=SkillLoader(self.root/'skills'))
        self.assertEqual(result,'done')
        self.assertFalse(member.operations.pending())
        receipt=next(r['result'] for r in member.operations.snapshots() if r['name']=='run_tests')
        self.assertEqual(receipt['role'],'teammate')
        self.assertEqual(receipt['task_id'],task.id)

    def test_workflow_call_returns_control_while_native_work_continues(self):
        from homebrew_harness.orchestration.workflow_runtime import WorkflowRuntime,WorkflowRegistry,WorkflowSpec
        from homebrew_harness.runtime.capabilities import Capabilities
        async def script(ctx,args):
            await asyncio.sleep(.2)
            return {'finished':True}
        from tests.orchestration.test_workflow import Runner
        runtime=WorkflowRuntime(self.root,Runner(),WorkflowRegistry([WorkflowSpec('slow','Slow fixture',script,{'type':'object','properties':{},'required':[]})]))
        self.addCleanup(runtime.close)
        client,_=fake_client([])
        requests=[]
        def respond(**request):
            requests.append(request)
            if len(requests)==1:
                return reply(calls=[call('workflow',name='workflow',arguments=json.dumps({'name':'slow','args':{},'execution':{'wait_seconds':0}}))])
            if len(requests)==2:
                result=json.loads(next(m['content'] for m in reversed(request['messages']) if m['role']=='tool'))
                self.assertTrue(result['execution_pending'])
                return reply(calls=[call('wait',name='wait_execution',arguments=json.dumps({'execution_id':result['execution_id'],'wait_seconds':3}))])
            return reply('done')
        client.chat.completions.create.side_effect=respond
        with HarnessSession(client,'fixture',self.root,workflow_runtime=runtime,capabilities=Capabilities(('workflow',))) as session:
            self.assertEqual(session.run_user('Run a slow workflow'),'done')
        self.assertGreaterEqual(len(requests),3)

    def test_team_message_wakes_member_waiting_on_a_long_test(self):
        from homebrew_harness.orchestration.teams import TeamManager, TeammateRuntime
        from homebrew_harness.runtime.turns import run_member_loop
        from homebrew_harness.context.skill_loader import SkillLoader
        path = self.root / 'tests/test_slow.py'
        path.write_text(path.read_text().replace('time.sleep(.25)', 'time.sleep(20)'))
        manager = TeamManager(self.root, lambda member: 'done')
        self.addCleanup(manager.close)
        manager.test_runner = self.tests
        member = TeammateRuntime(manager, 'coder', 'implementer', 'Run the long test', False)
        manager.members['coder'] = member
        task = manager.store.create('Test module')
        member.bind(member.store.claim(task.id))
        client, _ = fake_client([])
        seen, waiting = [], threading.Event()
        identity = None
        direction = 'Stop the test and report cancellation.'
        def respond(**request):
            nonlocal identity
            seen.append(request)
            if len(seen) == 1:
                return reply(calls=[call('long', name='run_tests', arguments=json.dumps({
                    'name': 'quality', 'selection': {'targets': ['tests.test_slow.Work.test_wait']},
                    'execution': {'wait_seconds': 0}
                }))])
            if len(seen) == 2:
                result = json.loads(next(m['content'] for m in reversed(request['messages']) if m['role'] == 'tool'))
                identity = result['execution_id']
                waiting.set()
                return reply('Waiting for the test.')
            if len(seen) == 3:
                self.assertTrue(any(direction in (m.get('content') or '') for m in request['messages']))
                return reply(calls=[call('stop', name='stop_execution', arguments=json.dumps({'execution_id': identity}))])
            return reply('Cancelled the test.')
        client.chat.completions.create.side_effect = respond
        def message():
            if waiting.wait(3):
                time.sleep(.1)
                manager.bus.send('lead', 'coder', direction, 'message')
        sender = threading.Thread(target=message, daemon=True)
        sender.start()
        guard = threading.Timer(6, self.tests.close)
        guard.start()
        self.addCleanup(guard.cancel)
        started = time.monotonic()
        with noninteractive_permissions():
            result = run_member_loop(member, client=client, model='fixture', hooks=create_hooks(),
                                     skill_loader=SkillLoader(self.root / 'skills'))
        sender.join(1)
        self.assertEqual(result, 'Cancelled the test.')
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(member.operations.get(identity)['state'], 'cancelled')

    def test_host_stop_uses_durable_control_without_cancelling_the_observer(self):
        from homebrew_harness.api.executions import Executions
        from homebrew_harness.execution.background import ShellRunner
        from types import SimpleNamespace
        manager=Operations(self.root,'lead:owner')
        self.addCleanup(manager.close)
        runner=ShellRunner()
        self.addCleanup(runner.close)
        handle=manager.start('bash',{},lambda:runner.run([sys.executable,'-c','import time;time.sleep(20)'],self.root,{},None))
        handle.wait(.05,until='settled')
        views=Executions(SimpleNamespace(changed=threading.Event()),manager.store,manager.owner)
        receipt=views.get(handle.id).stop(request_id='operator-stop')
        self.assertEqual(receipt['state'],'accepted')
        self.assertTrue(handle.wait(3,until='settled').condition_met)
        self.assertEqual(handle.snapshot().state,'cancelled')

    def test_host_input_reports_application_and_replays_after_settlement(self):
        from homebrew_harness.api.executions import Executions
        from homebrew_harness.execution.background import ShellRunner
        from tests.runtime.test_interaction import eventually
        from types import SimpleNamespace
        manager = Operations(self.root, 'lead:input')
        self.addCleanup(manager.close)
        runner = ShellRunner()
        self.addCleanup(runner.close)
        def work():
            result = runner.run([sys.executable, '-u', '-c', 'print(input(),flush=True)'], self.root, {}, None)
            return {'stdout': result.stdout, 'exit_code': result.returncode}
        handle = manager.start('bash', {}, work)
        eventually(lambda: manager.get(handle.id)['capabilities']['can_send_input'])
        operation = Executions(SimpleNamespace(changed=threading.Event()), manager.store, manager.owner).get(handle.id)
        first = operation.input('hello\n', request_id='same-input')
        self.assertTrue(handle.wait(3, until='settled').condition_met)
        inputs = eventually(lambda: operation.snapshot().details.get('inputs'))
        eventually(lambda: operation.snapshot().details['inputs'][first['id']]['state'] == 'applied')
        self.assertNotIn('text', inputs[first['id']])
        self.assertEqual(operation.input('hello\n', request_id='same-input')['id'], first['id'])
        with self.assertRaisesRegex(ValueError, 'different text'):
            operation.input('changed\n', request_id='same-input')
        self.assertEqual(handle.result()['stdout'], 'hello\n')

    def test_model_input_permission_uses_the_process_working_directory(self):
        from tests.runtime.test_interaction import eventually
        root = self.root / 'assigned'
        root.mkdir()
        manager = Operations(self.root, 'lead:scoped-input')
        self.addCleanup(manager.close)
        runner = ShellRunner()
        self.addCleanup(runner.close)
        def work():
            result = runner.run([sys.executable, '-u', '-c', 'print(input(),flush=True)'], root, {}, None)
            return {'stdout': result.stdout}
        handle = manager.start('bash', {}, work)
        eventually(lambda: manager.get(handle.id)['capabilities']['can_send_input'])
        with patch('homebrew_harness.security.permissions.check_permission', return_value=PermissionDecision('allow', 'test')) as permission:
            manager.input(handle.id, 'scoped\n', 'model-input')
        permission.assert_called_once_with('bash', {'command': 'scoped\n'}, root)
        self.assertTrue(handle.wait(3, until='settled').condition_met)
        self.assertEqual(handle.result()['stdout'], 'scoped\n')
