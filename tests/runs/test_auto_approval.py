"""Run automatic decisions retain the durable execute_approved contract."""

import json
import unittest

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
        review_input = json.loads(payloads[1][-1]["content"])
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
