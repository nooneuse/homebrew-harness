"""Trusted effective PR decisions are separate from generic platform comments."""

import unittest
from homebrew_harness.services.delivery.review_gate import check


class ReviewGate(unittest.TestCase):
    def test_trusted_request_changes_blocks_current_head_until_new_approval(self):
        policy = {"reviewer_ids": [17], "required_approvals": 1}
        feedback = {
            "head": "h2",
            "reviews": [
                {
                    "id": "1",
                    "actor_id": 17,
                    "head": "h2",
                    "status": "CHANGES_REQUESTED",
                    "version": "1",
                }
            ],
        }
        self.assertFalse(check(feedback, policy)["ready"])
        feedback["reviews"].append(
            {
                "id": "2",
                "actor_id": 17,
                "head": "h2",
                "status": "COMMENTED",
                "version": "2",
            }
        )
        self.assertFalse(check(feedback, policy)["ready"])
        feedback["reviews"].append(
            {
                "id": "3",
                "actor_id": 17,
                "head": "h2",
                "status": "APPROVED",
                "version": "3",
            }
        )
        self.assertTrue(check(feedback, policy)["ready"])

    def test_untrusted_votes_and_old_head_do_not_gain_gate_authority(self):
        feedback = {
            "head": "h2",
            "reviews": [
                {
                    "id": "1",
                    "actor_id": 99,
                    "head": "h2",
                    "status": "CHANGES_REQUESTED",
                },
                {"id": "2", "actor_id": 17, "head": "h1", "status": "APPROVED"},
            ],
        }
        self.assertTrue(
            check(feedback, {"reviewer_ids": [17], "required_approvals": 0})["ready"]
        )
        self.assertFalse(
            check(feedback, {"reviewer_ids": [17], "required_approvals": 1})["ready"]
        )
