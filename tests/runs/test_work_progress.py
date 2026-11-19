import json
import unittest

from homebrew_harness.runs import RunSpec
from homebrew_harness.storage.work_snapshot import verify
from tests.runs import test_controller as fixture
from tests.support.clients import call, reply


class WorkProgressRuns(unittest.TestCase):
    setUp = fixture.RunTests.setUp
    controller = fixture.RunTests.controller
    write = fixture.RunTests.write

    def spec(self, goal=None):
        return RunSpec(
            "Complete the local source change.",
            work_context={
                "service_id": "svc_" + "2" * 32,
                "work_id": "work_" + "1" * 32,
                "requirements_revision": 3,
            },
            episode_model_calls=4,
            goal=goal,
        )

    def handoff(self, **extra):
        return reply(
            calls=[
                call(
                    "handoff",
                    name="submit_work_progress",
                    arguments=json.dumps(
                        {
                            "summary": "Source change complete; candidate verification remains in Delivery.",
                            "complete": True,
                            **extra,
                        }
                    ),
                )
            ]
        )

    def test_explicit_handoff_completes_without_extra_model_or_attention(self):
        c, s, client, _ = self.controller(
            [self.write("write", "result.txt"), self.handoff()], self.spec()
        )
        outcome = c.execute()
        self.assertEqual(outcome.status, "completed", outcome)
        self.assertEqual(client.chat.completions.create.call_count, 2)
        state = c.store.get(c.id)
        record = s.runtime_store.get("run_work_progress", state["work_progress_ref"])
        self.assertTrue(record["accepted"])
        self.assertTrue(record["complete"])
        self.assertEqual(
            (
                verify(record["source"])
                and __import__("pathlib").Path(record["source"]["workspace"])
                / "result.txt"
            ).read_text(),
            "ok",
        )
        self.assertFalse(c.requests.records(c.id))

    def test_existing_hard_goal_still_checks_before_acceptance(self):
        goal = {
            "condition": "Result exists",
            "judge": False,
            "predicates": [
                {"id": "result", "type": "file_exists", "path": "result.txt"}
            ],
        }
        c, s, _, _ = self.controller(
            [self.write("write", "result.txt"), self.handoff()], self.spec(goal)
        )
        outcome = c.execute()
        self.assertEqual(outcome.status, "completed", outcome)
        self.assertEqual(s.goal.status()["status"], "achieved")

    def test_partial_progress_survives_without_becoming_completion(self):
        partial = reply(
            calls=[
                call(
                    "progress",
                    name="submit_work_progress",
                    arguments=json.dumps(
                        {
                            "summary": "One part finished.",
                            "remaining": ["Finish the rest."],
                            "complete": False,
                        }
                    ),
                )
            ]
        )
        c, s, _, _ = self.controller([partial, reply("Continue later")], self.spec())
        c.execute()
        row = next(iter(s.runtime_store.items("run_work_progress").values()))
        self.assertFalse(row["complete"])
        self.assertIsNone(row["source"])

    def test_foreign_evidence_and_input_scope_are_rejected(self):
        c, s, _, _ = self.controller(
            [
                self.handoff(evidence_refs=["test_foreign"]),
                reply("No handoff accepted."),
            ],
            self.spec(),
        )
        c.execute()
        self.assertFalse(s.runtime_store.items("run_work_progress"))
        self.assertIn(
            "not readable",
            next(
                m["content"] for m in s.messages if m.get("tool_call_id") == "handoff"
            ),
        )

    def test_failed_hard_goal_does_not_accept_the_completion_proposal(self):
        goal = {
            "condition": "Missing source must exist",
            "judge": False,
            "predicates": [
                {"id": "missing", "type": "file_exists", "path": "missing.txt"}
            ],
        }
        c, s, client, _ = self.controller(
            [self.handoff()] + [reply("Not done yet.") for _ in range(12)],
            self.spec(goal),
        )
        outcome = c.execute()
        self.assertNotEqual(outcome.status, "completed")
        rows = s.runtime_store.items("run_work_progress")
        self.assertTrue(rows)
        self.assertFalse(any(v.get("accepted") for v in rows.values()))

    def test_invalid_work_context_reports_a_validation_error(self):
        for changes in (
            {"work_id": None},
            {"service_id": []},
            {"requirements_revision": -1},
            {"progress": object()},
            {"progress": float("nan")},
        ):
            context = self.spec().work_context | changes
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                RunSpec("Work", work_context=context)
