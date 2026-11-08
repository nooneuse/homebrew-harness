import unittest

from homebrew_harness.execution.model_gateway import (
    Deadline,
    ModelDeadlineExceeded,
    ModelGateway,
    GatewayLimits,
)
from homebrew_harness.runs import RunSpec
from tests.runs import test_controller as fixture
from tests.support.clients import reply, fake_client


class DeadlineControlTests(unittest.TestCase):
    setUp = fixture.RunTests.setUp
    controller = fixture.RunTests.controller
    write = fixture.RunTests.write

    def spec(self):
        return RunSpec(
            "Create out.txt",
            total_seconds=30,
            retries=1,
            goal={
                "condition": "Output exists",
                "judge": False,
                "predicates": [{"id": "out", "type": "file_exists", "path": "out.txt"}],
            },
        )

    def test_request_timeout_preserves_goal_and_uses_finite_recovery(self):
        c, s, client, _ = self.controller([], self.spec())
        client.chat.completions.create.side_effect = ModelDeadlineExceeded(
            "request expired", source="request"
        )
        outcome = c.execute()
        self.assertEqual(outcome.status, "retrying", outcome)
        self.assertEqual(outcome.reason_code, "request_timeout")
        self.assertEqual(self.store.get(c.id)["recovery_count"], 1)
        self.assertNotEqual(s.goal.status()["status"], "limit")
        deadline = self.store.get(c.id)["deadline_at"]
        s.close()
        self.assertTrue(s.wait_closed(5))
        c.close_heartbeat()
        self.store.update(c.header, {"next_retry_at": 0})
        fresh, next_session, _, _ = self.controller(
            [self.write("result", "out.txt")], identity=c.id
        )
        self.assertEqual(fresh.execute().status, "completed")
        self.assertEqual(self.store.get(c.id)["deadline_at"], deadline)

    def test_episode_deadline_yields_without_consuming_failure_recovery(self):
        c, s, client, _ = self.controller([], self.spec())
        client.chat.completions.create.side_effect = [
            ModelDeadlineExceeded("episode expired", source="episode"),
            self.write("result", "out.txt"),
        ]
        outcome = c.execute()
        self.assertEqual(outcome.status, "completed", outcome)
        self.assertEqual(self.store.get(c.id)["recovery_count"], 0)
        self.assertEqual(outcome.episode, 2)

    def test_session_deadline_remains_a_stop_and_is_retained_after_restore(self):
        c, s, _, _ = self.controller([reply("unreachable")], self.spec())
        s.gateway._state.deadline = Deadline.after(0, source="session")
        s.gateway._state.session_deadline_at = 1.0
        outcome = c.execute()
        self.assertEqual(outcome.status, "blocked", outcome)
        self.assertEqual(outcome.reason_code, "session_timeout")
        self.assertGreater(c.deadline().remaining(), 0)
        client, _ = fake_client([reply("unreachable")])
        restored = ModelGateway(client, GatewayLimits(session_timeout=10))
        self.addCleanup(restored.close)
        restored.restore_usage(s.gateway.summary())
        self.assertEqual(restored._state.deadline.remaining(), 0)
        self.assertEqual(restored._state.deadline.source, "session")

    def test_deadline_intersection_keeps_source_and_outer_limit_wins_ties(self):
        when = Deadline.after(10).expires_at
        self.assertEqual(
            Deadline(when, "request").intersect(Deadline(when, "run")).source, "run"
        )
        self.assertEqual(
            Deadline.after(1, source="request")
            .intersect(Deadline.after(10, source="episode"))
            .source,
            "request",
        )

    def test_independent_member_scope_keeps_run_limit_without_lead_episode(self):
        client, _ = fake_client([reply("member response")])
        gateway = ModelGateway(client)
        self.addCleanup(gateway.close)
        with gateway.scope(role="lead", deadline=Deadline.after(0, source="episode")):
            with gateway.scope(
                role="teammate",
                deadline=Deadline.after(2, source="run"),
                inherit_deadline=False,
            ):
                answer = gateway.request(
                    model="fixture",
                    messages=[{"role": "user", "content": "assigned work"}],
                    max_tokens=128,
                )
        self.assertEqual(answer.text, "member response")
