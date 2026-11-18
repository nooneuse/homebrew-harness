import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from homebrew_harness.execution.operations import Operations, emit_output, options
from homebrew_harness.execution.background import ShellRunner
from homebrew_harness.execution.model_gateway import Deadline
from homebrew_harness.execution.test_runner import TestRecipe, TestRunner
from homebrew_harness.execution.test_selection import LAUNCHER, validate_selection


class OwnedExecutions(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.manager = Operations(self.root, 'lead:test')
        self.addCleanup(self.manager.close)

    def test_wait_returns_without_stopping_and_output_has_cursor(self):
        release = threading.Event()
        self.addCleanup(release.set)
        def work():
            emit_output('stdout', 'ready\n')
            release.wait(3)
            emit_output('stdout', 'done\n')
            return {'ok': True}
        handle = self.manager.start('fixture', {}, work)
        report = handle.wait(.05, until='settled')
        self.assertFalse(report.condition_met)
        first = handle.output()
        self.assertEqual(first['content'], 'ready\n')
        release.set()
        self.assertTrue(handle.wait(3, until='settled').condition_met)
        self.assertEqual(handle.output(first['next_cursor'])['content'], 'done\n')
        self.assertEqual(handle.result(), {'ok': True})

    def test_duplicate_start_never_runs_twice_and_rejects_changed_arguments(self):
        calls = []
        first = self.manager.start('fixture', {'value': 1}, lambda: calls.append(1), request_id='same')
        second = self.manager.start('fixture', {'value': 1}, lambda: calls.append(2), request_id='same')
        first.wait(3, until='settled')
        self.assertEqual(first.id, second.id)
        self.assertEqual(calls, [1])
        with self.assertRaisesRegex(ValueError, 'different arguments'):
            self.manager.start('fixture', {'value': 2}, lambda: None, request_id='same')

    def test_stop_is_requested_before_cooperative_cleanup(self):
        runner = ShellRunner()
        self.addCleanup(runner.close)
        def work():
            result = runner.run([sys.executable, '-u', '-c', 'import time;print("ready",flush=True);time.sleep(20)'], self.root, {}, None)
            return {'exit_code': result.returncode}
        handle = self.manager.start('bash', {}, work)
        handle.wait(.05, until='settled')
        self.assertEqual(handle.stop(request_id='cancel')['state'], 'stop_requested')
        self.assertTrue(handle.wait(3, until='settled').condition_met)
        self.assertEqual(handle.snapshot().state, 'cancelled')
        self.assertTrue(runner.wait_closed(1))

    def test_parent_deadline_stops_process_without_observation_timeout(self):
        runner = ShellRunner()
        self.addCleanup(runner.close)
        handle = self.manager.start('bash', {}, lambda: runner.run([sys.executable, '-c', 'import time;time.sleep(20)'], self.root, {}, None), deadline=Deadline.after(.15))
        self.assertFalse(handle.wait(.01, until='settled').condition_met)
        self.assertTrue(handle.wait(3, until='settled').condition_met)
        self.assertTrue(runner.wait_closed(1))

    def test_input_registration_failure_still_cleans_up_launched_process(self):
        runner = ShellRunner()
        self.addCleanup(runner.close)
        with patch('homebrew_harness.execution.operations.bind_input', side_effect=ValueError('registration failed')):
            handle = self.manager.start('bash', {}, lambda: runner.run(
                [sys.executable, '-c', 'import time;time.sleep(20)'], self.root, {}, None))
            self.assertTrue(handle.wait(3, until='settled').condition_met)
        self.assertEqual(handle.snapshot().state, 'failed')
        self.assertTrue(runner.wait_closed(1))

    def test_sibling_cannot_observe_or_control_another_owner(self):
        handle = self.manager.start('fixture', {}, lambda: 'done')
        other = Operations(self.root, 'lead:other')
        self.addCleanup(other.close)
        with self.assertRaisesRegex(ValueError, 'not owned'):
            other.stop(handle.id)
        child = Operations(self.root, 'sub:child', parent=self.manager)
        self.addCleanup(child.close)
        child_handle = child.start('fixture', {}, lambda: 'child')
        self.assertEqual(self.manager.get(child_handle.id)['owner'], 'sub:child')

    def test_modes_and_finite_limits(self):
        self.assertIsNone(options()['timeout_seconds'])
        for value in ({'mode': 'one_shot', 'wait_seconds': 1}, {'wait_seconds': float('inf')}, {'timeout_seconds': 0}, {'timeout_seconds': True}):
            with self.assertRaises(ValueError):
                options(value)

    def test_control_request_length_is_rejected_before_mutating_execution(self):
        from functools import partial
        from homebrew_harness.execution.model_protocol import ToolInvocation
        from homebrew_harness.tools.dispatch import execute_tool
        from homebrew_harness.runtime.hooks import create_hooks
        handle = self.manager.start('fixture', {}, lambda: 'done')
        handle.wait(3, until='settled')
        before = handle.snapshot().revision
        result = json.loads(execute_tool(ToolInvocation('stop', 'stop_execution', json.dumps({
            'execution_id': handle.id, 'request_id': 'x' * 201
        })), self.root, hooks=create_hooks(), handlers={
            'stop_execution': partial(self.manager.run_tool, 'stop_execution')
        }, operations=self.manager))
        self.assertIn('at most 200 characters', result['error'])
        self.assertEqual(handle.snapshot().revision, before)


class SelectedTests(unittest.TestCase):
    def test_default_limits_and_local_selection_do_not_verify_full_suite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root/'tests').mkdir()
            (root/'tests/__init__.py').write_text('')
            (root/'tests/test_example.py').write_text('import unittest\nclass Sample(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n def test_bad(self): self.fail("expected failure")\n')
            recipe = TestRecipe('unit', (sys.executable, '-I', '-c', LAUNCHER), selector='unittest')
            self.assertIsNone(recipe.timeout)
            self.assertIsNone(recipe.cpu_seconds)
            runner = TestRunner(root, [recipe])
            self.addCleanup(runner.close)
            runner.approve('unit')
            self.assertIsNone(runner.total_seconds)
            partial = runner.run('unit', selection={'targets': ['tests.test_example.Sample.test_ok']})
            self.assertEqual(partial['status'], 'passed')
            self.assertEqual(partial['test_scope'], 'selected')
            self.assertFalse(runner.verify('unit', root)['ok'])
            self.assertTrue(runner.verify('unit', root, selection={'targets': ['tests.test_example.Sample.test_ok']})['ok'])
            full = runner.run('unit')
            self.assertEqual(full['status'], 'failed')
            self.assertEqual(full['test_scope'], 'full')

    def test_selector_contract_rejects_empty_outside_and_unknown_framework(self):
        for value, framework in [({'targets': []}, 'unittest'), ({'targets': ['os.system']}, 'unittest'), ({'targets': ['tests/../secret.py']}, 'pytest'), ({'targets': ['--help']}, 'pytest'), ({'targets': ['tests.test_x'], 'filter': '-x'}, 'unittest'), ({'targets': ['tests']}, None)]:
            with self.assertRaises(ValueError):
                validate_selection(value, framework)

    def test_explicit_aggregate_limit_is_still_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = TestRunner(Path(directory), [TestRecipe('unit', (sys.executable, '-c', 'print(1)'))], total_seconds=.05)
            self.addCleanup(runner.close)
            runner.approve('unit')
            runner.seconds = .05
            with self.assertRaisesRegex(ValueError, 'time limit'):
                runner.run('unit')

    def test_exact_approval_is_private_to_its_invocation_and_not_exported(self):
        from homebrew_harness.execution.test_runner import TestContext
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            runner = TestRunner(root, [TestRecipe('unit', (sys.executable, '-c', 'pass'))])
            self.addCleanup(runner.close)
            runner.approve('unit', max_runs=1, private=True)
            self.assertEqual(runner.permission('unit', root, TestContext())[0], 'allow')
            other = []
            thread = threading.Thread(target=lambda: other.append(runner.permission('unit', root, TestContext())[0]))
            thread.start(); thread.join(3)
            self.assertEqual(other, ['ask'])
            self.assertEqual(runner.handoff_state()['grants'], [])
            self.assertEqual(runner.run('unit')['status'], 'passed')

    def test_host_authorization_changes_do_not_wait_for_a_running_test(self):
        from tests.runtime.test_interaction import eventually
        with tempfile.TemporaryDirectory() as directory:
            runner = TestRunner(Path(directory), [TestRecipe('unit', (sys.executable, '-c', 'import time;time.sleep(20)'))])
            self.addCleanup(runner.close)
            runner.approve('unit', max_runs=1)
            results = []
            worker = threading.Thread(target=lambda: results.append(runner.run('unit')), daemon=True)
            worker.start()
            guard = threading.Timer(8, runner.close)
            guard.start()
            try:
                activity = eventually(runner.activity, 2)
                eventually(lambda: bool(runner._processes), 2)
                started = time.monotonic()
                runner.approve('unit', max_runs=1)
                runner.revoke('unit')
                self.assertLess(time.monotonic() - started, 1)
                self.assertTrue(worker.is_alive())
                runner.stop(activity['id'])
                worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(results[0]['status'], 'cancelled')
            finally:
                guard.cancel()
                runner.close()
                worker.join(3)

    def test_later_selected_failure_does_not_hide_behind_full_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();(root/'tests').mkdir();(root/'tests/__init__.py').write_text('')
            (root/'tests/test_scope.py').write_text('import unittest,json,os\nclass Scope(unittest.TestCase):\n def test_mode(self): self.assertIsNone(json.loads(os.environ["HARNESS_TEST_SELECTION"])["selection"])\n')
            recipe=TestRecipe('unit',(sys.executable,'-I','-c',LAUNCHER),selector='unittest')
            runner=TestRunner(root,[recipe]);self.addCleanup(runner.close);runner.approve('unit')
            self.assertEqual(runner.run('unit')['status'],'passed')
            self.assertTrue(runner.verify('unit',root)['ok'])
            self.assertEqual(runner.run('unit',selection={'targets':['tests.test_scope.Scope.test_mode']})['status'],'failed')
            self.assertFalse(runner.verify('unit',root)['ok'])
