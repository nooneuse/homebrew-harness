"""Fixed-candidate reuse and acceptance using only synthetic receipts."""

import tempfile
import unittest
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

from homebrew_harness.execution.source_snapshot import snapshot
from homebrew_harness.execution.verification import basis
from homebrew_harness.services.delivery import quality


class QualityEvidenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.cwd = Path(temp.name).resolve()
        (self.cwd / "source.txt").write_text("unchanged source")
        self.candidate = {"workspace": str(self.cwd), "content_hash": "frozen-content"}
        self.selection = {"targets": ["tests.unit"]}
        self.recipe = SimpleNamespace(input_globs=("source.txt",), snapshot_limits={},
                                      selector="unittest", test_roots=("tests",))
        self.runner = SimpleNamespace(
            _recipe=lambda _: self.recipe, _fingerprint=lambda _: "contract",
            backend=SimpleNamespace(name="container"), approve=Mock(), run=Mock(),
            verify=Mock(return_value={"ok": False}), authorization_available=Mock(return_value=False),
        )
        self.delivery = SimpleNamespace(
            git=Mock(), tests=Mock(), stop=Event(), spec={"required_tests": []}, store=Mock(),
            runtime=SimpleNamespace(core=SimpleNamespace(config=SimpleNamespace(
                settings=SimpleNamespace(test_config="test-config")))),
        )
        self.work = {"id": "work_one", "run_id": "run_one", "run_spec": {}, "validation_plan": {
            "tests": ["unit"], "selections": {"unit": self.selection},
        }}
        self.history = []
        native = patch.object(quality.work_evidence, "receipts", side_effect=lambda *_, **__: self.history)
        loader = patch.object(quality, "load_test_runner", return_value=self.runner)
        for mocked in (native, loader):
            mocked.start()
            self.addCleanup(mocked.stop)

    def receipt(self, run_id, time, *, selection=None, status="passed"):
        row = {
            "run_id": run_id, "recipe": "unit", "fingerprint": "contract", "role": "lead",
            "selection": selection, "test_scope": "selected" if selection else "full",
            "status": status, "exit_code": 0 if status == "passed" else 1, "executed_at": time,
            "source_stable": True, "execution_backend": "container",
            "isolation": {"cleanup_complete": True, "os_isolation_verified": True},
            "inputs": snapshot(self.cwd, self.recipe.input_globs),
        }
        row["verification_basis"] = basis(row)
        row.pop("inputs")
        return row

    def require_goal(self, **predicate):
        self.work["run_spec"]["goal"] = {"condition": "Registered tests passed", "judge": False,
            "predicates": [{"id": "unit-passed", "type": "tests_passed", "recipe": "unit", **predicate}]}

    def verify(self):
        return quality.verify(self.delivery, self.candidate, self.work)

    def test_exact_matching_pass_is_reused_for_delivery_and_exact_goal(self):
        self.history.append(self.receipt("focused", 20, selection=self.selection))
        self.require_goal(selection=self.selection)
        result = self.verify()
        self.assertTrue(result["ok"])
        use = result["quality"][0]["evidence_use"]
        self.assertEqual(use["mode"], "reused")
        self.assertEqual(use["receipt_ref"], "focused")
        self.assertEqual(use["candidate_digest"], "frozen-content")
        self.runner.run.assert_not_called()
        self.runner.approve.assert_not_called()
        self.runner.verify.assert_not_called()

    def test_reused_selected_pass_never_satisfies_full_goal(self):
        self.history.append(self.receipt("focused", 20, selection=self.selection))
        self.require_goal()
        result = self.verify()
        self.assertFalse(result["ok"])
        self.assertFalse(result["goal_evidence"]["ok"])
        self.runner.verify.assert_called_once_with("unit", self.cwd, since=0, task_id=None)
        self.runner.run.assert_not_called()

    def test_normal_and_forced_selected_passes_do_not_clear_work_full_failure(self):
        self.history.extend([self.receipt("full-failed", 10, status="failed"),
                             self.receipt("focused", 20, selection=self.selection)])
        result = self.verify()
        self.assertFalse(result["ok"])
        self.runner.run.assert_not_called()
        use = result["quality"][0]["evidence_use"]
        self.assertEqual(use["mode"], "blocking")
        self.assertEqual(use["receipt_ref"], "full-failed")
        self.assertEqual(use["reason"], "unresolved_full_scope_failure")
        self.assertEqual(use["checked_basis"]["selection"], self.selection)
        self.assertEqual(use["historical_basis"]["test_scope"], "full")
        self.assertEqual(result["goal_evidence"]["results"][0]["evidence_use"], use)
        self.assertNotIn("test_failure", result)

        self.work["force_test_recheck"] = True
        self.runner.run.return_value = self.receipt("new-focused", 30, selection=self.selection)
        self.runner.verify.return_value = {"ok": True}
        result = self.verify()
        self.runner.run.assert_called_once_with("unit", selection=self.selection)
        self.assertFalse(result["ok"])
        self.assertEqual(result["quality"][0]["status"], "passed")
        use = result["goal_evidence"]["results"][0]["evidence_use"]
        self.assertEqual(use["receipt_ref"], "full-failed")
        self.assertEqual(use["focused_recheck_ref"], "new-focused")

    def test_candidate_full_failure_survives_replanning_and_full_recheck_can_recover(self):
        self.work["changeset_id"] = "changeset_one"
        full_failure = self.receipt("candidate-full-failed", 10, status="failed")
        row = {"work_id": "work_one", "run_id": "run_one", "validation_history": [{"quality": [full_failure]}]}
        self.delivery.store.get.return_value = row
        blocked = self.verify()
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["quality"][0]["evidence_use"]["receipt_ref"], "candidate-full-failed")
        self.runner.run.assert_not_called()

        self.work["force_test_recheck"] = True
        self.work["validation_plan"]["selections"] = {}
        self.runner.run.return_value = self.receipt("candidate-full-pass", 30)
        self.runner.verify.return_value = {"ok": True}
        recovered = self.verify()
        self.assertTrue(recovered["ok"])
        self.runner.run.assert_called_once_with("unit", selection=None)
        self.assertEqual(recovered["quality"][0]["run_id"], "candidate-full-pass")

        row["quality"] = recovered["quality"]
        self.runner.run.reset_mock()
        self.work.pop("force_test_recheck")
        reused = self.verify()
        self.assertTrue(reused["ok"])
        self.assertEqual(reused["quality"][0]["evidence_use"]["mode"], "reused")
        self.runner.run.assert_not_called()

    def test_candidate_receipts_are_scoped_to_owned_runs(self):
        self.work["changeset_id"] = "changeset_two"
        failed = self.receipt("previous-full-failure", 10, status="failed")
        self.history.append(self.receipt("focused", 20, selection=self.selection))
        previous = {"work_id": "work_one", "run_id": "run_previous", "quality": [failed]}
        rows = {"changeset_two": {"work_id": "work_one", "run_id": "run_one", "previous": "changeset_one"},
                "changeset_one": previous}
        self.delivery.store.get.side_effect = lambda _, key: rows[key]
        self.assertTrue(self.verify()["ok"])
        self.work["run_history"] = ["run_previous"]
        self.assertFalse(self.verify()["ok"])
        previous["work_id"] = "foreign_work"
        self.assertTrue(self.verify()["ok"])
        self.runner.run.assert_not_called()

    def test_preset_cannot_regrant_tests_after_changing_locked_inputs(self):
        self.delivery.runtime.spec = SimpleNamespace(preset="self_maintenance")
        self.candidate.update(repo="fixture", target_base="pinned-base", commit="candidate")
        self.recipe.locked_globs = ("tests/trusted_*.py",)
        self.runner.recipes = {"unit": self.recipe}
        self.runner.backend.fingerprint = lambda: "isolated"
        self.runner.max_runs = self.runner.total_seconds = None
        from homebrew_harness.services.self_maintenance import test_registry_identity
        self.delivery.runtime.saved_model_bindings = {"coding_resources": {"tests": test_registry_identity(self.runner)}}
        baseline = {"tests/trusted_suite.py": {"mode": "100644", "oid": "old"}}
        self.delivery.git.tree.side_effect = lambda repo, sha: baseline if sha == "pinned-base" else {}
        self.work["force_test_recheck"] = True
        result = self.verify()
        self.assertFalse(result["ok"])
        self.assertEqual(result["goal_evidence"]["results"][0]["paths"], ["tests/trusted_suite.py"])
        self.runner.approve.assert_not_called()
        self.runner.run.assert_not_called()

    def test_force_runs_even_when_exact_successful_receipt_is_available(self):
        self.history.append(self.receipt("old-pass", 20, selection=self.selection))
        self.work["force_test_recheck"] = True
        self.runner.run.return_value = self.receipt("new-pass", 30, selection=self.selection)
        self.runner.verify.return_value = {"ok": True}
        result = self.verify()
        self.assertTrue(result["ok"])
        self.assertEqual(result["quality"][0]["run_id"], "new-pass")
        self.assertNotIn("evidence_use", result["quality"][0])
        self.runner.approve.assert_called_once_with("unit", cwd=self.cwd, role="lead", max_runs=1)
        self.runner.run.assert_called_once_with("unit", selection=self.selection)
