"""Actual native merge histories, including squash/rebase and moving targets."""

import unittest
from pathlib import Path

from examples.delivery_fixture import Peer
from homebrew_harness.services.delivery.integration import prove
from tests.services.delivery import test_git as fixture


class IntegrationTests(unittest.TestCase):
    setUp = fixture.GitTests.setUp
    run_git = fixture.GitTests.run_git
    candidate = fixture.GitTests.candidate

    def publish(self, candidate):
        self.backend.publish(
            candidate, "harness/proof", self.backend.ref("harness/proof")
        )
        peer = Peer(self.git, self.remote)
        self.addCleanup(peer.close)
        peer.pulls.append(
            {"number": 1, "head": {"ref": "harness/proof"}, "base": {"ref": "main"}}
        )
        return peer

    def check(self, method):
        candidate = self.candidate()
        peer = self.publish(candidate)
        result = peer.merge_pull(1, method=method)
        actual = prove(
            self.backend,
            candidate,
            {
                "merged": True,
                "head": candidate["commit"],
                "result_commit": result,
                "method": method,
            },
            self.base / "integration",
        )
        self.assertEqual(actual["commit"], result)
        self.assertEqual(
            (Path(actual["workspace"]) / "module.py").read_text(), "value=2\n"
        )
        self.assertEqual(self.backend.head(candidate["workspace"]), candidate["commit"])
        self.backend.verify(actual)
        return candidate, peer, actual

    def test_merge_result_is_fixed_and_reused_without_mutating_candidate(self):
        candidate, _, actual = self.check("merge")
        self.assertNotEqual(actual["commit"], candidate["commit"])
        self.assertEqual(actual["proof"], "merge_tree")
        repeated = prove(
            self.backend,
            candidate,
            {
                "merged": True,
                "head": candidate["commit"],
                "result_commit": actual["commit"],
            },
            self.base / "integration",
        )
        self.assertEqual(repeated, actual)

    def test_squash_does_not_require_the_source_commit_in_target_history(self):
        candidate, _, actual = self.check("squash")
        self.assertNotEqual(actual["commit"], candidate["commit"])
        self.assertEqual(actual["proof"], "squash_tree")

    def test_rebase_replays_content_and_maps_rewritten_commits(self):
        candidate, _, actual = self.check("rebase")
        self.assertNotEqual(actual["commit"], candidate["commit"])
        self.assertEqual(actual["proof"], "rebase_replay")

    def test_multi_commit_rebase_checks_the_complete_source_range(self):
        first = self.candidate()
        self.backend.publish(first, "harness/proof", None)
        baseline = {"files": fixture.snapshot(self.workspace)}
        (self.workspace / "module.py").write_text("value=3\n")
        second = self.backend.prepare(
            self.work, self.workspace, baseline, self.base / "repair", previous=first
        )
        peer = self.publish(second)
        result = peer.merge_pull(1, method="rebase")
        actual = prove(
            self.backend,
            second,
            {
                "merged": True,
                "head": second["commit"],
                "result_commit": result,
                "method": "rebase",
            },
            self.base / "integration",
        )
        self.assertEqual(actual["proof"], "rebase_replay")
        self.assertEqual(len(actual["source_commits"]), 2)
        self.assertEqual(
            (Path(actual["workspace"]) / "module.py").read_text(), "value=3\n"
        )

    def test_target_can_advance_after_the_verified_merge(self):
        candidate, _, actual = self.check("merge")
        repo = "--git-dir=" + str(self.remote)
        later = (
            self.run_git(
                repo,
                "commit-tree",
                actual["tree"],
                "-p",
                actual["commit"],
                "-m",
                "later contribution",
            )
            .decode()
            .strip()
        )
        self.run_git(repo, "update-ref", "refs/heads/main", later, actual["commit"])
        repeated = prove(
            self.backend,
            candidate,
            {
                "merged": True,
                "head": candidate["commit"],
                "result_commit": actual["commit"],
            },
            self.base / "integration",
        )
        self.assertEqual(repeated["commit"], actual["commit"])
        self.assertEqual(repeated["target_tip"], later)

    def test_unknown_or_wrong_merge_content_is_not_delivery_evidence(self):
        candidate = self.candidate()
        peer = self.publish(candidate)
        result = peer.merge_pull(1)
        for head, merged in (("f" * 40, True), (candidate["commit"], False)):
            with self.assertRaises(ValueError):
                prove(
                    self.backend,
                    candidate,
                    {"head": head, "merged": merged, "result_commit": result},
                    self.base / "wrong",
                )

    def test_unreviewed_extra_merge_content_is_rejected(self):
        candidate = self.candidate()
        self.publish(candidate)
        repo = "--git-dir=" + str(self.remote)
        # Native metadata alone cannot turn another tree into the accepted work.
        wrong_tree = (
            self.run_git(repo, "rev-parse", candidate["target_base"] + "^{tree}")
            .decode()
            .strip()
        )
        result = (
            self.run_git(
                repo,
                "commit-tree",
                wrong_tree,
                "-p",
                candidate["target_base"],
                "-p",
                candidate["commit"],
                "-m",
                "wrong merge",
            )
            .decode()
            .strip()
        )
        self.run_git(repo, "update-ref", "refs/heads/main", result)
        with self.assertRaises(ValueError):
            prove(
                self.backend,
                candidate,
                {"head": candidate["commit"], "merged": True, "result_commit": result},
                self.base / "wrong",
            )

    def test_explicit_rebase_refresh_rewrites_only_the_pinned_review_head(self):
        from homebrew_harness.services.delivery.integration import refresh_candidate

        candidate = self.candidate()
        self.publish(candidate)
        (self.workspace / "upstream.txt").write_text("upstream\n")
        self.run_git("add", "upstream.txt", cwd=self.workspace)
        self.run_git("commit", "-qm", "upstream", cwd=self.workspace)
        base = self.run_git("rev-parse", "HEAD", cwd=self.workspace).decode().strip()
        repo = "--git-dir=" + str(self.remote)
        self.run_git(repo, "fetch", str(self.workspace), "HEAD")
        self.run_git(repo, "update-ref", "refs/heads/main", base)
        refreshed, previous = refresh_candidate(
            self.backend,
            candidate,
            {
                "head": candidate["commit"],
                "base": base,
                "source_branch": "harness/proof",
            },
            self.base / "refreshed",
            linear=True,
        )
        self.assertTrue(refreshed["review_rewrite"])
        with self.assertRaises(ValueError):
            self.backend.publish(refreshed, "main", base)
        self.backend.publish(refreshed, "harness/proof", previous)
        self.assertEqual(self.backend.ref("main"), base)
        self.assertEqual(self.backend.ref("harness/proof"), refreshed["commit"])
