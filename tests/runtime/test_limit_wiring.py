"""Exercise advisory defaults, scoped configuration and exact persisted boundaries."""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from homebrew_harness import Harness, HostConfig
from homebrew_harness.runtime.limits import current_limits, limits_scope
from homebrew_harness.runtime.runtime_control import Workers
from homebrew_harness.storage.sqlite_store import SQLiteDatabase
from homebrew_harness.runtime.turns import run_subagent
from homebrew_harness.runs import RunSpec
from homebrew_harness.runs.team import validate_members
from homebrew_harness.context.todo import TodoManager
from tests.runs import test_controller as run_fixture
from tests.support.clients import call, fake_client, reply


class ScopedLimits(unittest.TestCase):
    def test_new_run_uses_host_scope_but_an_explicit_override_is_independent(self):
        with limits_scope({'file_read_chars': 25000, 'review_page_chars': 19000}):
            inherited = RunSpec('Work in the host scope.')
            explicit = RunSpec('Use the explicit scope.', runtime_limits={'file_read_chars': 9000})
        self.assertEqual(inherited.runtime_limits['file_read_chars'], 25000)
        self.assertEqual(inherited.runtime_limits['review_page_chars'], 19000)
        self.assertEqual(explicit.runtime_limits['file_read_chars'], 9000)
        self.assertEqual(explicit.runtime_limits['review_page_chars'], 10000)
        self.assertEqual(current_limits().file_read_chars, 10000)

    def test_worker_and_host_scopes_do_not_leak_configuration(self):
        values = []
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            client, _ = fake_client([])
            first = Harness(workdir=one, config=HostConfig.from_components(client=client, model='fixture', runtime_limits={'file_read_chars': 17000, 'storage_open_seconds': 7}))
            second = Harness(workdir=two, config=HostConfig.from_components(client=client, model='fixture', runtime_limits={'file_read_chars': 28000, 'storage_open_seconds': 9}))
            workers = Workers()
            for host in (first, second):
                with host.operation():
                    workers.start(lambda: values.append((current_limits().file_read_chars, SQLiteDatabase(one).open_timeout)))
            self.assertTrue(workers.wait(5))
            self.assertEqual(sorted(values), [(17000, 7), (28000, 9)])
            self.assertEqual(current_limits().file_read_chars, 10000)
            first.close(); second.close()
            self.assertTrue(first.wait_closed(5).resources_closed)
            self.assertTrue(second.wait_closed(5).resources_closed)

    def test_subagent_inherits_window_and_call_advisory_then_decides_to_finish(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, 'source.txt').write_text('x' * 12000)
            client, seen = fake_client([reply(calls=[call(str(i), name='read_file', arguments='{"path":"source.txt"}')]) for i in range(3)] + [reply('done')])
            with limits_scope({'file_read_chars': 18000, 'subagent_call_warning': 2}):
                value = json.loads(run_subagent('Read the source and finish.', Path(root), client=client, model='fixture'))
            self.assertEqual(value['status'], 'completed')
            self.assertEqual(client.chat.completions.create.call_count, 4)
            self.assertTrue(any('"recommended_value": 2' in m.get('content', '') for history in seen for m in history if m.get('name') == 'execution_advisories'))
            result = next(m['content'] for m in seen[1] if m.get('role') == 'tool')
            self.assertIn('x' * 12000, result)

    def test_declared_member_count_and_retries_are_open_by_default(self):
        stages = [{'executor': 'member' + str(i), 'write_paths': ['part' + str(i)]} for i in range(6)]
        values = [{'name': 'member' + str(i), 'role': 'coder', 'prompt': 'Complete your scoped work.'} for i in range(6)]
        members = validate_members(values, stages)
        self.assertEqual(len(members), 6)
        self.assertTrue(all(m['retries'] is None for m in members))
        with self.assertRaises(ValueError):
            validate_members(values, stages, {'managed_members': 4})
        self.assertEqual(validate_members([{**v, 'retries': 100} for v in values], stages)[0]['retries'], 100)

    def test_todo_count_is_open_and_explicit_cap_is_still_respected(self):
        manager = TodoManager()
        values = [{'content': str(i), 'status': 'pending'} for i in range(30)]
        manager.update(values)
        with limits_scope({'todo_items': 20}), self.assertRaises(ValueError):
            manager.update(values)
        self.assertEqual(len(manager.items), 30)


class RetryWindows(unittest.TestCase):
    setUp = run_fixture.RunTests.setUp
    controller = run_fixture.RunTests.controller

    def test_open_run_restores_no_deadline_and_crosses_thirty_two_retry_windows(self):
        control, session, _, _ = self.controller([reply('done')], RunSpec('Finish the task.'))
        self.assertIsNone(control.deadline().remaining())
        self.assertEqual(control.execute().status, 'completed')
        # Model-independent persisted retry admission, with cleanup genuinely settled.
        session.close(); self.assertTrue(session.wait_closed(5)); control.close_heartbeat()
        self.store.ended(control.header, cleanup=True)
        for index in range(34):
            self.store.update(control.header, {'status': 'paused', 'finalized_at': None})
            result = self.store.retry(control.id, request_id='window-' + str(index))
            self.assertIsNone(result['deadline_at'])
        self.assertEqual(len(result['retry_windows']), 34)
        self.assertEqual(result['id'], control.id)


class GoalBudgetDefaults(unittest.TestCase):
    def test_long_condition_and_model_reason_are_not_implicit_failure_criteria(self):
        from homebrew_harness.orchestration.goal_loop import GoalController, GoalEvaluation
        from tests.orchestration import test_goal_loop as fixture
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = temporary.name
        evaluation = fixture.Evaluator(lambda packet: GoalEvaluation(True, 'Explained from the supplied facts. ' * 40, evidence_ids=('final',)))
        goal = GoalController(root, evaluation)
        self.addCleanup(goal.close)
        condition = ('Complete the agreed requirement. ' * 170).strip()
        goal.set_goal(condition)
        decision = goal.evaluate_after_turn('The agreed requirement is complete.')
        self.assertEqual(decision.action, 'achieved')
        self.assertEqual(goal.store.load(goal.state['goal_id'])['condition'], condition)
        self.assertEqual(evaluation.calls[0]['completion_condition'], condition)

    def test_explicit_condition_cap_rejects_only_the_new_proposal(self):
        import copy
        from homebrew_harness.orchestration.goal_loop import GoalController, GoalError
        from tests.orchestration import test_goal_loop as fixture
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = temporary.name
        goal = GoalController(root, fixture.Evaluator(fixture.final_ok), runtime_limits={'goal_condition_chars': 100})
        self.addCleanup(goal.close)
        goal.set_goal('Current condition.')
        previous = copy.deepcopy(goal.state)
        with self.assertRaises(GoalError):
            goal.set_goal('new condition ' * 20)
        self.assertEqual(goal.state, previous)
