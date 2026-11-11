"""Merge decisions expose exact typed action references to Python and HTTP clients."""

import unittest
from types import SimpleNamespace

from homebrew_harness.services.request_views import project


class MergeRequestProjection(unittest.TestCase):
    def test_merge_request_carries_action_pr_head_and_method(self):
        subject = {
            "provider": "github",
            "instance": "primary",
            "project": "example/project",
            "kind": "change_request",
            "id": "1",
            "number": 1,
        }
        action = {
            "state": "awaiting_approval",
            "plan": {"subject": subject, "commit": "a" * 40, "method": "squash"},
        }
        store = SimpleNamespace(
            id="service", get=lambda kind, identity: action if kind == "actions" else {}
        )
        maintenance = SimpleNamespace(
            store=store,
            delivery=SimpleNamespace(store=SimpleNamespace(current=lambda row: row)),
            runtime=SimpleNamespace(
                store=SimpleNamespace(
                    work=lambda identity, owner: {"requirements_revision": 0}
                )
            ),
        )
        value = {
            "id": "request",
            "kind": "delivery_authority",
            "work_id": "work",
            "requirements_revision": 0,
            "action_id": "action",
            "changeset_id": "change",
            "candidate": {"scope": "delivery", "plan": "Merge the fixed candidate."},
            "revision": 0,
            "state": "pending",
        }
        result = project(maintenance, value, None)
        self.assertEqual(result["arguments"]["action_id"], "action")
        self.assertEqual(result["arguments"]["changeset_id"], "change")
        self.assertEqual(result["arguments"]["subject"], subject)
        self.assertEqual(result["arguments"]["head"], "a" * 40)
        self.assertEqual(result["arguments"]["method"], "squash")
