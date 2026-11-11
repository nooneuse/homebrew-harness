"""A trusted blocking review must be resolved before target publication is offered."""

import unittest
from unittest.mock import patch
from tests.runtime.test_interaction import eventually
from tests.services.delivery import test_flow as fixture


class ReviewIntegration(unittest.TestCase):
    setUp = fixture.FlowTests.setUp
    start = fixture.FlowTests.start
    run_git = fixture.FlowTests.run_git
    pending_target = fixture.FlowTests.pending_target

    def test_blocking_review_waits_then_approved_review_delivers(self):
        service = self.start(reviewer_ids=[17], required_approvals=1)
        delivery = service.runtime.maintenance.delivery
        actual = delivery.adapter.feedback
        state = {"status": "CHANGES_REQUESTED", "version": "1"}

        def feedback(subject):
            result = actual(subject)
            result["reviews"] = [
                {
                    "id": "77",
                    "actor_id": 17,
                    "head": result["head"],
                    "status": state["status"],
                    "version": state["version"],
                    "body": "Fixture review.",
                }
            ]
            return result

        with patch.object(delivery.adapter, "feedback", side_effect=feedback):
            work = service.work.submit("Fix the scoped value.")

            def waiting():
                identity = work.snapshot().details.get("changeset_id")
                return (
                    identity
                    and delivery.store.get("changesets", identity)["state"]
                    == "review_waiting"
                )

            self.assertTrue(eventually(waiting, 30))
            self.assertIsNone(self.pending_target(service))
            state.update(status="APPROVED", version="2")
            request = eventually(lambda: self.pending_target(service), 15)
            service.decide(request, "approve", decision_id="target")
            eventually(lambda: work.snapshot().state == "delivered", 20)
