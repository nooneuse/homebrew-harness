"""An open PR's base snapshot is distinct from its live target reference."""

import copy
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call

from homebrew_harness.platforms.github.merge import metadata


class GithubMetadataTests(unittest.TestCase):
    def fixture(self, *, closed=False, merged=False, branch="main"):
        spec = {"provider": "github", "instance": "primary", "project": "example/project"}
        subject = {**spec, "kind": "change_request", "id": "1", "number": 1}
        row = {"head": {"sha": "a" * 40, "ref": "harness/work", "repo": {"full_name": spec["project"]}},
               "base": {"sha": "b" * 40, "ref": branch, "repo": {"full_name": spec["project"]}},
               "state": "closed" if closed else "open", "merged": merged,
               "merge_commit_sha": "d" * 40 if merged else None}
        target = {"ref": "refs/heads/" + branch, "object": {"type": "commit", "sha": "c" * 40}}
        adapter = SimpleNamespace(spec=spec, prefix="repos/example/project", request=Mock(side_effect=[row, target]))
        return adapter, subject, row, target

    def test_open_pr_uses_the_live_target_without_mutating_the_snapshot(self):
        adapter, subject, row, _ = self.fixture()
        original = copy.deepcopy(row)
        value = metadata(adapter, subject)
        self.assertEqual(value["base"], "c" * 40)
        self.assertEqual(value["head"], "a" * 40)
        self.assertEqual(row, original)
        self.assertEqual(adapter.request.call_args_list, [
            call("GET", "repos/example/project/pulls/1"),
            call("GET", "repos/example/project/git/ref/heads/main")])

    def test_target_branch_is_encoded_as_one_reference(self):
        adapter, subject, _, _ = self.fixture(branch="topic/next")
        self.assertEqual(metadata(adapter, subject)["base"], "c" * 40)
        self.assertEqual(adapter.request.call_args.args[1],
                         "repos/example/project/git/ref/heads/topic%2Fnext")

    def test_closed_or_merged_history_does_not_require_a_live_branch(self):
        for merged in (False, True):
            with self.subTest(merged=merged):
                adapter, subject, _, _ = self.fixture(closed=True, merged=merged)
                value = metadata(adapter, subject)
                self.assertEqual(value["base"], "b" * 40)
                self.assertEqual(adapter.request.call_count, 1)

    def test_a_different_reference_cannot_supply_the_target_commit(self):
        adapter, subject, _, target = self.fixture()
        target["ref"] = "refs/heads/other"
        with self.assertRaisesRegex(ValueError, "current PR target reference"):
            metadata(adapter, subject)

    def test_a_non_commit_object_cannot_supply_the_target(self):
        adapter, subject, _, target = self.fixture()
        target["object"]["type"] = "tag"
        with self.assertRaisesRegex(ValueError, "current PR target reference"):
            metadata(adapter, subject)
