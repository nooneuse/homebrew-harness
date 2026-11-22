"""Interrupted configuration and long-job state retain accepted facts."""

import unittest
from pathlib import Path
from unittest.mock import patch
from homebrew_harness.runs import RunStore, RunSpec
from tests.services import test_stopped_budgets as budget_fixture
from tests.services import test_maintenance as fixture


class BudgetCommitRecovery(unittest.TestCase):
    setUp = budget_fixture.StoppedBudgets.setUp

    def test_run_step_failure_has_durable_admission_and_idempotent_completion(self):
        root = Path(self.meta["spec"]["workspace"])
        native = RunStore(root)
        self.addCleanup(native.close)
        state = native.claim(native.create(RunSpec("same work", retries=1))["id"])
        header = {
            "run_id": state["id"],
            "owner": state["owner"],
            "generation": state["generation"],
        }
        native.update(
            header, {"status": "paused", "cleanup_complete": True, "test_runs": 7}
        )
        work = {
            "id": "work_" + "3" * 32,
            "service_id": self.meta["id"],
            "revision": 1,
            "pause_requested": True,
            "run_workspace": str(root),
            "run_id": state["id"],
            "run_spec": state["spec"],
        }
        with self.store.db.write() as conn:
            self.store.db._put(conn, "service_work", work["id"], work)
        original = RunStore.configure_budgets

        def interrupted(store, *args, **kw):
            result = original(store, *args, **kw)
            raise RuntimeError("Injected interruption after Run commit.")

        changes = {"run_defaults": {"retries": None}}
        with patch.object(
            RunStore, "configure_budgets", interrupted
        ), self.assertRaisesRegex(ValueError, "migration did not finish"):
            self.store.configure_stopped_budgets(
                self.meta["id"],
                changes,
                self.meta["revision"],
                "stable-one",
                work_id=work["id"],
                run_changes={"retries": None},
            )
        self.assertIsNone(native.get(state["id"])["spec"]["retries"])
        self.assertEqual(self.store.work(work["id"])["run_spec"]["retries"], 1)
        with self.assertRaisesRegex(ValueError, "accepted stopped budget migration"):
            self.store.claim(self.meta["id"], {})
        # A newer stopped-owner note must not strand the admitted migration or
        # disappear when the caller explicitly replays the same request.
        with self.store.db.write() as conn:
            meta = self.store.db._get(conn, "service_meta", self.meta["id"])
            meta["config"] = {**meta["config"], "model_call_warning": 77}
            meta["revision"] += 1
            self.store.db._put(conn, "service_meta", meta["id"], meta)
            current = self.store.db._get(conn, "service_work", work["id"])
            current["execution_context"] = ["Keep the selected branch fixed."]
            current["revision"] += 1
            self.store.db._put(conn, "service_work", work["id"], current)
        result = self.store.configure_stopped_budgets(
            self.meta["id"],
            changes,
            self.meta["revision"],
            "stable-one",
            work_id=work["id"],
            run_changes={"retries": None},
        )
        self.assertIsNone(self.store.work(work["id"])["run_spec"]["retries"])
        self.assertEqual(native.get(state["id"])["test_runs"], 7)
        self.assertFalse(result["execution_resumed"])
        self.assertEqual(
            self.store.get(self.meta["id"])["config"]["model_call_warning"], 77
        )
        self.assertEqual(
            self.store.work(work["id"])["execution_context"],
            ["Keep the selected branch fixed."],
        )


class LeadInstructions(unittest.TestCase):
    setUp = fixture.MaintenanceTests.setUp
    start = fixture.MaintenanceTests.start

    def test_more_than_eight_execution_notes_retain_fixed_branch_instruction(self):
        service = self.start(auto=False)
        service.pause("all")
        work = service.work.submit("Keep the existing feature.")
        work.context("Keep this branch fixed during development.")
        for number in range(12):
            work.context("Independent observation " + str(number))
        row = service.store.work(work.id)
        self.assertEqual(len(row["execution_context"]), 13)
        self.assertIn("branch fixed", row["execution_context"][0])

    def test_delayed_jobs_do_not_hide_later_eligible_job(self):
        import time

        service = self.start(auto=False)
        service.pause("all")
        receipts = [service.converse("Queued " + str(n)) for n in range(40)]
        store = service.runtime.maintenance.store
        with store.db.write() as conn:
            for receipt in receipts[:-1]:
                job = store.get("jobs", receipt["job_id"], conn)
                job["next_attempt"] = time.time() + 3600
                store.put(conn, "jobs", job)
        claimed = store.claim_job()
        self.assertEqual(claimed["id"], receipts[-1]["job_id"])
        with store.db.write() as conn:
            current = store.get("jobs", claimed["id"], conn)
            current["state"] = "queued"
            store.put(conn, "jobs", current)
