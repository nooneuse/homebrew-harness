import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

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
        self.baseline = {"files": snapshot(self.workspace)}
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

    def test_changed_target_is_not_overwritten(self):
        candidate = self.candidate()
        with self.assertRaises(StaleDelivery):
            self.backend.publish(candidate, "harness/review", "0" * 40)

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
