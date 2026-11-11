"""Irrelevant service heartbeats do not invalidate a sealed session retirement."""

import unittest
from unittest.mock import patch
from tests.storage import test_checkpoint_retirement as fixture
from homebrew_harness.storage import checkpoint_retirement as retirement


class ConcurrentService(unittest.TestCase):
    setUp = fixture.RetirementCases.setUp
    session = fixture.RetirementCases.session
    plan = fixture.RetirementCases.plan

    def test_heartbeat_update_during_archive_is_preserved(self):
        self.session()
        plan = self.plan()
        original = retirement._publish_archive

        def publish(root, data):
            result = original(root, data)
            self.store.put("service_meta", "fixture", {"heartbeat_at": 123})
            return result

        with patch.object(retirement, "_publish_archive", side_effect=publish):
            result = retirement.retire(self.root, plan, now=fixture.NOW)
        self.assertEqual(result["retired"], ["old"])
        self.assertEqual(self.store.get("service_meta", "fixture")["heartbeat_at"], 123)

    def test_new_goal_reference_still_prevents_retirement(self):
        self.session()
        plan = self.plan()
        original = retirement._publish_archive

        def publish(root, data):
            result = original(root, data)
            self.store.put(
                "goals", "new", {"id": "new", "session_id": "old", "status": "active"}
            )
            return result

        with (
            patch.object(retirement, "_publish_archive", side_effect=publish),
            self.assertRaises(ValueError),
        ):
            retirement.retire(self.root, plan, now=fixture.NOW)
        self.assertIsNotNone(self.store.get("checkpoints", "lead:old"))

    def test_empty_plan_does_not_open_a_runtime_replacement_transaction(self):
        self.session()
        plan = retirement.preview(self.root, selected=[], now=fixture.NOW)
        with patch.object(
            type(self.store),
            "transaction",
            side_effect=AssertionError("No retirement writes needed"),
        ):
            self.assertEqual(
                retirement.retire(self.root, plan, now=fixture.NOW)["retired"], []
            )
