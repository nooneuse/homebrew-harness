"""Periodic integration against real Git refs in a local fixture remote."""

import unittest
from pathlib import Path
from unittest.mock import patch

from homebrew_harness.runtime.errors import error_info
from homebrew_harness.services.delivery.integration import (
    probe_candidate,
    refresh_candidate,
)
from tests.services.delivery import test_git as fixture


class PeriodicGitTests(unittest.TestCase):
    setUp = fixture.GitTests.setUp
    run_git = fixture.GitTests.run_git
    candidate = fixture.GitTests.candidate
    source_branch = "harness/periodic"

    def advance(self, **files):
        for name, contents in files.items():
            (self.workspace / name).write_text(contents)
        self.run_git("add", "--", *files, cwd=self.workspace)
        self.run_git("commit", "-qm", "target update", cwd=self.workspace)
        self.run_git("push", str(self.remote), "HEAD:main", cwd=self.workspace)
        return self.backend.ref("main")

    def observe(self, candidate, base):
        return {
            "head": candidate["commit"],
            "base": base,
            "source_branch": self.source_branch,
        }

    def test_periodic_prepare_keeps_the_development_base(self):
        self.spec["base_update"] = "periodic"
        tip = self.advance(**{"module.py": "value=3\n"})
        candidate = self.candidate()
        self.assertEqual(candidate["parent"], self.baseline["head"])
        self.assertEqual(candidate["target_base"], self.baseline["head"])
        self.assertEqual(candidate["conflicts"], [])
        self.assertEqual(self.backend.ref("main"), tip)
        self.backend.verify(candidate)

    def test_probe_reads_pinned_refs_and_reports_only_real_paths(self):
        unusual = "notes\twith space.txt"
        (self.workspace / unusual).write_text("candidate addition\n")
        candidate = self.candidate()
        self.backend.publish(candidate, self.source_branch, None)
        clean = self.advance(**{"upstream.txt": "upstream\n"})
        conflict = self.advance(**{"module.py": "value=3\n", unusual: "target addition\n"})
        candidate_fetch = Path(candidate["repo"]) / "FETCH_HEAD"
        before = candidate_fetch.read_bytes()
        refs = self.run_git("--git-dir=" + str(self.remote), "show-ref")

        # The remote has advanced to a conflict, but the earlier observation is clean.
        self.assertEqual(probe_candidate(self.backend, candidate, self.observe(candidate, clean)), [])
        with patch.object(self.backend, "run", wraps=self.backend.run) as run:
            paths = probe_candidate(self.backend, candidate, self.observe(candidate, conflict))
        self.assertCountEqual(paths, ["module.py", unusual])
        init, *_ = run.call_args_list
        scratch = Path(init.args[0][-1])
        self.assertIn("--bare", init.args[0])
        self.assertFalse(scratch.parent.exists())
        fetches = [
            (call.args[0][-1], call.kwargs.get("credentials"))
            for call in run.call_args_list
            if call.args[0][1] == "fetch"
        ]
        self.assertEqual(fetches, [("refs/heads/main", True), ("refs/heads/" + self.source_branch, True)])
        self.assertEqual(candidate_fetch.read_bytes(), before)
        self.assertEqual(self.run_git("--git-dir=" + str(self.remote), "show-ref"), refs)
        self.backend.verify(candidate)

    def test_fixed_refresh_materializes_observed_conflict_not_newer_clean_tip(self):
        candidate = self.candidate()
        self.backend.publish(candidate, self.source_branch, None)
        base = self.advance(**{"module.py": "value=3\n"})
        tip = self.advance(**{"module.py": "value=2\n", "later.txt": "new tip\n"})
        observed = self.observe(candidate, base)
        refreshed, published = refresh_candidate(
            self.backend, candidate, observed, self.base / "fixed", fixed_target=True,
        )
        self.assertEqual(refreshed["target_base"], base)
        self.assertEqual(refreshed["conflicts"], ["module.py"])
        workspace = Path(refreshed["workspace"])
        self.assertIn("<<<<<<<", (workspace / "module.py").read_text())
        self.assertFalse((workspace / "later.txt").exists())
        self.assertEqual(published, candidate["commit"])
        self.assertEqual(self.backend.ref("main"), tip)
        self.assertEqual(self.backend.ref(self.source_branch), published)
        self.assertEqual(
            self.run_git("rev-list", "--parents", "-n", "1", "HEAD", cwd=workspace)
            .decode().split(),
            [refreshed["commit"], published, base],
        )
        self.backend.verify(refreshed)

    def test_fixed_paths_report_a_rewritten_target(self):
        candidate = self.candidate()
        self.backend.publish(candidate, self.source_branch, None)
        head = candidate["commit"]
        available = candidate["target_base"]  # Still available via the source branch.
        missing = self.advance(**{"upstream.txt": "only on the old target\n"})
        repo = "--git-dir=" + str(self.remote)
        unrelated = self.run_git(
            repo, "commit-tree", available + "^{tree}", "-m", "new root",
        ).decode().strip()
        self.run_git(repo, "update-ref", "refs/heads/main", unrelated)

        for base in (available, missing):
            with self.subTest(base=base):
                observed = self.observe(candidate, base)
                with self.assertRaisesRegex(ValueError, "not reachable from the current target") as probe:
                    probe_candidate(self.backend, candidate, observed)
                with self.assertRaisesRegex(ValueError, "not reachable from the current target") as refresh:
                    refresh_candidate(
                        self.backend, candidate, observed, self.base / ("rewritten-" + base),
                        fixed_target=True,
                    )
                for result in (probe, refresh):
                    info = error_info(result.exception)
                    self.assertEqual(info["code"], "integration_target_rewritten")
                    self.assertEqual(info["details"]["expected_version"], f"head={head};base={base}")
                    self.assertEqual(info["details"]["observed_version"], f"head={head};base={unrelated}")

        with self.assertRaisesRegex(ValueError, "PR refs changed during candidate refresh") as manual:
            refresh_candidate(
                self.backend, candidate, self.observe(candidate, available), self.base / "manual-rewritten",
            )
        self.assertEqual(error_info(manual.exception)["code"], "pr_version_changed")

    def test_fixed_paths_reject_an_external_source_integration(self):
        candidate = self.candidate()
        self.backend.publish(candidate, self.source_branch, None)
        base = self.advance(**{"upstream.txt": "upstream\n"})
        repo = "--git-dir=" + str(self.remote)
        tree = self.run_git(repo, "merge-tree", "--write-tree", base, candidate["commit"]).decode().strip()
        external = self.run_git(
            repo, "commit-tree", tree, "-p", candidate["commit"], "-p", base, "-m", "external integration",
        ).decode().strip()
        self.run_git(repo, "update-ref", "refs/heads/" + self.source_branch, external)
        stale = self.observe(candidate, base)
        current = {**stale, "head": external}
        for observed in (stale, current):
            with self.subTest(head=observed["head"]):
                with self.assertRaises(ValueError) as probe_error:
                    probe_candidate(self.backend, candidate, observed)
                self.assertEqual(probe_error.exception.code, "pr_version_changed")
                with self.assertRaises(ValueError) as refresh_error:
                    refresh_candidate(
                        self.backend, candidate, observed, self.base / "changed", fixed_target=True,
                    )
                self.assertEqual(refresh_error.exception.code, "pr_version_changed")
        # Legacy refresh still allows the same deterministic external integration.
        refreshed, published = refresh_candidate(
            self.backend, candidate, current, self.base / "default",
        )
        self.assertEqual((refreshed["commit"], published), (external, external))
