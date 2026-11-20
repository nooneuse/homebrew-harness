"""An Episode boundary retains an owned execution; interruption still ends it."""

import json
import sys
import threading
import unittest
from unittest.mock import patch

from homebrew_harness.execution.test_runner import TestRecipe, TestRunner
from homebrew_harness.runs import RunSpec
from homebrew_harness.runtime.session import HarnessSession
from tests.runs import test_controller as helpers
from tests.runtime.test_interaction import eventually
from tests.support.clients import call, reply


class LiveOperationRuns(unittest.TestCase):
    setUp = helpers.RunTests.setUp
    controller = helpers.RunTests.controller

    def setup_test(self, *, duration=1, wait=.05, episode_seconds=5, calls=1, total_seconds=20, tools=('run_tests',)):
        runner = TestRunner(self.root, [TestRecipe('quality', (
            sys.executable, '-u', '-I', '-c',
            'import time;print("started",flush=True);time.sleep(' + str(duration) + ');print("finished",flush=True)',
        ))])
        self.addCleanup(runner.close)
        spec = RunSpec('Run the registered test and observe its result.', tools=tools,
                       allow_tests=('quality',), total_seconds=total_seconds,
                       episode_seconds=episode_seconds, episode_model_calls=calls,
                       request_seconds=5, heartbeat_seconds=.05, shutdown_seconds=3)
        control, session, client, _ = self.controller([], spec=spec, test_runner=runner)
        self.test_client = client
        steps = []
        def respond(**request):
            steps.append(request)
            if len(steps) == 1:
                execution = {'mode': 'interactive'}
                if wait is not None:
                    execution['wait_seconds'] = wait
                return reply(calls=[call('start', name='run_tests', arguments=json.dumps({
                    'name': 'quality', 'execution': execution,
                }))])
            row = session.operations.snapshots()[0]
            if row['execution_pending']:
                return reply(calls=[call('wait-' + str(len(steps)), name='wait_execution', arguments=json.dumps({
                    'execution_id': row['id'], 'wait_seconds': 3,
                }))])
            self.assertEqual(row['result']['status'], 'passed')
            return reply('The actual test passed.')
        client.chat.completions.create.side_effect = respond
        return control, session, runner, steps

    def assert_live_boundary(self, **options):
        control, session, runner, steps = self.setup_test(**options)
        boundaries = []
        original = HarnessSession._save_checkpoint
        def save(owner, phase='idle', **kwargs):
            if owner is session and phase == 'yielded':
                live = [row for row in owner.recovery_operations() if row['active']]
                if live:
                    boundaries.append(live)
            result = original(owner, phase, **kwargs)
            if owner is session:
                self.assertEqual(owner._recovery_blocked, [])
            return result
        with patch.object(HarnessSession, '_save_checkpoint', save):
            outcome = control.execute()
        self.assertEqual(outcome.status, 'completed', outcome)
        self.assertTrue(boundaries, 'The fixture must reach a boundary with a live accepted operation.')
        rows = session.operations.snapshots()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['state'], 'completed')
        self.assertEqual(rows[0]['result']['status'], 'passed')
        self.assertEqual(rows[0]['effect_state'], 'observed')
        self.assertTrue(rows[0]['cleanup']['complete'])
        self.assertEqual(runner.runs, 1)
        self.assertGreaterEqual(len(steps), 3)
        self.assertGreaterEqual(outcome.episode, 2)

    def test_interactive_test_survives_model_call_boundary_without_relaunch(self):
        self.assert_live_boundary()

    def test_interactive_test_survives_episode_time_boundary(self):
        self.assert_live_boundary(duration=3, wait=1.1, episode_seconds=1, calls=20)

    def test_default_ten_second_observation_preserves_live_test(self):
        self.assert_live_boundary(duration=11, wait=None, episode_seconds=20)

    def test_recoverable_model_error_keeps_the_live_test_available(self):
        from homebrew_harness.execution.model_protocol import ProviderError
        control, session, runner, steps = self.setup_test(
            duration=2, calls=8, tools=('run_tests', 'report_run_blocker'))
        provider = self.test_client
        original = provider.chat.completions.create.side_effect
        failed = []
        def respond(**request):
            if steps and not failed:
                self.assertTrue(session.operations.snapshots()[0]['execution_pending'])
                failed.append(True)
                raise ProviderError('invalid_request', code='data_inspection_failed', message='Output data was rejected.')
            return original(**request)
        with patch.object(provider.chat.completions.create, 'side_effect', respond):
            outcome = control.execute()
        self.assertEqual(outcome.status, 'completed', outcome)
        self.assertEqual(failed, [True])
        self.assertTrue(any(m.get('name') == 'model_error_observation' for m in steps[1]['messages']))
        self.assertEqual(runner.runs, 1)

    def test_real_pause_ends_a_live_test_without_claiming_success(self):
        control, session, runner, _ = self.setup_test(duration=20, calls=1)
        outcomes = []
        worker = threading.Thread(target=lambda: outcomes.append(control.execute()))
        worker.start()
        self.addCleanup(lambda: worker.join(5))
        try:
            eventually(lambda: bool(runner._processes), 3)
            control.store.control(control.id, 'pause')
            worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(outcomes[0].status, 'paused')
        finally:
            session.close()
            self.assertTrue(session.wait_closed(5))
        row = session.operations.snapshots()[0]
        self.assertFalse(row['execution_pending'])
        self.assertEqual(row['effect_state'], 'observed')
        self.assertEqual(row['result']['status'], 'cancelled')
        self.assertNotEqual((row.get('result') or {}).get('status'), 'passed')

    def test_pause_during_receipt_commit_preserves_the_observed_test_result(self):
        control, session, runner, _ = self.setup_test(duration=.1, wait=3, calls=1)
        save = runner._save
        paused = []

        def stop_after_receipt(reference, value):
            save(reference, value)
            if reference.endswith('/receipt.json') and not paused:
                paused.append(True)
                control.store.control(control.id, 'pause')
                # Deliver the accepted pause at the same point as its heartbeat.
                control.host_action = 'pause'
                control.cancel.set()

        with patch.object(runner, '_save', stop_after_receipt):
            outcome = control.execute()
        self.assertEqual(outcome.status, 'paused', outcome)
        self.assertEqual(paused, [True])
        session.close()
        self.assertTrue(session.wait_closed(5))
        row = session.operations.snapshots()[0]
        self.assertFalse(row['execution_pending'])
        self.assertEqual(row['effect_state'], 'observed')
        # The test actually completed before pause; the Run is still paused.
        self.assertEqual(row['result']['status'], 'passed')

    def test_parent_deadline_ends_the_live_test_instead_of_extending_it(self):
        control, session, _, _ = self.setup_test(duration=20, calls=1, total_seconds=2, episode_seconds=1)
        outcome = control.execute()
        self.assertEqual(control.deadline().remaining(), 0)
        self.assertIn(outcome.reason_code, {'deadline_reached', 'recovery_required'})
        session.close()
        self.assertTrue(session.wait_closed(5))
        row = session.operations.snapshots()[0]
        self.assertFalse(row['execution_pending'])
        self.assertNotEqual((row.get('result') or {}).get('status'), 'passed')
        if outcome.reason_code == 'recovery_required':
            self.assertEqual(row['effect_state'], 'unknown')
            self.assertTrue(outcome.required_host_actions)

    def test_exact_approved_bash_survives_a_model_call_boundary(self):
        import shlex
        from tests.runs.test_human_control import ManagedRunFixture
        command = shlex.quote(sys.executable) + ' -u -I -c ' + shlex.quote(
            'import time;print("started",flush=True);time.sleep(1);print("done",flush=True)')
        control, session, _, _ = self.controller([reply(calls=[call('ask', name='bash', arguments=json.dumps({
            'command': command, 'execution': {'wait_seconds': .05},
        }))])], RunSpec('Run this exact local command.', tools=('bash',), episode_model_calls=1, total_seconds=20))
        self.assertEqual(control.execute().status, 'waiting')
        request = control.requests.records(control.id)[0]
        ManagedRunFixture.close(self, control, session)
        control.requests.decide(control.id, request['id'], request['revision'], 'approve')
        resumed, owner, client, _ = self.controller([], identity=control.id)
        seen = []
        def respond(**payload):
            seen.append(payload)
            if len(seen) == 1:
                return reply(calls=[call('approved', name='execute_approved', arguments=json.dumps({'request_id': request['id']}))])
            row = owner.operations.snapshots()[-1]
            if row['execution_pending']:
                return reply(calls=[call('wait', name='wait_execution', arguments=json.dumps({'execution_id': row['id'], 'wait_seconds': 3}))])
            return reply('The approved command finished.')
        client.chat.completions.create.side_effect = respond
        self.assertEqual(resumed.execute().status, 'completed')
        rows = [row for row in owner.operations.snapshots() if row['state'] == 'completed']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['effect_state'], 'observed')
        self.assertEqual(rows[0]['result']['exit_code'], 0)
        self.assertEqual(rows[0]['result']['stdout'], 'started\ndone\n')
        self.assertEqual(resumed.requests.get(resumed.id, request['id'])['state'], 'consumed')


class LiveMCPRuns(unittest.TestCase):
    from tests.runs.test_mcp import RunMCPTests as _fixture
    setUp = _fixture.setUp
    controller = _fixture.controller
    close = _fixture.close

    def test_stdio_mcp_call_survives_a_model_call_boundary(self):
        from dataclasses import replace
        from homebrew_harness.tools.mcp.runtime import ToolPolicy
        self.server = replace(self.server, args=(*self.server.args, '--test-tools'), tools={'delay': ToolPolicy('allow', True)})
        control, session, _ = self.controller([], spec=RunSpec(
            'Observe the selected local MCP call.', mcp={'docs': ['delay']},
            episode_model_calls=1, total_seconds=20))
        provider = session.client._client
        steps = []
        def respond(**request):
            steps.append(request)
            if len(steps) == 1:
                return reply(calls=[call('mcp', name='invoke_mcp_tool', arguments=json.dumps({
                    'tool': 'mcp__docs__delay', 'arguments': {'seconds': 1},
                    'execution': {'wait_seconds': .05},
                }))])
            row = session.operations.snapshots()[0]
            if row['execution_pending']:
                return reply(calls=[call('wait', name='wait_execution', arguments=json.dumps({'execution_id': row['id'], 'wait_seconds': 3}))])
            return reply('The actual MCP call finished.')
        provider.chat.completions.create.side_effect = respond
        outcome = control.execute()
        self.assertEqual(outcome.status, 'completed', outcome)
        rows = session.operations.snapshots()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['state'], 'completed')
        self.assertEqual(rows[0]['effect_state'], 'observed')
        self.assertEqual(session._recovery_blocked, [])
        self.assertGreaterEqual(outcome.episode, 2)
        self.close(control, session)
