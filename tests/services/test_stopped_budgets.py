import tempfile
import unittest
from pathlib import Path
from homebrew_harness.services.store import ServiceStore
from homebrew_harness.services.spec import ServiceSpec


class StoppedBudgets(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        control = root / "control"
        work = root / "work"
        control.mkdir()
        work.mkdir()
        self.store = ServiceStore(control)
        self.meta = self.store.create(
            ServiceSpec(str(work), run_defaults={"total_seconds": 30, "retries": 1}),
            {},
            {},
            "one",
        )

    def test_stopped_owner_budget_change_is_guarded_idempotent_and_keeps_permissions(
        self,
    ):
        changes = {
            "run_defaults": {"total_seconds": None, "retries": None},
            "config": {"max_turns": None},
        }
        result = self.store.configure_stopped_budgets(
            self.meta["id"], changes, self.meta["revision"], "one"
        )
        current = self.store.get(self.meta["id"])
        self.assertIsNone(current["spec"]["run_defaults"]["retries"])
        self.assertEqual(current["spec"]["workspace"], self.meta["spec"]["workspace"])
        self.assertEqual(current["binding"], {})
        self.assertEqual(
            self.store.configure_stopped_budgets(
                self.meta["id"], changes, self.meta["revision"], "one"
            ),
            result,
        )
        with self.assertRaises(ValueError):
            self.store.configure_stopped_budgets(
                self.meta["id"],
                {"run_defaults": {"write_paths": ["elsewhere"]}},
                current["revision"],
                "bad",
            )
        self.assertEqual(self.store.get(self.meta["id"]), current)

    def test_live_service_cannot_be_reconfigured(self):
        current = self.store.claim(self.meta["id"], {})
        with self.assertRaises(ValueError):
            self.store.configure_stopped_budgets(
                self.meta["id"],
                {"run_defaults": {"retries": None}},
                current["revision"],
                "bad",
            )

    def test_bound_paused_run_from_serialized_workspace_keeps_code_and_history(self):
        from homebrew_harness.runs import RunStore, RunSpec

        workspace = Path(self.meta["spec"]["workspace"])
        code = workspace / "retained.py"
        code.write_text("draft = True\n")
        native = RunStore(workspace)
        self.addCleanup(native.close)
        created = native.create(RunSpec("same work", total_seconds=30, retries=1))
        claimed = native.claim(created["id"])
        header = {
            "run_id": claimed["id"],
            "owner": claimed["owner"],
            "generation": claimed["generation"],
        }
        native.update(
            header, {"status": "paused", "cleanup_complete": True, "test_runs": 7}
        )
        before = native.get(created["id"])
        work = {
            "id": "work_" + "1" * 32,
            "service_id": self.meta["id"],
            "revision": 1,
            "pause_requested": True,
            "run_workspace": str(workspace),
            "run_id": before["id"],
            "run_spec": before["spec"],
        }
        with self.store.db.write() as conn:
            self.store.db._put(conn, "service_work", work["id"], work)
        changes = {"run_defaults": {"total_seconds": None, "retries": None}}
        result = self.store.configure_stopped_budgets(
            self.meta["id"],
            changes,
            self.meta["revision"],
            "bound-run",
            work_id=work["id"],
            run_changes=changes["run_defaults"],
        )
        after = native.get(before["id"])
        current = self.store.work(work["id"], self.meta["id"])
        self.assertEqual(after["test_runs"], before["test_runs"])
        self.assertEqual(after["deadline_at"], before["deadline_at"])
        self.assertEqual(after["test_grants"], before["test_grants"])
        self.assertIsNone(after["spec"]["retries"])
        self.assertIsNone(current["run_spec"]["retries"])
        self.assertEqual(current["run_id"], before["id"])
        self.assertTrue(current["pause_requested"])
        self.assertEqual(code.read_text(), "draft = True\n")
        self.assertFalse(result["execution_resumed"])
        self.assertEqual(
            self.store.configure_stopped_budgets(
                self.meta["id"],
                changes,
                self.meta["revision"],
                "bound-run",
                work_id=work["id"],
                run_changes=changes["run_defaults"],
            ),
            result,
        )

    def test_invalid_run_change_is_rejected_before_durable_admission(self):
        from homebrew_harness.runs import RunSpec

        identity = "work_" + "4" * 32
        with self.store.db.write() as conn:
            self.store.db._put(
                conn,
                "service_work",
                identity,
                {
                    "id": identity,
                    "service_id": self.meta["id"],
                    "revision": 1,
                    "pause_requested": True,
                    "run_spec": RunSpec("test").data(),
                },
            )
        for values in ({"write_paths": ["elsewhere"]}, {"retries": -1}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.store.configure_stopped_budgets(
                    self.meta["id"],
                    {"run_defaults": {"retries": None}},
                    self.meta["revision"],
                    "invalid",
                    work_id=identity,
                    run_changes=values,
                )
        self.assertEqual(self.store.runtime.items("service_commands"), {})
