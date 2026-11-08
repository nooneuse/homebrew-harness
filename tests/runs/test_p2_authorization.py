import json
import sys
import time
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from homebrew_harness import HarnessSession, Capabilities
from homebrew_harness.execution.test_runner import TestRunner, TestRecipe, TestContext
from homebrew_harness.orchestration.task_store import TaskStore
from homebrew_harness.runs import RunSpec, RunController
from homebrew_harness.runs.stages import MemberStageAccess
from tests.runs import test_managed_team as fixture
from tests.support.clients import fake_client, call, reply


class TestGrantTests(unittest.TestCase):
    setUp = fixture.ManagedTeamTests.setUp

    def setup_run(self, *, locked=(), one=False, slow=False):
        if one:
            from tests.runs.test_p1_members import one_member

            data = one_member().data()
        else:
            data = fixture.team_spec().data()
        data["total_seconds"] = 60
        data["allow_tests"] = ["check"]
        for stage in data["stages"]:
            if stage["executor"] == "lead":
                continue
            stage["acceptance"]["predicates"].append(
                {
                    "id": "checks",
                    "type": "tests_passed",
                    "recipe": "check",
                    "workspace": "base",
                }
            )
        spec = RunSpec.from_dict(data)
        state = self.store.claim(self.store.create(spec)["id"])
        c = RunController(self.store, state["id"], state["owner"], state["generation"])
        runner = TestRunner(
            self.root,
            [
                TestRecipe(
                    "check",
                    (
                        sys.executable,
                        "-c",
                        "from pathlib import Path\nimport time\np=Path('.harness');p.mkdir(exist_ok=True)\n(p/'test-started').touch()\nwhile not (p/'test-release').exists(): time.sleep(0.01)\nprint('verified')"
                        if slow
                        else "print('verified')",
                    ),
                    input_globs=("alpha.txt", "beta.txt"),
                    roles=("lead", "teammate"),
                    locked_globs=locked,
                    max_runs=1,
                )
            ],
            max_runs=6,
        )
        client, _ = fake_client([])
        s = HarnessSession(
            client,
            "fixture",
            self.root,
            capabilities=Capabilities(spec.enabled),
            run_control=c,
            test_runner=runner,
            shutdown_timeout=2,
        )
        self.addCleanup(c.close_heartbeat)
        self.addCleanup(lambda: self.assertTrue(s.wait_closed(5)))
        self.addCleanup(s.close)
        c.prepare()
        s.intent.update(spec.prompt, new_task=True)
        s._checkpoint_writer.save(overrides={"intent": s.intent.to_dict()})
        return c, s, client

    def claim(self, c, s, name):
        own = TaskStore(self.root, owner=name, leases=s.workspace.leases)
        own.intent = s.intent
        own.stage_control = MemberStageAccess(c.stages, name, own)
        self.addCleanup(own.close)
        c.team_control._tokens[name] = s.inputs.token()
        task = own.claim(c.team_control.task_for(name).id)
        return task, own.workspace(task), TestContext("teammate", task.id)

    def wait(self, predicate):
        end = time.monotonic() + 8
        while time.monotonic() < end:
            value = predicate()
            if value:
                return value
            time.sleep(0.02)
        self.fail("Expected durable member state did not arrive")

    def test_same_member_continues_after_host_renews_exhausted_test(self):
        c, s, client = self.setup_run()
        steps = [0]
        task = c.team_control.task_for("alpha")

        def respond(**kwargs):
            self.assertIn("You are teammate alpha", json.dumps(kwargs["messages"]))
            n = steps[0]
            steps[0] += 1
            name, arguments = (
                ("write_file", {"path": "alpha.txt", "content": "alpha done"})
                if n == 0
                else (
                    ("run_tests", {"name": "check"})
                    if n < 4
                    else ("complete_task", {"task_id": task.id})
                )
            )
            return reply(
                calls=[
                    call("step" + str(n), name=name, arguments=json.dumps(arguments))
                ]
            )

        client.chat.completions.create.side_effect = respond
        args = c.team_control.arguments("alpha")
        c.team_control.authorize(args)
        s.team.spawn(**args)
        member = s.team.members["alpha"]
        thread = member.thread
        outcome = self.wait(
            lambda: self.store.runtime.get("run_member_outcomes", c.id + ":alpha")
        )
        self.assertEqual(outcome["kind"], "waiting_event", outcome)
        request = c.requests.get(c.id, outcome["wait_on"]["request_id"])
        self.assertEqual(request["kind"], "test_grant")
        self.assertEqual(s.tests.runs, 1)
        self.assertEqual(steps[0], 3)
        c.requests.decide(
            c.id, request["id"], request["revision"], "approve", runs=1, seconds=30
        )
        self.wait(lambda: s.workspace.store.load(task.id).status == "completed")
        self.assertIs(member.thread, thread)
        self.assertEqual(s.tests.runs, 2)
        self.assertEqual(
            self.store.runtime.get("run_members", c.id + ":alpha")["recoveries"], 0
        )
        self.assertEqual(c.requests.get(c.id, request["id"])["state"], "consumed")

    def test_shared_recipe_consumes_only_the_actual_task_grant(self):
        c, s, _ = self.setup_run()
        a, cwd_a, ctx_a = self.claim(c, s, "alpha")
        b, cwd_b, ctx_b = self.claim(c, s, "beta")
        recipe = s.tests._recipe("check")
        self.assertIsNotNone(c.test_authorizations.grant(recipe, cwd_a, ctx_a))
        self.assertIsNotNone(c.test_authorizations.grant(recipe, cwd_b, ctx_b))
        fields = c.test_authorization_fields("check", cwd_a)
        self.store.runtime.backend().accept_operation(
            "fixture-test",
            {
                "checkpoint": "member:" + s.session_id + ":alpha",
                "state": "started",
                "name": "run_tests",
                "managed_run": c.header,
                "test_recipe": "check",
                "test_grant_key": c.test_grant_key("check", cwd_a),
                **fields,
            },
            s.inputs.token(),
        )
        self.assertIsNone(c.test_authorizations.grant(recipe, cwd_a, ctx_a))
        self.assertIsNotNone(c.test_authorizations.grant(recipe, cwd_b, ctx_b))
        self.assertEqual(self.store.get(c.id)["test_attempts"], {"check": 1})
        self.store.runtime.remove("operations", "fixture-test")

    def test_renewal_commit_return_loss_and_repeated_decision_do_not_grant_twice(self):
        c, s, _ = self.setup_run()
        task, cwd, ctx = self.claim(c, s, "alpha")
        request = c.test_authorizations.request("check", cwd, ctx)
        db = self.store.runtime.backend()
        original = db.write

        @contextmanager
        def lost():
            with original() as conn:
                yield conn
            raise OSError("COMMIT return lost")

        with patch.object(db, "write", lost):
            result = c.requests.decide(
                c.id, request["id"], 0, "approve", runs=1, seconds=30
            )
        self.assertEqual(result["state"], "consumed")
        prior = self.store.get(c.id)["test_grants"]
        self.assertEqual(
            c.requests.decide(c.id, request["id"], 0, "approve", runs=1, seconds=30),
            result,
        )
        self.assertEqual(self.store.get(c.id)["test_grants"], prior)
        self.assertEqual(
            len(
                [
                    m
                    for m in self.store.runtime.mailbox("inspect", "alpha")
                    if m["type"] == "test_grant_decision"
                ]
            ),
            1,
        )

    def test_changed_protected_inputs_revision_and_widening_are_rejected(self):
        c, s, _ = self.setup_run(locked=(".gitignore",))
        task, cwd, ctx = self.claim(c, s, "alpha")
        request = c.test_authorizations.request("check", cwd, ctx)
        for options in ({"runs": 2}, {"seconds": 3601}, {"runs": True}):
            with self.assertRaises(ValueError):
                c.requests.decide(c.id, request["id"], 0, "approve", **options)
        (cwd / ".gitignore").write_text("changed")
        with self.assertRaisesRegex(ValueError, "Protected"):
            c.requests.decide(c.id, request["id"], 0, "approve")
        self.assertEqual(s.tests.runs, 0)

    def test_denial_and_new_revision_do_not_authorize_an_old_proposal(self):
        from homebrew_harness.storage.control_boundary import ProposalSuperseded

        c, s, _ = self.setup_run()
        task, cwd, ctx = self.claim(c, s, "alpha")
        self.assertIsNotNone(
            c.test_authorizations.grant(s.tests._recipe("check"), cwd, ctx)
        )
        grants = self.store.get(c.id)["test_grants"]
        grants[c.test_grant_key("check", cwd)]["expires_at"] = time.time() - 1
        self.store.update(c.header, {"test_grants": grants})
        request = c.test_authorizations.request("check", cwd, ctx)
        c.requests.decide(c.id, request["id"], 0, "deny")
        self.assertIsNone(
            c.test_authorizations.grant(s.tests._recipe("check"), cwd, ctx)
        )
        self.store.submit(c.id, "Keep the original task and reconsider its tests")
        with self.assertRaises(ProposalSuperseded):
            c.test_authorizations.request("check", cwd, ctx)
        self.assertEqual(s.tests.runs, 0)

    def test_goal_availability_uses_durable_grants_and_checks_task_workspace(self):
        c, s, _ = self.setup_run()
        a, cwd_a, ctx_a = self.claim(c, s, "alpha")
        b, cwd_b, ctx_b = self.claim(c, s, "beta")
        self.assertEqual(s.tests.grants, [])
        self.assertTrue(s.tests.authorization_available("check", cwd_a, a.id))
        self.assertFalse(s.tests.authorization_available("check", cwd_a, b.id))
        self.assertIsNotNone(
            c.test_authorizations.grant(s.tests._recipe("check"), cwd_a, ctx_a)
        )
        self.assertTrue(s.tests.authorization_available("check", cwd_a, a.id))
        with self.assertRaisesRegex(ValueError, "workspace"):
            c.test_authorizations.grant(s.tests._recipe("check"), cwd_a, ctx_b)

    def test_lead_waits_for_real_member_test_and_then_finishes(self):
        c, s, client = self.setup_run(one=True, slow=True)
        seen = []
        steps = [0]

        def respond(**kwargs):
            raw = json.dumps(kwargs["messages"])
            tasks = {
                value["stage"]["key"]: value for value in c.stages.records().values()
            }

            def tool(name, args):
                return reply(
                    calls=[
                        call(
                            name + str(steps[0]), name=name, arguments=json.dumps(args)
                        )
                    ]
                )

            if "You are teammate alpha" in raw:
                n = steps[0]
                steps[0] += 1
                if n == 0:
                    return tool(
                        "write_file", {"path": "alpha.txt", "content": "alpha done"}
                    )
                if n == 1:
                    return tool("run_tests", {"name": "check"})
                return tool("complete_task", {"task_id": tasks["alpha"]["id"]})
            if "alpha" not in s.team.members:
                return tool("spawn_teammate", c.team_control.arguments("alpha"))
            if tasks["alpha"]["status"] != "completed":
                return reply("Waiting for the actual member test result.")
            join = tasks["join"]
            if join["status"] == "pending":
                return tool("claim_task", {"task_id": join["id"]})
            records = list(c.store.runtime.items("run_transfers").values())
            if not any(value["kind"] == "integrate" for value in records):
                return tool("integrate_stage", {"task_id": join["id"]})
            if join["status"] == "in_progress":
                return tool("complete_task", {"task_id": join["id"]})
            return tool("publish_stage", {"task_id": join["id"]})

        client.chat.completions.create.side_effect = respond
        args = c.team_control.arguments("alpha")
        c.team_control.authorize(args)
        s.team.spawn(**args)
        original_episode = s.run_episode
        first = [True]

        def episode():
            if first[0]:
                first[0] = False
                directory = (
                    s.workspace.store.workspace(c.team_control.task_for("alpha"))
                    / ".harness"
                )
                self.wait(lambda: (directory / "test-started").exists())
                seen.append(c.pending_work())
                return "Waiting for the actual member test result."
            return original_episode()

        original_finish = c.finish

        def finish(*args, **kwargs):
            result = original_finish(*args, **kwargs)
            if result.status == "waiting" and "tests" in result.pending_work:
                directory = (
                    s.workspace.store.workspace(c.team_control.task_for("alpha"))
                    / ".harness"
                )
                (directory / "test-release").touch()
            return result

        with (
            patch.object(c, "finish", side_effect=finish),
            patch.object(s, "run_episode", side_effect=episode),
        ):
            outcome = c.execute()
        self.assertEqual(outcome.status, "completed", outcome)
        self.assertTrue(any("tests" in pending for pending in seen))
        self.assertEqual(s.tests.runs, 1)
