"""Exact historical source inputs remain evidence, protected from model writes."""

import copy
import hashlib
import json
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from homebrew_harness.services.delivery.git import GitBackend
from homebrew_harness.services.delivery.source_evidence import install
from homebrew_harness.tools.files import run_read, run_write, run_edit, run_delete


class SourceEvidenceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.git = GitBackend(self.root, {"git_executable": shutil.which("git"), "request_seconds": 30}, {})
        self.addCleanup(self.git.close)
        self.repo = self.root / "repo"
        self.git.run(["init", "--template=", str(self.repo)])
        self.git.run(["config", "user.name", "Fixture"], cwd=self.repo)
        self.git.run(["config", "user.email", "fixture@example.invalid"], cwd=self.repo)
        (self.repo / "src").mkdir()
        self.path = "src/value [literal].py"
        (self.repo / self.path).write_text("base = '原始逻辑'\n")
        (self.repo / "unrelated-link").symlink_to("missing-target")
        self.base = self.commit()
        (self.repo / self.path).write_text("base = '原始逻辑'\nmedia = True\n")
        self.head = self.commit()
        self.workspace = self.root / "workspace"
        (self.workspace / "src").mkdir(parents=True)
        (self.workspace / self.path).write_text("Editable conflict draft\n")
        self.candidate = {"commit": self.head, "target_base": self.base, "origin_candidate": self.head,
                          "repo": str(self.repo / ".git"), "conflicts": [self.path],
                          "snapshot_limits": {"max_files": 100, "max_bytes": 32_000_000, "max_paths": 1000}}
        self.work = {"id": "work_fixture", "requirements_revision": 19, "run_spec": {"write_paths": ["src"]}}

    def commit(self):
        self.git.run(["add", "--all"], cwd=self.repo)
        self.git.run(["commit", "-qm", "fixture source"], cwd=self.repo)
        return self.git.run(["rev-parse", "HEAD"], cwd=self.repo).decode().strip()

    def index(self, candidate=None):
        receipt = install(self.git, candidate or self.candidate, self.work, self.workspace)
        return receipt, json.loads((self.workspace / receipt["index"]).read_text())

    def test_exact_base_and_proposal_are_readable_immutable_and_do_not_change_draft(self):
        before = (self.workspace / self.path).read_bytes()
        original = copy.deepcopy(self.candidate)
        receipt, index = self.index()
        self.assertEqual(index["versions"]["target_base"]["commit"], self.base)
        self.assertEqual(index["versions"]["proposed_head"]["commit"], self.head)
        base = index["versions"]["target_base"]["files"][0]
        proposed = index["versions"]["proposed_head"]["files"][0]
        self.assertEqual(base["status"], "available")
        self.assertEqual(json.loads(run_read(base["snapshot"], self.workspace))["content"], "base = '原始逻辑'\n")
        self.assertIn("media = True", json.loads(run_read(proposed["snapshot"], self.workspace))["content"])
        self.assertEqual(hashlib.sha256((self.workspace / base["snapshot"]).read_bytes()).hexdigest(), base["sha256"])
        self.assertEqual(stat.S_IMODE((self.workspace / base["snapshot"]).stat().st_mode), 0o400)
        with self.assertRaisesRegex(ValueError, "host-managed"):
            run_write(base["snapshot"], "Wrong base", self.workspace)
        with self.assertRaisesRegex(ValueError, "host-managed"):
            run_edit(base["snapshot"], "base", "altered", self.workspace)
        with self.assertRaisesRegex(ValueError, "host-managed"):
            run_delete(base["snapshot"], self.workspace)
        self.assertEqual((self.workspace / self.path).read_bytes(), before)
        self.assertEqual(self.candidate, original)
        self.assertEqual(install(self.git, self.candidate, self.work, self.workspace), receipt)
        (self.workspace / self.path).write_text("Model changed its working draft\n")
        self.assertEqual(install(self.git, self.candidate, self.work, self.workspace), receipt)
        self.assertEqual((self.workspace / self.path).read_text(), "Model changed its working draft\n")

    def test_absent_and_unavailable_sources_are_explicit_observations(self):
        candidate = {**self.candidate, "conflicts": [self.path, "src/new.py"]}
        _, index = self.index(candidate)
        self.assertEqual(index["versions"]["target_base"]["files"][0]["status"], "absent")
        candidate = {**self.candidate, "target_base": "f" * 40}
        _, index = self.index(candidate)
        entry = index["versions"]["target_base"]["files"][0]
        self.assertEqual(entry["status"], "unavailable")
        self.assertIn("Controlled Git operation failed", entry["reason"])
        self.assertEqual(index["versions"]["proposed_head"]["files"][0]["status"], "available")

    def test_byte_budget_omits_evidence_without_deciding_or_touching_code(self):
        candidate = {**self.candidate, "snapshot_limits": {**self.candidate["snapshot_limits"], "max_bytes": 8}}
        before = (self.workspace / self.path).read_bytes()
        _, index = self.index(candidate)
        for version in index["versions"].values():
            entry = version["files"][0]
            self.assertEqual(entry["status"], "omitted")
            self.assertEqual(entry["limit_value"], 8)
            self.assertGreater(entry["bytes"], 8)
        self.assertEqual(index["staged_bytes"], 0)
        self.assertEqual((self.workspace / self.path).read_bytes(), before)

    def test_private_or_out_of_scope_paths_reject_before_publication(self):
        for path in ("../outside.py", ".harness/state.json", "outside.py", "src/.env"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                install(self.git, {**self.candidate, "conflicts": [path]}, self.work, self.workspace)
        self.assertFalse((self.workspace / ".harness").exists())

    def test_tampered_existing_snapshot_is_not_overwritten(self):
        _, index = self.index()
        path = self.workspace / index["versions"]["target_base"]["files"][0]["snapshot"]
        path.chmod(0o600)
        path.write_text("tampered")
        with self.assertRaisesRegex(ValueError, "Immutable conflict source evidence changed"):
            self.index()
        self.assertEqual(path.read_text(), "tampered")

    def test_normal_repair_does_not_install_unneeded_sources(self):
        self.assertIsNone(install(self.git, {**self.candidate, "conflicts": []}, self.work, self.workspace))
        self.assertFalse((self.workspace / ".harness").exists())

    def test_execution_timeout_is_not_reclassified_as_missing_evidence(self):
        with patch.object(self.git, "run", side_effect=TimeoutError("operation deadline")):
            with self.assertRaisesRegex(TimeoutError, "operation deadline"):
                self.index()
        self.assertFalse((self.workspace / ".harness").exists())

    def test_tree_object_is_not_presented_as_an_authoritative_commit(self):
        tree = self.git.run(["rev-parse", self.base + "^{tree}"], cwd=self.repo).decode().strip()
        _, index = self.index({**self.candidate, "target_base": tree})
        entry = index["versions"]["target_base"]["files"][0]
        self.assertEqual(entry["status"], "unavailable")
        self.assertIn("not a commit", entry["reason"])
        self.assertEqual(index["versions"]["proposed_head"]["files"][0]["status"], "available")

    def test_historical_symlink_is_reported_without_following_it(self):
        (self.repo / "src/link.py").symlink_to(str(self.root / "outside.py"))
        head = self.commit()
        _, index = self.index({**self.candidate, "origin_candidate": head, "conflicts": ["src/link.py"]})
        entry = index["versions"]["proposed_head"]["files"][0]
        self.assertEqual(entry["status"], "unavailable")
        self.assertNotIn("snapshot", entry)
        self.assertEqual(index["staged_bytes"], 0)

    def test_missing_proposed_history_does_not_label_the_integration_as_clean_source(self):
        candidate = dict(self.candidate)
        candidate.pop("origin_candidate")
        _, index = self.index(candidate)
        self.assertIsNone(index["versions"]["proposed_head"]["commit"])
        entry = index["versions"]["proposed_head"]["files"][0]
        self.assertEqual(entry["status"], "unavailable")
        self.assertIn("not recorded", entry["reason"])
        self.assertEqual(index["versions"]["target_base"]["files"][0]["status"], "available")
