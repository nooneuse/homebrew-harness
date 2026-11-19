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
                input_globs=("source.txt",), snapshot_limits={}
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
