import shutil
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from homebrew_harness.services.delivery.git import GitBackend, StaleDelivery
from homebrew_harness.services.delivery.spec import validate, read_values
from homebrew_harness.execution.source_snapshot import snapshot


class GitTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.workspace = self.base / "project"
        self.workspace.mkdir()
        self.control = self.base / "control"
        self.control.mkdir(mode=0o700)
        self.git = shutil.which("git")
        self.run_git("init", "-q", cwd=self.workspace)
        (self.workspace / "module.py").write_text("value=1\n")
        self.run_git("add", "module.py", cwd=self.workspace)
        self.run_git("commit", "-qm", "baseline", cwd=self.workspace)
        self.run_git("branch", "-M", "main", cwd=self.workspace)
        self.remote = self.base / "remote.git"
        self.run_git("clone", "--bare", str(self.workspace), str(self.remote))
        self.spec = validate(
            {
                "mode": "local",
                "git_executable": self.git,
                "provider": "github",
                "instance": "primary",
                "project": "example/project",
                "remote_env": "REMOTE",
                "api_url_env": "API",
                "api_token_env": "TOKEN",
                "author_name_env": "AUTHOR",
                "author_email_env": "EMAIL",
                "required_tests": ["unit-tests"],
            }
        )
        self.values = read_values(
            self.spec,
            {
                "REMOTE": str(self.remote),
                "API": "http://127.0.0.1:9",
                "TOKEN": "synthetic",
                "AUTHOR": "fixture",
                "EMAIL": "fixture@invalid",
            },
            (self.workspace, self.control),
        )
        self.backend = GitBackend(self.control, self.spec, self.values)
        self.addCleanup(self.backend.close)
        self.baseline = {
            "files": snapshot(self.workspace),
            "head": self.backend.head(self.workspace),
        }
        self.work = {
            "id": "work_fixture",
            "run_spec": {
                "write_paths": ["."],
                "snapshot_limits": {
                    "max_files": 5000,
                    "max_bytes": 32000000,
                    "max_paths": 100000,
                },
            },
        }

    def run_git(self, *args, cwd=None):
        return subprocess.check_output(
            [
                self.git,
                "-c",
                "user.name=fixture",
                "-c",
                "user.email=fixture@invalid",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.hooksPath=/dev/null",
                *args,
            ],
            cwd=cwd or self.base,
            stderr=subprocess.DEVNULL,
        )

    def candidate(self):
        (self.workspace / "module.py").write_text("value=2\n")
        return self.backend.prepare(
            self.work, self.workspace, self.baseline, self.base / "candidate"
        )

    def test_candidate_and_target_are_fixed_and_published_with_cas(self):
        candidate = self.candidate()
        self.assertEqual(
            (Path(candidate["workspace"]) / "module.py").read_text(), "value=2\n"
        )
        self.assertEqual(self.backend.ref("main"), candidate["target_base"])
        result = self.backend.publish(candidate, "harness/review", None)
        self.assertTrue(result["confirmed"])
        self.assertEqual(self.backend.ref("harness/review"), candidate["commit"])
        self.assertEqual(self.backend.ref("main"), candidate["target_base"])
        self.assertEqual(
            result, self.backend.publish(candidate, "harness/review", None)
        )

    def test_candidate_workspace_reports_only_actual_changes(self):
        from homebrew_harness.tools.queries import git_status, git_diff

        candidate = self.candidate()
        workspace = Path(candidate["workspace"])
        self.assertEqual(json.loads(git_status(workdir=workspace))["files"], [])
        self.assertEqual((workspace / "module.py").read_text(), "value=2\n")
        (workspace / "module.py").write_text("value=3\n")
        status = json.loads(git_status(workdir=workspace))
        self.assertEqual(status["files"], [{"status": " M", "path": "module.py"}])
        diff = json.loads(git_diff(workdir=workspace, paths=["module.py"]))
        self.assertIn("-value=2\n+value=3", diff["diff"])
        self.assertFalse(diff["truncated"])

    def test_repair_review_keeps_the_entire_proposed_feature(self):
        from homebrew_harness.services.job_contracts import JobContract

        (self.workspace / "feature.py").write_text("feature = True\n")
        first = self.candidate()
        workspace = Path(first["workspace"])
        (workspace / "module.py").write_text("value=3\n")
        repaired = self.backend.prepare(
            self.work, workspace, {"files": first["files"], "head": first["commit"]},
            self.base / "repair-candidate", previous=first,
        )
        self.assertEqual(set(repaired["delta"]), {"module.py"})
        store = SimpleNamespace(
            id="service_fixture", delivery=SimpleNamespace(git=self.backend),
            store=SimpleNamespace(work=lambda *args: self.work),
        )
        primary = {"id": "changeset_repair", "work_id": self.work["id"],
                   "previous": "changeset_feature", "candidate": repaired}
        contract = JobContract(store, {"id": "job_review", "kind": "review", "target": primary["id"]})
        projected = contract.input(primary)["candidate"]
        self.assertEqual(set(projected["delta"]), {"module.py", "feature.py"})
        self.assertEqual(projected["repair_delta"], repaired["delta"])
        self.assertEqual(set(repaired["delta"]), {"module.py"})

    def test_delayed_git_stdin_preserves_large_input_across_poll_boundaries(self):
        from homebrew_harness.execution.model_gateway import Deadline

        real_popen = subprocess.Popen

        def delayed(argv, **kwargs):
            return real_popen(
                [
                    sys.executable,
                    "-c",
                    "import os,sys,time; time.sleep(.15); os.execv(sys.argv[1],sys.argv[1:])",
                    *argv,
                ],
                **kwargs
            )

        data = b"large source input\n" * 10000
        with patch(
            "homebrew_harness.services.delivery.git.subprocess.Popen",
            side_effect=delayed,
        ):
            actual = self.backend.run(
                ["hash-object", "--stdin"], data=data, deadline=Deadline.after(3)
            )
        expected = hashlib.sha1(
            b"blob " + str(len(data)).encode() + b"\0" + data
        ).hexdigest()
        self.assertEqual(actual.decode().strip(), expected)

    def test_changed_target_is_not_overwritten(self):
        candidate = self.candidate()
        with self.assertRaises(StaleDelivery):
            self.backend.publish(candidate, "harness/review", "0" * 40)

    def advance_target(self, path, contents):
        other = self.base / "upstream"
        self.run_git("clone", str(self.remote), str(other))
        (other / path).write_text(contents)
        self.run_git("add", path, cwd=other)
        self.run_git("commit", "-qm", "upstream", cwd=other)
        self.run_git("push", "origin", "HEAD:main", cwd=other)
        return self.backend.ref("main")

    def test_first_candidate_integrates_advanced_target_without_changing_original_base(
        self,
    ):
        original = self.baseline["head"]
        target = self.advance_target("upstream.txt", "preserve upstream")
        candidate = self.candidate()
        self.assertEqual(candidate["target_base"], target)
        self.assertEqual(self.baseline["head"], original)
        self.assertEqual(
            (Path(candidate["workspace"]) / "upstream.txt").read_text(),
            "preserve upstream",
        )
        self.assertEqual(set(candidate["delta"]), {"module.py"})
        self.assertFalse(candidate["conflicts"])

    def test_conflicting_target_is_exposed_without_choosing_a_side(self):
        target = self.advance_target("module.py", "value=3\n")
        candidate = self.candidate()
        self.assertEqual(candidate["conflicts"], ["module.py"])
        self.assertIn(
            "<<<<<<<", (Path(candidate["workspace"]) / "module.py").read_text()
        )
        self.assertEqual(self.backend.ref("main"), target)

    def test_changes_already_in_target_are_not_repeated_in_delta(self):
        self.advance_target("module.py", "value=2\n")
        candidate = self.candidate()
        self.assertEqual(candidate["delta"], {})
        self.assertFalse(candidate["conflicts"])

    def test_candidate_mutation_invalidates_delivery(self):
        candidate = self.candidate()
        (Path(candidate["workspace"]) / "module.py").write_text("tampered")
        with self.assertRaises(StaleDelivery):
            self.backend.publish(candidate, "harness/review", None)

    def test_delta_does_not_include_preexisting_uncommitted_work(self):
        (self.workspace / "unrelated.txt").write_text("another work")
        baseline = {"files": snapshot(self.workspace)}
        (self.workspace / "module.py").write_text("value=3\n")
        candidate = self.backend.prepare(
            self.work, self.workspace, baseline, self.base / "delta"
        )
        self.assertFalse((Path(candidate["workspace"]) / "unrelated.txt").exists())
        self.assertEqual(set(candidate["delta"]), {"module.py"})

    def test_explicit_existing_draft_is_included_without_publishing_other_drafts(self):
        (self.workspace / "accepted.txt").write_text("accepted proposal")
        (self.workspace / "unrelated.txt").write_text("another work")
        baseline = {"files": snapshot(self.workspace), "head": self.baseline["head"]}
        candidate = self.backend.prepare(
            self.work | {"included_inputs": ["accepted.txt"]},
            self.workspace,
            baseline,
            self.base / "included",
        )
        self.assertEqual(set(candidate["delta"]), {"accepted.txt"})
        self.assertFalse((Path(candidate["workspace"]) / "unrelated.txt").exists())

    def test_target_rename_cannot_expand_the_approved_publication_paths(self):
        other = self.base / "upstream"
        self.run_git("clone", str(self.remote), str(other))
        self.run_git("mv", "module.py", "outside.py", cwd=other)
        self.run_git("commit", "-qm", "rename", cwd=other)
        self.run_git("push", "origin", "HEAD:main", cwd=other)
        (self.workspace / "module.py").write_text("value=2\n")
        self.work["run_spec"]["write_paths"] = ["module.py"]
        with self.assertRaisesRegex(ValueError, "approved paths"):
            self.backend.prepare(
                self.work, self.workspace, self.baseline, self.base / "renamed"
            )

    def test_hooks_and_filters_are_not_executed(self):
        marker = self.base / "hook-executed"
        script = self.base / "danger.sh"
        script.write_text("#!/bin/sh\ntouch " + str(marker) + "\n")
        script.chmod(0o755)
        self.run_git("config", "core.hooksPath", str(self.base), cwd=self.workspace)
        (self.base / "post-checkout").write_bytes(script.read_bytes())
        (self.base / "post-checkout").chmod(0o755)
        self.run_git("config", "filter.danger.clean", str(script), cwd=self.workspace)
        (self.workspace / ".gitattributes").write_text("*.py filter=danger\n")
        self.candidate()
        self.assertFalse(marker.exists())

    def test_git_publication_always_refuses_the_target_branch(self):
        candidate = self.candidate()
        with self.assertRaises(ValueError):
            self.backend.publish(candidate, "main", candidate["target_base"])
        self.assertEqual(self.backend.ref("main"), candidate["target_base"])
