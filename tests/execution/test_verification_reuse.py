import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from homebrew_harness.execution.source_snapshot import snapshot
from homebrew_harness.execution.verification import applicable, basis


class EvidenceReuse(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        (self.root / "source.txt").write_text("version one")
        self.runner = SimpleNamespace(
            _recipe=lambda n: SimpleNamespace(
                input_globs=("source.txt",), snapshot_limits={}, selector="unittest",
                test_roots=("tests",),
            ),
            _fingerprint=lambda r: "contract",
            backend=SimpleNamespace(name="container"),
        )
        self.receipt = {
            "run_id": "test_original",
            "recipe": "unit",
            "role": "lead",
            "fingerprint": "contract",
            "inputs": snapshot(self.root, ("source.txt",)),
            "execution_backend": "container",
            "test_scope": "full",
            "selection": None,
            "executed_at": 10,
            "status": "passed",
            "exit_code": 0,
            "source_stable": True,
            "isolation": {"cleanup_complete": True, "os_isolation_verified": True},
        }
        self.receipt["verification_basis"] = basis(self.receipt)

    def receipt_at(self, run_id, time, *, selection=None, status="passed", **changes):
        row = {**self.receipt, "run_id": run_id, "executed_at": time,
               "selection": selection, "test_scope": "selected" if selection else "full",
               "status": status, "exit_code": 0 if status == "passed" else 1, **changes}
        row["verification_basis"] = basis(row)
        return row

    def test_same_input_in_another_root_reuses_original_receipt(self):
        other = self.root / "candidate"
        other.mkdir()
        (other / "source.txt").write_text("version one")
        result = applicable([self.receipt], self.runner, "unit", other)
        self.assertTrue(result["ok"])
        self.assertEqual(result["receipt_ref"], "test_original")
        self.assertEqual(result["mode"], "reused")

    def test_changed_input_or_contract_does_not_reuse(self):
        (self.root / "source.txt").write_text("version two")
        self.assertFalse(
            applicable([self.receipt], self.runner, "unit", self.root)["ok"]
        )
        (self.root / "source.txt").write_text("version one")
        self.runner._fingerprint = lambda r: "other contract"
        self.assertFalse(
            applicable([self.receipt], self.runner, "unit", self.root)["ok"]
        )

    def test_newer_related_failure_cannot_be_hidden(self):
        failure = copy.deepcopy(self.receipt)
        failure.update(
            run_id="test_failure", status="failed", exit_code=1, executed_at=20
        )
        failure["verification_basis"]["selection"] = {"targets": ["tests.some_case"]}
        failure["verification_basis"]["test_scope"] = "selected"
        result = applicable([self.receipt, failure], self.runner, "unit", self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "newer_failure")

    def test_focused_pass_cannot_clear_full_failure_until_a_new_full_pass(self):
        selection = {"targets": ["tests.one"]}
        failure = self.receipt_at("full_failed", 20, status="failed")
        focused = self.receipt_at("focused_pass", 30, selection=selection)
        rows = [self.receipt, failure, focused]
        blocked = applicable(rows, self.runner, "unit", self.root, selection=selection)
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["reason"], "unresolved_full_scope_failure")
        self.assertEqual(blocked["receipt_ref"], "full_failed")
        self.assertEqual(blocked["basis"]["test_scope"], "selected")
        self.assertEqual(blocked["historical_basis"]["test_scope"], "full")
        self.assertFalse(applicable(rows, self.runner, "unit", self.root)["ok"])

        full_pass = self.receipt_at("full_pass", 40)
        selected = applicable([*rows, full_pass], self.runner, "unit", self.root, selection=selection)
        self.assertTrue(selected["ok"])
        self.assertEqual(selected["receipt_ref"], "focused_pass")
        self.assertEqual(applicable([*rows, full_pass], self.runner, "unit", self.root)["receipt_ref"], "full_pass")
        self.assertFalse(applicable([failure, full_pass], self.runner, "unit", self.root, selection=selection)["ok"])

    def test_exact_selector_and_compatible_source_environment_only(self):
        selection = {"targets": ["tests.one"]}
        focused = self.receipt_at("focused_pass", 30, selection=selection)
        other_failure = self.receipt_at("different_selection", 40, status="failed",
                                        selection={"targets": ["tests.two"]})
        full = self.receipt_at("unrelated_full", 50, status="failed", recipe="other-recipe")
        different_rows = [
            other_failure, full,
            self.receipt_at("different_contract", 50, status="failed", fingerprint="different"),
            self.receipt_at("native", 50, status="failed", execution_backend="native"),
            self.receipt_at("role", 50, status="failed", role="worker"),
            self.receipt_at("unstable", 50, status="failed", source_stable=False),
            self.receipt_at("unverified", 50, status="failed", isolation={"cleanup_complete": True}),
            self.receipt_at("unclean", 50, status="failed", isolation={"os_isolation_verified": True}),
        ]
        (self.root / "source.txt").write_text("another basis")
        different_rows.append(self.receipt_at("different_inputs", 50, status="failed",
                                              inputs=snapshot(self.root, ("source.txt",))))
        (self.root / "source.txt").write_text("version one")
        result = applicable([focused, *different_rows], self.runner, "unit", self.root,
                            selection={"targets": ["tests.one", "tests.one"]})
        self.assertEqual(result["receipt_ref"], "focused_pass")
        self.assertTrue(result["ok"])
        self.assertFalse(applicable([focused], self.runner, "unit", self.root, selection={"targets": ["tests.two"]})["ok"])
        exact_failure = self.receipt_at("same_selection", 40, status="failed", selection=selection)
        result = applicable([focused, exact_failure], self.runner, "unit", self.root, selection=selection)
        self.assertFalse(result["ok"])
        self.assertEqual(result["receipt_ref"], "same_selection")

    def test_invalid_later_full_pass_and_timestamp_tie_cannot_clear_failure(self):
        selection = {"targets": ["tests.one"]}
        failure = self.receipt_at("full_failed", 20, status="failed")
        focused = self.receipt_at("focused_pass", 30, selection=selection)
        for not_clearing in [self.receipt_at("tie", 20), self.receipt_at("unstable", 40, source_stable=False)]:
            with self.subTest(receipt=not_clearing["run_id"]):
                result = applicable([failure, focused, not_clearing], self.runner, "unit", self.root,
                                    selection=selection)
                self.assertFalse(result["ok"])
                self.assertEqual(result["receipt_ref"], "full_failed")

    def test_partial_unstable_unknown_and_native_results_are_not_full_evidence(self):
        for changed in [
            {"test_scope": "selected", "selection": {"targets": ["tests.one"]}},
            {"source_stable": False},
            {"status": "failed", "exit_code": 1},
            {"isolation": {"cleanup_complete": False, "os_isolation_verified": True}},
        ]:
            row = {**self.receipt, **changed}
            row["verification_basis"] = basis(row)
            self.assertFalse(applicable([row], self.runner, "unit", self.root)["ok"])
        self.runner.backend.name = "native"
        self.assertFalse(
            applicable([self.receipt], self.runner, "unit", self.root)["ok"]
        )
