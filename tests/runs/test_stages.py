import copy
import json
import sys
import unittest
from dataclasses import replace
from unittest.mock import patch

from homebrew_harness.execution.test_runner import TestRunner, TestRecipe, TestContext
from homebrew_harness.orchestration.task_store import TaskStore
from homebrew_harness.runs import RunSpec
from homebrew_harness.runs.stages import completion_guard
from homebrew_harness.storage.control_boundary import ProposalSuperseded
from tests.runs import test_controller as fixture
from tests.support.clients import reply, call


def stage_spec(**kwargs):
    acceptance = {
        "condition": "Implementation verified",
        "judge": False,
        "predicates": [
            {"id": "code", "type": "file_contains", "path": "code.txt", "text": "done"}
        ],
    }
    goal = {
        "condition": "Reported",
        "judge": False,
        "predicates": [{"id": "report", "type": "file_exists", "path": "REPORT.md"}],
    }
    return RunSpec(
        "Implement and report without losing the original constraints",
        goal=goal,
        stages=(
            {"key": "implement", "subject": "Implement", "acceptance": acceptance},
            {
                "key": "report",
                "subject": "Report",
                "depends_on": ["implement"],
                "acceptance": goal,
            },
        ),
        **kwargs,
    )


# Keep the fixture without inheriting its unrelated tests.
class StageTests(unittest.TestCase):
    setUp = fixture.RunTests.setUp
    controller = fixture.RunTests.controller

    def prepared(self, spec=None):
        control, session, *_ = self.controller([], spec or stage_spec())
        control.prepare()
        session.intent.update(control.spec.prompt, new_task=True)
        tasks = sorted(session.workspace.store.list(), key=lambda t: t.stage["key"])
        return control, session, tasks

    def test_stage_plan_rejects_cycles_missing_goal_and_mutable_acceptance(self):
        spec = stage_spec().data()
        self.assertIn("tasks", spec["enabled"])
        self.assertNotIn("create_task", spec["tools"])
        for change in ("cycle", "unknown", "judge", "goal"):
            value = copy.deepcopy(spec)
            if change == "cycle":
                value["stages"][0]["depends_on"] = ["report"]
            if change == "unknown":
                value["stages"][0]["depends_on"] = ["missing"]
            if change == "judge":
                value["stages"][0]["acceptance"]["judge"] = True
            if change == "goal":
                value["goal"] = None
            with self.subTest(change=change), self.assertRaises(ValueError):
                RunSpec.from_dict(value)

    def test_completion_verifies_before_unlock_and_handoff_preserves_requirements(self):
        control, session, (first, report) = self.prepared()
        tasks = session.workspace.store
        with self.assertRaisesRegex(ValueError, "blocked"):
            tasks.claim(report.id)
        tasks.claim(first.id)
        with self.assertRaisesRegex(ValueError, "acceptance failed"):
            tasks.complete(first.id)
        self.assertEqual(tasks.load(first.id).status, "in_progress")
        (self.root / "code.txt").write_text("done")
        completed, ready = tasks.complete(
            first.id, summary="Implementation ready", next_steps="Write report"
        )
        self.assertEqual([t.id for t in ready], [report.id])
        handoff = json.loads(
            control.read_handoff(
                workdir=self.root, handoff_id=completed.stage["handoff"]
            )
        )
        self.assertEqual(handoff["task_intent"], session.intent.to_dict())
        self.assertTrue(handoff["verification"]["ok"])
        self.assertEqual(handoff["proposal"]["next_steps"], "Write report")
        self.assertEqual(len(self.store.runtime.items("stage_submissions")), 2)
        tasks.claim(report.id)
        (self.root / "REPORT.md").write_text("Reported")
        tasks.complete(report.id)
        self.assertTrue(control.stages.check(control.spec.goal)["ok"])

    def test_submission_failure_rolls_back_all_three_records_and_keeps_lease(self):
        control, session, (first, _) = self.prepared()
        tasks = session.workspace.store
        tasks.claim(first.id)
        (self.root / "code.txt").write_text("done")
        db = self.store.runtime.backend()
        original = db._put

        def fail(conn, section, key, value):
            if section == "tasks":
                raise OSError("commit fault")
            return original(conn, section, key, value)

        with patch.object(db, "_put", side_effect=fail), self.assertRaises(OSError):
            tasks.complete(first.id)
        self.assertEqual(self.store.runtime.items("stage_submissions"), {})
        self.assertEqual(self.store.runtime.items("stage_handoffs"), {})
        self.assertEqual(tasks.load(first.id).status, "in_progress")
        tasks.assert_execution(first.id)
        tasks.complete(first.id)

    def test_changed_inputs_invalidate_stage_and_active_descendants(self):
        control, session, (first, report) = self.prepared()
        tasks = session.workspace.store
        tasks.claim(first.id)
        (self.root / "code.txt").write_text("done")
        complete, _ = tasks.complete(first.id)
        session.workspace.lead_assignment = tasks.claim(report.id)
        (self.root / "unrelated.txt").write_text("keep acceptance")
        control.stages.reconcile()
        self.assertEqual(tasks.load(first.id).status, "completed")
        (self.root / "code.txt").write_text("done changed")
        control.stages.reconcile()
        self.assertEqual(
            [tasks.load(t.id).status for t in (first, report)], ["pending", "pending"]
        )
        self.assertIsNone(session.workspace.lead_assignment)
        self.assertTrue(
            self.store.runtime.get("stage_handoffs", complete.stage["handoff"])
        )
        self.assertEqual(tasks._executions, {})

    def test_stage_owner_and_frozen_definition_cannot_be_bypassed(self):
        control, session, (first, _) = self.prepared()
        other = TaskStore(self.root)
        self.addCleanup(other.close)
        with self.assertRaisesRegex(ValueError, "controller"):
            other.claim(first.id)
        with self.assertRaisesRegex(ValueError, "host-selected"):
            session.workspace.store.update_dependencies(first.id, [])
        with self.assertRaises(ValueError):
            control.read_handoff(workdir=self.root, handoff_id="missing")

    def test_new_input_during_acceptance_cannot_complete(self):
        control, session, (first, report) = self.prepared()
        tasks = session.workspace.store
        tasks.claim(first.id)
        (self.root / "code.txt").write_text("done")
        check = control.stages._check

        def changed(task):
            result = check(task)
            self.store.submit(control.id, "Preserve the changed requirement")
            return result

        with (
            patch.object(control.stages, "_check", side_effect=changed),
            self.assertRaises(ProposalSuperseded),
        ):
            tasks.complete(first.id)
        self.assertEqual(tasks.load(first.id).status, "in_progress")
        self.assertEqual(tasks.load(report.id).status, "pending")

    def test_final_commit_requires_exact_accepted_dag(self):
        control, session, _ = self.prepared()
        db = self.store.runtime.backend()
        with db.write() as conn, self.assertRaises(ProposalSuperseded):
            completion_guard(db, conn, control.state, control.stages.records())

    def test_managed_receipt_survives_new_runner_but_not_changed_scope_source_or_recipe(
        self,
    ):
        control, session, (first, _) = self.prepared()
        (self.root / "code.txt").write_text("done")
        recipe = TestRecipe(
            "check",
            (sys.executable, "-c", "pass"),
            input_globs=("code.txt",),
            locked_globs=(),
        )

        def runner(recipe=recipe):
            result = TestRunner(self.root, [recipe])
            result.receipt_scope = control.evidence_scope
            self.addCleanup(result.close)
            return result

        tests = runner()
        tests.approve("check", task_id=first.id)
        receipt = tests.run("check", context=TestContext("lead", first.id))
        self.assertEqual(receipt["status"], "passed")
        restored = runner()
        self.assertTrue(
            restored.verify("check", self.root, task_id=first.id, since=float("inf"))[
                "ok"
            ]
        )
        self.assertEqual(restored.get(receipt["run_id"])["run_id"], receipt["run_id"])
        self.assertFalse(
            restored.verify("check", self.root, task_id="task_00000000")["ok"]
        )
        self.assertFalse(
            runner(replace(recipe, env=(("MODE", "changed"),))).verify(
                "check", self.root, task_id=first.id
            )["ok"]
        )
        session.intent.update("A new constraint")
        self.assertFalse(restored.verify("check", self.root, task_id=first.id)["ok"])
        session.intent.restore({"original": control.spec.prompt, "amendments": []})
        (self.root / "code.txt").write_text("changed")
        self.assertFalse(restored.verify("check", self.root, task_id=first.id)["ok"])

    def test_complete_task_through_full_managed_loop(self):
        control, session, client, _ = self.controller([], stage_spec())
        first, report = sorted(
            session.workspace.store.list(), key=lambda t: t.stage["key"]
        )
        actions = [
            ("claim_task", {"task_id": first.id}),
            ("write_file", {"path": "code.txt", "content": "done"}),
            ("complete_task", {"task_id": first.id}),
            ("claim_task", {"task_id": report.id}),
            ("write_file", {"path": "REPORT.md", "content": "reported"}),
            ("complete_task", {"task_id": report.id}),
        ]
        responses = [
            reply(calls=[call(str(n), name=name, arguments=json.dumps(args))])
            for n, (name, args) in enumerate(actions)
        ]
        client.chat.completions.create.side_effect = responses
        result = control.execute()
        self.assertEqual(
            result.status,
            "completed",
            (result, [m for m in session.messages if m.get("role") == "tool"]),
        )
        self.assertTrue(
            all(t.status == "completed" for t in session.workspace.store.list())
        )

    def test_dependency_change_within_one_batch_cannot_accept_downstream(self):
        control, session, (first, report) = self.prepared()
        tasks = session.workspace.store
        tasks.claim(first.id)
        (self.root / "code.txt").write_text("done")
        tasks.complete(first.id)
        tasks.claim(report.id)
        (self.root / "REPORT.md").write_text("reported")
        (self.root / "code.txt").write_text("done changed within batch")
        with self.assertRaisesRegex(ValueError, "unverified predecessor"):
            tasks.complete(report.id)
        self.assertEqual(tasks.load(report.id).status, "in_progress")
        self.assertEqual(len(self.store.runtime.items("stage_handoffs")), 1)

    def test_committed_acceptance_with_lost_return_is_reconciled(self):
        from contextlib import contextmanager

        control, session, (first, _) = self.prepared()
        tasks = session.workspace.store
        tasks.claim(first.id)
        (self.root / "code.txt").write_text("done")
        db = self.store.runtime.backend()
        original = db.write

        @contextmanager
        def lost_return():
            with original() as conn:
                yield conn
            raise OSError("commit acknowledgement lost")

        with patch.object(db, "write", lost_return):
            completed, _ = tasks.complete(first.id)
        self.assertEqual(completed.status, "completed")
        self.assertEqual(len(self.store.runtime.items("stage_handoffs")), 1)
        self.assertNotIn(first.id, tasks._executions)

    def test_missing_dag_record_is_not_a_smaller_successful_plan(self):
        control, session, (first, report) = self.prepared()
        self.store.runtime.remove("tasks", report.id)
        with self.assertRaisesRegex(ValueError, "missing"):
            control.stages.check(control.spec.goal)

    def test_pending_execution_prevents_acceptance(self):
        control, session, (first, _) = self.prepared()
        tasks = session.workspace.store
        tasks.claim(first.id)
        (self.root / "code.txt").write_text("done")
        for pending in ("background", "user_input", "source"):
            with (
                patch.object(control, "pending_work", return_value=[pending]),
                self.assertRaisesRegex(ValueError, "pending work"),
            ):
                tasks.complete(first.id)
        with (
            patch.object(
                session,
                "recovery_operations",
                return_value=[{"operation_id": "unknown"}],
            ),
            self.assertRaisesRegex(ValueError, "inspection"),
        ):
            tasks.complete(first.id)
        self.assertEqual(self.store.runtime.items("stage_handoffs"), {})

    def test_new_failed_receipt_overrides_success_without_monotonic_comparison(self):
        control, session, (first, _) = self.prepared()
        source = self.root / "code.txt"
        source.write_text("pass")
        recipe = TestRecipe(
            "check",
            (
                sys.executable,
                "-c",
                "from pathlib import Path;raise SystemExit(0 if Path('code.txt').read_text() == 'pass' else 1)",
            ),
            input_globs=("code.txt",),
            locked_globs=(),
        )
        runner = TestRunner(self.root, [recipe])
        runner.receipt_scope = control.evidence_scope
        self.addCleanup(runner.close)
        runner.approve("check", task_id=first.id, max_runs=2)
        passed = runner.run("check", context=TestContext("lead", first.id))
        prior = self.store.runtime.get("test_receipts", passed["run_id"])
        prior["started"] = 1e30
        self.store.runtime.put("test_receipts", passed["run_id"], prior)
        source.write_text("fail")
        failed = runner.run("check", context=TestContext("lead", first.id))
        self.assertEqual(failed["status"], "failed")
        restored = TestRunner(self.root, [recipe])
        self.addCleanup(restored.close)
        restored.receipt_scope = control.evidence_scope
        checked = restored.verify("check", self.root, task_id=first.id)
        self.assertFalse(checked["ok"])
        self.assertEqual(checked["run_id"], failed["run_id"])
        # A JSON receipt file is never imported into the trusted database.
        self.store.runtime.remove("test_receipts", passed["run_id"])
        self.store.runtime.remove("test_receipts", failed["run_id"])
        self.assertFalse(restored.verify("check", self.root, task_id=first.id)["ok"])

    def test_unfinished_sqlite_task_protects_checkpoint_retirement(self):
        from homebrew_harness.storage.checkpoint_retirement import workspace_blockers

        control, session, (first, report) = self.prepared()
        state = self.store.runtime.read()
        self.assertIn(
            "workspace_unfinished_task",
            workspace_blockers(state, {"records": {}}, session.session_id),
        )
        self.assertEqual(state["tasks"][report.id]["blockedBy"], [first.id])
