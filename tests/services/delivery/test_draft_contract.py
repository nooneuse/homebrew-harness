"""Draft publication is source-bound; readiness still requires real quality."""

import unittest
from homebrew_harness.platforms.github.delivery import GithubDelivery


class DraftAdapter(unittest.TestCase):
    def test_new_request_carries_draft_and_ready_transition_is_confirmed(self):
        adapter = object.__new__(GithubDelivery)
        adapter.spec = {
            "project": "example/project",
            "provider": "github",
            "instance": "primary",
            "target_branch": "main",
        }
        adapter.prefix = "repos/example/project"
        import threading

        adapter.lock = threading.RLock()
        calls = []
        row = {
            "number": 3,
            "node_id": "PR_fixture",
            "head": {"ref": "harness/feature", "sha": "a" * 40},
            "base": {"ref": "main"},
            "state": "open",
            "body": "",
            "draft": True,
        }
        adapter.pages = lambda *args: []

        def request(method, path, body=None, params=None):
            calls.append((method, path, body))
            if path == "graphql":
                return {
                    "data": {
                        "markPullRequestReadyForReview": {
                            "pullRequest": {"id": "PR_fixture", "isDraft": False}
                        }
                    }
                }
            if method == "POST":
                row.update(body=body["body"], draft=body["draft"])
            return dict(row)

        adapter.request = request
        plan = {
            "correlation": "first",
            "title": "WIP feature",
            "body": "Verification pending.",
            "branch": "harness/feature",
            "commit": "a" * 40,
            "draft": True,
        }
        draft = adapter.ensure_change_request(plan)
        self.assertTrue(draft["confirmed"])
        self.assertTrue(calls[0][2]["draft"])
        plan.update(
            draft=False,
            existing={
                "provider": "github",
                "instance": "primary",
                "project": "example/project",
                "kind": "change_request",
                "id": "3",
                "number": 3,
            },
            previous_correlation="first",
            correlation="ready",
        )
        ready = adapter.ensure_change_request(plan)
        self.assertTrue(ready["confirmed"])
        self.assertTrue(any(path == "graphql" for _, path, _ in calls))

    def test_enterprise_graphql_uses_same_host_api_endpoint(self):
        adapter = object.__new__(GithubDelivery)
        adapter.values = {"api_url": "https://git.example.test/api/v3/"}
        self.assertEqual(adapter.graphql_path(), "https://git.example.test/api/graphql")
        adapter.values = {"api_url": "https://api.github.com"}
        self.assertEqual(adapter.graphql_path(), "https://api.github.com/graphql")

    def test_reconcile_never_mutates_a_draft_state_mismatch(self):
        import threading

        adapter = object.__new__(GithubDelivery)
        adapter.lock = threading.RLock()
        adapter.spec = {
            "project": "example/project",
            "provider": "github",
            "instance": "primary",
            "target_branch": "main",
        }
        adapter.prefix = "repos/example/project"
        adapter.pages = lambda *args: [
            {
                "number": 3,
                "body": adapter.marker("once"),
                "head": {"ref": "feature", "sha": "a" * 40},
                "base": {"ref": "main"},
                "draft": True,
            }
        ]
        adapter.request = lambda *args: self.fail(
            "Read-only reconciliation must not repeat publication."
        )
        self.assertIsNone(
            adapter.ensure_change_request(
                {
                    "correlation": "once",
                    "title": "Ready",
                    "body": "Tests passed.",
                    "branch": "feature",
                    "commit": "a" * 40,
                    "draft": False,
                },
                reconcile=True,
            )
        )
