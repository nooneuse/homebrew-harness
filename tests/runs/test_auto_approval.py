"""Run automatic decisions retain the durable execute_approved contract."""

import json
import unittest
import threading
import time
from unittest.mock import patch

from homebrew_harness.runs.spec import RunSpec, DEFAULT_TOOLS
from tests.runs import test_controller as helpers
from tests.support.clients import reply, call


class RunAutoApproval(unittest.TestCase):
    setUp = helpers.RunTests.setUp
    controller = helpers.RunTests.controller

    def test_tool_request_is_reviewed_and_published_to_native_request(self):
        approved = {
            "decision": "approve",
            "reason": "Exact requested command",
            "risk": "low",
        }
        spec = RunSpec(
            "Print ok",
            tools=tuple(set(DEFAULT_TOOLS) | {"bash"}),
            approval_review={"mode": "auto_review"},
            episode_model_calls=3,
        )
        control, session, client, payloads = self.controller(
            [
                reply(calls=[call("run", command="printf ok")]),
                reply(json.dumps(approved)),
            ],
            spec,
        )
        outcome = control.execute()
        self.assertEqual(outcome.status, "waiting")
        requests = control.requests.records(control.id)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["state"], "approved")
        self.assertEqual(requests[0]["arguments"], {"command": "printf ok"})
        review_input = next(
            value for value in (
                json.loads(message["content"])
                for message in payloads[1] if message["role"] == "user"
            ) if "host_context" in value
        )
        self.assertEqual(review_input["request"]["id"], requests[0]["id"])
        scope = review_input["host_context"]["host_scope"]
        self.assertEqual(scope["write_paths"], list(spec.write_paths))
        self.assertEqual(scope["bash_backend"]["mode"], "host")
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertEqual(
            next(iter(session.runtime_store.items("approval_reviews").values()))[
                "state"
            ],
            "approved",
        )

    def test_unknown_review_remains_pending_for_operator(self):
        unknown = {
            "decision": "human_required",
            "reason": "Insufficient context",
            "risk": "medium",
        }
        spec = RunSpec(
            "Consider command",
            tools=tuple(set(DEFAULT_TOOLS) | {"bash"}),
            approval_review={"mode": "auto_review"},
        )
        control, session, client, payloads = self.controller(
            [
                reply(calls=[call("run", command="printf ok")]),
                reply(json.dumps(unknown)),
            ],
            spec,
        )
        outcome = control.execute()
        self.assertEqual(control.requests.records(control.id)[0]["state"], "pending")
        self.assertEqual(outcome.reason_code, "approval_required")

    def test_auto_approval_survives_resume_and_is_consumed_once(self):
        from tests.runs.test_human_control import ManagedRunFixture

        approved = {
            "decision": "approve",
            "reason": "Exact requested output",
            "risk": "low",
        }
        spec = RunSpec(
            "Append ok to output.txt",
            tools=tuple(set(DEFAULT_TOOLS) | {"bash"}),
            approval_review={"mode": "auto_review"},
        )
        c, s, _, _ = self.controller(
            [
                reply(calls=[call("ask", command="printf ok >> output.txt")]),
                reply(json.dumps(approved)),
            ],
            spec,
        )
        self.assertEqual(c.execute().status, "waiting")
        request = c.requests.records(c.id)[0]
        ManagedRunFixture.close(self, c, s)
        execute = lambda identity: reply(
            calls=[
                call(
                    identity,
                    name="execute_approved",
                    arguments=json.dumps({"request_id": request["id"]}),
                )
            ]
        )
        resumed, session, client, _ = self.controller(
            [execute("accepted"), execute("repeat"), reply("Done")], identity=c.id
        )
        self.assertEqual(resumed.execute().status, "completed")
        self.assertEqual((self.root / "output.txt").read_text(), "ok")
        saved = resumed.requests.get(c.id, request["id"])
        self.assertEqual(saved["state"], "consumed")
        self.assertTrue(saved["operation_id"].endswith(":accepted"))
        self.assertEqual(len(session.runtime_store.items("approval_reviews")), 1)
        self.assertEqual(client.chat.completions.create.call_count, 3)


class MemberAutoApproval(unittest.TestCase):
    from tests.runs.test_p2_authorization import TestGrantTests as _fixture

    setUp = _fixture.setUp
    setup_run = _fixture.setup_run
    wait = _fixture.wait

    def test_requester_binding_reviews_member_renewal_and_same_member_continues(self):
        c, s, client = self.setup_run(
            approval_review={"mode": "auto_review", "model_source": "requester"}
        )
        task = c.team_control.task_for("alpha")
        steps = []
        reviewed = []

        def respond(**kwargs):
            if any(
                "host's independent approval reviewer" in str(m.get("content"))
                for m in kwargs["messages"]
                if m["role"] == "system"
            ):
                reviewed.append(kwargs)
                return reply(
                    json.dumps(
                        {
                            "decision": "approve",
                            "reason": "Registered test in assigned work",
                            "risk": "low",
                        }
                    )
                )
            n = len(steps)
            steps.append(n)
            name, args = (
                ("write_file", {"path": "alpha.txt", "content": "alpha done"})
                if n == 0
                else ("run_tests", {"name": "check"})
                if n < 4
                else ("complete_task", {"task_id": task.id})
            )
            return reply(
                calls=[call("step" + str(n), name=name, arguments=json.dumps(args))]
            )

        client.chat.completions.create.side_effect = respond
        args = c.team_control.arguments("alpha")
        c.team_control.authorize(args)
        s.team.spawn(**args)
        member = s.team.members["alpha"]
        thread = member.thread
        self.wait(lambda: s.workspace.store.load(task.id).status == "completed")
        self.assertIs(member.thread, thread)
        self.assertEqual(s.tests.runs, 2)
        self.assertEqual(len(reviewed), 1)
        request = c.requests.records(c.id)[0]
        self.assertEqual(request["kind"], "test_grant")
        self.assertEqual(request["state"], "consumed")
        audit = next(iter(s.runtime_store.items("approval_reviews").values()))
        self.assertEqual(audit["reviewer_binding"], member.model_binding.identity())
        self.assertEqual(self.store.get(c.id)["test_grants"]["check"]["max_runs"], 1)


class NativeReviewLifecycleTests(unittest.TestCase):
    setUp = helpers.RunTests.setUp
    controller = helpers.RunTests.controller

    def delayed_review(self, *, verdict='approve', approval_seconds=60):
        from homebrew_harness.execution.test_runner import TestRunner, TestRecipe
        import sys
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        runner = TestRunner(self.root, [TestRecipe('quality', (sys.executable, '-I', '-c', 'print("ok")'))])
        self.addCleanup(runner.close)
        spec = RunSpec('Run the quality test', approval_review={'mode': 'auto_review', 'timeout_seconds': 5}, approval_seconds=approval_seconds)
        control, session, client, payloads = self.controller([], spec=spec, test_runner=runner)
        def respond(**kwargs):
            if 'independent approval reviewer' in kwargs['messages'][0]['content']:
                entered.set(); release.wait(5)
                return reply(json.dumps({'decision': verdict, 'reason': 'Exact registered invocation.'}))
            if any(row['state'] == 'denied' for row in control.requests.records(control.id)):
                return reply('The test request was denied; no test was executed.')
            return reply(calls=[call('test', name='run_tests', arguments=json.dumps({'name': 'quality', 'execution': {'mode': 'interactive', 'wait_seconds': .02}}))])
        client.chat.completions.create.side_effect = respond
        outcomes = []
        worker = threading.Thread(target=lambda: outcomes.append(control.execute()))
        worker.start()
        def settle_worker():
            if control.store.get(control.id)["status"] not in {"completed", "cancelled", "failed"}:
                control.store.control(control.id, "cancel")
            release.set()
            worker.join(5)
            self.assertFalse(worker.is_alive())
        self.addCleanup(settle_worker)
        self.assertTrue(entered.wait(3))
        return control, session, worker, outcomes, release, runner

    def test_slow_auto_review_survives_short_observation_and_episode_return(self):
        control, session, worker, outcomes, release, runner = self.delayed_review()
        time.sleep(.1)
        self.assertTrue(worker.is_alive(), 'Episode returned while native review was still active.')
        self.assertFalse(session._closed)
        request = control.requests.records(control.id)[0]
        self.assertEqual(request['state'], 'pending')
        release.set();worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(control.requests.get(control.id, request['id'])['state'], 'approved')
        self.assertEqual(next(iter(session.runtime_store.items('approval_reviews').values()))['state'], 'approved')
        self.assertEqual(runner.runs, 0, 'Review must not execute its own proposed test.')

    def test_real_user_pause_cancels_review_and_preserves_pending_request(self):
        control, session, worker, outcomes, release, runner = self.delayed_review()
        control.store.control(control.id, 'pause')
        worker.join(3);release.set();worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcomes[-1].status, 'paused')
        self.assertNotEqual(control.requests.records(control.id)[0]['state'], 'approved')
        self.assertEqual(runner.runs, 0)

    def test_explicit_short_request_expiry_remains_effective(self):
        control, session, worker, outcomes, release, runner = self.delayed_review(approval_seconds=1)
        worker.join(2);release.set();worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertNotEqual(outcomes[-1].status, 'completed')
        self.assertEqual(control.requests.records(control.id)[0]['state'], 'expired')
        self.assertEqual(runner.runs, 0)

    def test_human_override_supersedes_live_model_review_without_replaying_test(self):
        control, session, worker, outcomes, release, runner = self.delayed_review()
        request = control.requests.records(control.id)[0]
        control.requests.decide(control.id, request['id'], request['revision'], 'deny')
        worker.join(3);release.set();worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(control.requests.get(control.id, request['id'])['state'], 'denied')
        self.assertNotEqual(next(iter(session.runtime_store.items('approval_reviews').values()))['state'], 'approved')
        self.assertEqual(runner.runs, 0)

    def test_default_request_window_covers_review_and_closing_allowance(self):
        from homebrew_harness.security.approval_review import ReviewConfig
        from homebrew_harness.configuration import DEFAULT_SHUTDOWN_SECONDS
        self.assertIsNone(RunSpec("Task").approval_seconds)
        self.assertIsNone(ReviewConfig().timeout_seconds)

    def test_request_publication_window_is_owned_before_request_row_exists(self):
        control, session, client, payloads = self.controller([], spec=RunSpec('Task', approval_review={'mode': 'auto_review'}))
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def create():
            with control.native_reviews.creating('tool'):
                entered.set(); release.wait(3)
        owner = threading.Thread(target=create);owner.start()
        self.assertTrue(entered.wait(2))
        waiter = threading.Thread(target=lambda: (control.native_reviews.wait(), finished.set()));waiter.start()
        try:
            self.assertFalse(finished.wait(.1))
            self.assertEqual(control.requests.records(control.id), [])
        finally:
            release.set();owner.join(3);waiter.join(3)
        self.assertTrue(finished.is_set())

    def test_episode_timeout_does_not_cancel_a_still_valid_auto_review(self):
        control, session, worker, outcomes, release, runner = self.delayed_review()
        from homebrew_harness.execution.model_gateway import Deadline
        control.episode_deadline = Deadline.after(0, source='episode')
        time.sleep(.1)
        self.assertTrue(worker.is_alive())
        release.set();worker.join(5)
        self.assertEqual(control.requests.records(control.id)[0]['state'], 'approved')
        self.assertEqual(runner.runs, 0)

    def test_another_pending_permission_does_not_abandon_active_auto_review(self):
        control, session, worker, outcomes, release, runner = self.delayed_review()
        control.permission_required = True
        control.permission_reason = 'approval_required'
        time.sleep(.1)
        self.assertTrue(worker.is_alive())
        release.set();worker.join(5)
        self.assertEqual(control.requests.records(control.id)[0]['state'], 'approved')
        self.assertEqual(runner.runs, 0)
