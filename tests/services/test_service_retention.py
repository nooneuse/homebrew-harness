"""Retained requests protect old evidence; service GC reuses the storage guards."""

import json
import time
import unittest

from tests.services.delivery import test_flow as fixture
from tests.services import test_operations as operations
from tests.runtime.test_interaction import eventually


class RetentionTests(unittest.TestCase):
    setUp = fixture.FlowTests.setUp
    run_git = fixture.FlowTests.run_git
    start = fixture.FlowTests.start
    pending_target = fixture.FlowTests.pending_target
    wait_operation = operations.OperationTests.wait_operation

    def test_pending_delivery_pins_native_sessions_past_retirement_age(self):
        service = self.start(operations={"mode": "local"})
        work = service.work.submit("Fix the scoped value.")
        eventually(lambda: self.pending_target(service), 30)
        self.wait_operation(service, service.quiesce())
        pins = json.loads(
            (self.workspace / ".harness/service-retention.json").read_text()
        )
        native = service.runtime.child.run_store.get(work.snapshot().details["run_id"])
        self.assertIn(native["session_id"], pins["sessions"])
        from homebrew_harness.storage.checkpoint_retirement import workspace_blockers

        state = service.runtime.child.store.read()
        blockers = workspace_blockers(
            state,
            {"records": {".harness/service-retention.json": pins}},
            native["session_id"],
        )
        self.assertIn("service_retained_execution", blockers)
        plan = self.wait_operation(service, service.retention_preview())
        self.assertEqual(plan["result"]["candidates"], [])

    def test_retired_candidate_keeps_delivery_facts_and_refuses_new_actions(self):
        service = self.start(
            operations={"mode": "local", "candidate_retention_days": 1}
        )
        work = service.work.submit("Fix the scoped value.")
        target = eventually(lambda: self.pending_target(service), 30)
        service.decide(target, "approve", decision_id="target")
        eventually(lambda: work.snapshot().state == "delivered", 20)
        self.wait_operation(service, service.quiesce())
        row = service.runtime.maintenance.store.get(
            "changesets", work.snapshot().details["changeset_id"]
        )
        service.runtime.maintenance.delivery.store.update(
            "changesets", row["id"], created_at=time.time() - 2 * 86400
        )
        plan = self.wait_operation(service, service.retention_preview())
        self.assertEqual(len(plan["result"]["candidates"]), 2)
        result = self.wait_operation(service, service.retention_apply(plan["id"]))
        self.assertTrue(result["result"]["candidate:" + row["id"]]["retired"])
        self.assertEqual(work.snapshot().state, "delivered")
        self.assertTrue(service.delivery(row["id"])["candidate"]["artifacts_retired"])
        self.assertTrue(service.delivery(row["id"])["integration"]["artifacts_retired"])
        from homebrew_harness.contracts import SDKError

        with self.assertRaises(SDKError):
            service.plan_delivery(row["id"], "merge_change_request")
