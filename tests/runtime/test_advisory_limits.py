"""Defaults report progress; only explicit budgets and real contracts stop work."""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from homebrew_harness.runtime.limits import RuntimeLimits, limits_scope, current_limits
from homebrew_harness.runtime.interaction_config import InteractionConfig
from homebrew_harness.runs.spec import RunSpec
from homebrew_harness.execution.test_runner import TestRunner, TestRecipe, test_context
from homebrew_harness.security.approval_review import ReviewConfig
from homebrew_harness.orchestration.workflow_runtime import WorkflowLimits
from tests.runtime import test_loop as fixture
from tests.support.clients import reply, call, fake_client


class AdvisoryDefaultsTests(unittest.TestCase):
    def test_new_hosts_and_runs_do_not_have_implicit_work_budgets(self):
        config = InteractionConfig()
        self.assertIsNone(config.max_turns)
        self.assertIsNone(config.total_seconds)
        self.assertIsNone(config.turn_seconds)
        spec = RunSpec('Continue until the requested result is achieved.')
        for name in ('total_seconds', 'retries', 'repeat_limit', 'replan_limit'):
            self.assertIsNone(getattr(spec, name))
        self.assertIsNone(ReviewConfig().max_turns)
        self.assertIsNone(ReviewConfig().timeout_seconds)
        self.assertIsNone(WorkflowLimits().timeout_seconds)

    def test_explicit_limits_remain_finite_and_are_not_silently_waived(self):
        spec = RunSpec('Bounded work', total_seconds=12, retries=2, repeat_limit=3, replan_limit=1)
        self.assertEqual(spec.total_seconds, 12)
        self.assertEqual(spec.retries, 2)
        self.assertEqual(InteractionConfig(max_turns=200).max_turns, 200)
        self.assertEqual(ReviewConfig(max_turns=20).max_turns, 20)
        for value in (True, 0, -1, float('nan'), float('inf')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                RunSpec('Invalid explicit budget', total_seconds=value)

    def test_limits_scopes_are_independent_and_restored(self):
        default = current_limits()
        with limits_scope({'file_read_chars': 24000, 'run_retry_windows': 64}):
            self.assertEqual(current_limits().file_read_chars, 24000)
            with limits_scope({'file_read_chars': 3000}):
                self.assertEqual(current_limits().file_read_chars, 3000)
            self.assertEqual(current_limits().file_read_chars, 24000)
        self.assertEqual(current_limits(), default)

    def test_default_tests_continue_past_the_old_recipe_and_runner_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = TestRunner(directory, [TestRecipe('sample', (sys.executable, '-c', 'print("done")'))])
            self.addCleanup(runner.close)
            runner.approve('sample', role='lead')
            with test_context('lead'):
                for _ in range(22):
                    self.assertEqual(runner.run('sample')['status'], 'passed')
            self.assertEqual(runner.runs, 22)

    def test_explicit_test_budget_still_blocks_a_new_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = TestRunner(directory, [TestRecipe('sample', (sys.executable, '-c', 'pass'))], max_runs=1)
            self.addCleanup(runner.close)
            runner.approve('sample', role='lead')
            with test_context('lead'):
                self.assertEqual(runner.run('sample')['status'], 'passed')
                with self.assertRaises(Exception) as error:
                    runner.run('sample')
                self.assertIn('limit', str(error.exception).lower())


class AdvisoryLoopTests(unittest.TestCase):
    setUp = fixture.AgentLoopTests.setUp
    run_loop = fixture.AgentLoopTests.run_loop

    def test_model_continues_past_old_call_limit_and_observes_advice(self):
        (self.workdir / 'source.txt').write_text('source evidence\n')
        responses = [reply(calls=[call(str(index), name='read_file', arguments=json.dumps({'path': 'source.txt'}))])
                     for index in range(35)]
        client, snapshots = fake_client([*responses, reply('Finished from the evidence.')])
        result = self.run_loop(client)
        self.assertIn('Finished', result)
        self.assertEqual(client.chat.completions.create.call_count, 36)
        self.assertTrue(any(any(m.get('name') == 'execution_advisories' for m in snapshot)
                            for snapshot in snapshots))

    def test_explicit_call_budget_is_still_enforced(self):
        (self.workdir / 'source.txt').write_text('source evidence\n')
        client, _ = fake_client([reply(calls=[call('first', name='read_file', arguments='{"path":"source.txt"}')]), reply('done')])
        with self.assertRaises(Exception) as error:
            self.run_loop(client, max_turns=1)
        self.assertIn('limit', str(error.exception).lower())
