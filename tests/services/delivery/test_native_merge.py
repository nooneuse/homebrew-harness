"""GitHub fixed-head native API and non-bypassable merge policy over real HTTP."""

import unittest
from unittest.mock import patch

from examples.delivery_fixture import Peer
from homebrew_harness.platforms.effects import EffectFailure
from homebrew_harness.platforms.github.delivery import GithubDelivery
from homebrew_harness.services.delivery.spec import validate
from tests.services.delivery import test_git as fixture


class NativeMergeTests(unittest.TestCase):
    run_git = fixture.GitTests.run_git
    candidate = fixture.GitTests.candidate

    def setUp(self):
        fixture.GitTests.setUp(self)
        self.proposed = self.candidate()
        self.backend.publish(self.proposed, "harness/proof", None)
        self.peer = Peer(self.git, self.remote)
        self.addCleanup(self.peer.close)
        self.peer.pulls.append(
            {"number": 1, "head": {"ref": "harness/proof"}, "base": {"ref": "main"}}
        )
        self.spec = validate(
            {
                **self.spec,
                "merge": {"mode": "service_approved"},
                "required_checks": ["CI"],
                "check_app_ids": [11],
            }
        )
        self.adapter = GithubDelivery(
            self.spec, {**self.values, "api_url": self.peer.url}
        )
        self.addCleanup(self.adapter.close)
        self.plan = {
            "subject": {
                "provider": "github",
                "instance": "primary",
                "project": "example/project",
                "kind": "change_request",
                "id": "1",
                "number": 1,
            },
            "commit": self.proposed["commit"],
            "branch": "harness/proof",
            "method": "merge",
            "hash": "f" * 64,
            "gate_hash": "a" * 64,
            "title": "Reviewed change",
            "body": "Verified fixed candidate.",
        }

    def test_native_merge_returns_actual_commit_and_read_reconciliation(self):
        result = self.adapter.merge_change_request(self.plan)
        self.assertEqual(result["state"], "merged")
        self.assertNotEqual(result["result_commit"], self.plan["commit"])
        self.assertEqual(result, self.adapter.inspect_merge(self.plan))
        self.assertEqual(result["actor_id"], 12)
        self.assertEqual(sum(kind == "merge" for kind, _ in self.peer.writes), 1)

    def test_sha_condition_rejects_head_race_at_the_platform(self):
        original = self.adapter.request

        def race(method, *args, **kwargs):
            if method == "PUT":
                repo = "--git-dir=" + str(self.remote)
                newer = (
                    self.run_git(
                        repo,
                        "commit-tree",
                        self.proposed["tree"],
                        "-p",
                        self.proposed["commit"],
                        "-m",
                        "new head",
                    )
                    .decode()
                    .strip()
                )
                self.run_git(repo, "update-ref", "refs/heads/harness/proof", newer)
            return original(method, *args, **kwargs)

        with (
            patch.object(self.adapter, "request", race),
            self.assertRaises(EffectFailure) as error,
        ):
            self.adapter.merge_change_request(self.plan)
        self.assertEqual(error.exception.outcome, "rejected")
        self.assertEqual(error.exception.status, 409)
        self.assertFalse(self.peer.writes)
        self.assertEqual(self.backend.ref("main"), self.proposed["target_base"])

    def test_lost_ack_preserves_unknown_but_read_confirms_without_resend(self):
        self.peer.drop = "merge"
        with self.assertRaises(EffectFailure) as error:
            self.adapter.merge_change_request(self.plan)
        self.assertEqual(error.exception.outcome, "unknown")
        self.assertEqual(self.adapter.inspect_merge(self.plan)["state"], "merged")
        self.assertEqual(sum(kind == "merge" for kind, _ in self.peer.writes), 1)

    def test_open_pr_does_not_prove_an_unexecuted_attempt(self):
        self.assertIsNone(self.adapter.inspect_merge(self.plan))
        self.assertFalse(self.peer.writes)

    def test_non_strict_or_bypassable_legacy_policy_rejects_without_writing(self):
        for protection in (
            {
                "required_status_checks": {"contexts": ["CI"], "strict": False},
                "required_pull_request_reviews": {},
                "enforce_admins": {"enabled": True},
            },
            {
                "required_status_checks": {"contexts": ["CI"], "strict": True},
                "required_pull_request_reviews": {
                    "bypass_pull_request_allowances": {"users": [{"id": 12}]}
                },
                "enforce_admins": {"enabled": True},
            },
        ):
            self.peer.protection = protection
            with self.assertRaises(EffectFailure) as error:
                self.adapter.merge_change_request(self.plan)
            self.assertEqual(error.exception.outcome, "not_executed")
            self.assertFalse(self.peer.writes)

    def test_effective_rulesets_supply_strict_pr_merge_policy(self):
        common = {"ruleset_id": 1, "ruleset_source_type": "Repository"}
        self.peer.branch_rules = [
            {**common, "type": "pull_request"},
            {**common, "type": "non_fast_forward"},
            {
                **common,
                "type": "required_status_checks",
                "parameters": {
                    "required_status_checks": [{"context": "CI"}],
                    "strict_required_status_checks_policy": True,
                },
            },
        ]
        self.peer.rulesets = {
            1: {
                "enforcement": "active",
                "bypass_actors": [],
                "rules": self.peer.branch_rules,
            }
        }
        self.peer.protection = None
        self.assertEqual(
            self.adapter.merge_change_request(self.plan)["state"], "merged"
        )

    def test_ruleset_change_or_queue_never_falls_back_to_direct_push(self):
        common = {"ruleset_id": 1, "ruleset_source_type": "Repository"}
        self.peer.branch_rules = [{**common, "type": "merge_queue"}]
        self.peer.rulesets = {
            1: {
                "enforcement": "active",
                "bypass_actors": [],
                "rules": self.peer.branch_rules,
            }
        }
        with self.assertRaises(EffectFailure):
            self.adapter.merge_change_request(self.plan)
        self.assertFalse(self.peer.writes)
        self.peer.branch_rules = [
            {
                **common,
                "type": "required_status_checks",
                "parameters": {"strict_required_status_checks_policy": True},
            }
        ]
        self.peer.rulesets[1]["rules"] = []
        with self.assertRaises(EffectFailure):
            self.adapter.merge_change_request(self.plan)
        self.assertFalse(self.peer.writes)

    def test_foreign_scope_method_and_implicit_closing_are_rejected(self):
        for fields in ({"method": "squash"}, {"branch": "main"}, {"body": "Fixes #1"}):
            with self.subTest(fields=fields), self.assertRaises(EffectFailure):
                self.adapter.merge_change_request({**self.plan, **fields})
        self.assertFalse(self.peer.writes)
