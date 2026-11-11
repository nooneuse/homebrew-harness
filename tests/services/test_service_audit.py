"""Fixed audits, deduplicated findings and reuse of native development/CI delivery."""

import json
import unittest

from tests.services.delivery import test_flow as fixture
from tests.services import test_operations as operations
from tests.runtime.test_interaction import eventually


class AuditTests(unittest.TestCase):
    setUp = fixture.FlowTests.setUp
    run_git = fixture.FlowTests.run_git
    start = fixture.FlowTests.start
    pending_target = fixture.FlowTests.pending_target
    wait_operation = operations.OperationTests.wait_operation

    def test_expired_original_authority_renews_without_restarting_development(self):
        import time

        self.peer.ci_failure = True
        service = self.start(auto_ci_fix=False)
        work = service.work.submit("Fix the scoped value.")
        request = eventually(
            lambda: next(
                (r for r in service.requests() if r.kind == "ci_repair"), None
            ),
            30,
        )
        value = service.store.work(work.id)
        original_run = value["run_id"]
        service.store.update_work(
            service.runtime.header,
            work.id,
            authority={**value["authority"], "expires_at": time.time() - 1},
        )
        service.decide(request, "approve", decision_id="repair")
        authority = eventually(
            lambda: next(
                (r for r in service.requests() if r.kind == "work_authority"), None
            ),
            15,
        )
        self.assertEqual(work.snapshot().details["run_id"], original_run)
        service.decide(authority, "approve", decision_id="renew")
        target = eventually(lambda: self.pending_target(service), 35)
        self.assertEqual(service.store.work(work.id)["ci_fix_attempts"], 1)
        self.assertEqual(len(self.peer.pulls), 1)
        service.decide(target, "approve", decision_id="target")
        eventually(lambda: work.snapshot().state == "delivered", 20)

    def test_ci_delivery_synchronizes_primary_and_preserves_other_files(self):
        self.peer.ci_failure = True
        (self.workspace / "other_work.txt").write_text("Keep unrelated work.")
        service = self.start(auto_ci_fix=True)
        work = service.work.submit("Fix the scoped value.")
        target = eventually(lambda: self.pending_target(service), 45)
        service.decide(target, "approve", decision_id="target")
        eventually(
            lambda: (
                (work.snapshot().details.get("workspace_sync") or {}).get("state")
                == "synchronized"
            ),
            20,
        )
        self.assertEqual((self.workspace / "src/value.txt").read_text(), "2\n")
        self.assertEqual(
            (self.workspace / "other_work.txt").read_text(), "Keep unrelated work."
        )

    def test_ci_delivery_does_not_overwrite_new_primary_changes(self):
        from unittest.mock import patch

        self.peer.ci_failure = True
        service = self.start(auto_ci_fix=True)
        work = service.work.submit("Fix the scoped value.")
        target = eventually(lambda: self.pending_target(service), 45)
        delivery = service.runtime.maintenance.delivery
        with patch.object(delivery, "sync_pending", return_value=True):
            service.decide(target, "approve", decision_id="target")
            eventually(lambda: work.snapshot().state == "delivered", 20)
            (self.workspace / "src/value.txt").write_text("99\n")
        eventually(
            lambda: (
                (work.snapshot().details.get("workspace_sync") or {}).get("state")
                == "conflict"
            ),
            15,
        )
        self.assertEqual((self.workspace / "src/value.txt").read_text(), "99\n")
        self.assertFalse(service.health()["ready"])
        (self.workspace / "src/value.txt").write_text("2\n")
        eventually(
            lambda: (
                (work.snapshot().details.get("workspace_sync") or {}).get("state")
                == "synchronized"
            ),
            15,
        )

    def test_findings_enter_native_work_and_unchanged_audit_skips_model(self):
        service = self.start(operations={"mode": "local", "git_executable": self.git})
        self.peer.audit_findings = [
            {
                "severity": "blocking",
                "path": "src/value.txt",
                "message": "Correct the initial zero value and preserve its fixed tests.",
            }
        ]
        first = self.wait_operation(service, service.audit())
        audit_id = first["result"]["audit_id"]
        eventually(
            lambda: (
                service.record("audits", audit_id)["text"].find('"state": "completed"')
                >= 0
            ),
            15,
        )
        request = eventually(lambda: self.pending_target(service), 30)
        self.assertEqual(len(service.work.list()["items"]), 1)
        self.assertEqual(
            service.records("audits")["items"][0]["work_ids"],
            [service.work.list()["items"][0]["id"]],
        )
        # Repeating the same finding while work remains un-delivered reuses the work.
        second = self.wait_operation(service, service.audit(force=True))
        eventually(
            lambda: (
                service.record("audits", second["result"]["audit_id"])["text"].find(
                    '"state": "completed"'
                )
                >= 0
            ),
            15,
        )
        self.assertEqual(len(service.work.list()["items"]), 1)
        before = len(self.peer.model_requests)
        skipped = self.wait_operation(service, service.audit())
        self.assertTrue(skipped["result"]["skipped"])
        self.assertEqual(len(self.peer.model_requests), before)
        service.decide(request, "approve", decision_id="audit-target")
        eventually(lambda: service.work.list()["items"][0]["state"] == "delivered", 20)

    def test_parked_audit_can_complete_without_resuming_writers(self):
        service = self.start(operations={"mode": "local", "git_executable": self.git})
        self.wait_operation(service, service.quiesce())
        result = self.wait_operation(service, service.audit())
        audit = json.loads(
            service.record("audits", result["result"]["audit_id"], limit=16000)["text"]
        )
        self.assertEqual(audit["state"], "completed")
        self.assertEqual(service.health()["operations_phase"], "parked")

    def test_new_revision_captures_a_new_baseline_after_prior_delivery(self):
        service = self.start()
        work = service.work.submit("Correct the scoped initial value.")
        request = eventually(lambda: self.pending_target(service), 30)
        service.decide(request, "approve", decision_id="first")
        eventually(lambda: work.snapshot().state == "delivered", 20)
        eventually(
            lambda: (
                (work.snapshot().details.get("workspace_sync") or {}).get("state")
                == "synchronized"
            ),
            20,
        )
        self.peer.coding_value = "3\n"
        work.revise("Now supply value 3 while retaining the public file and tests.")
        authority = eventually(
            lambda: next(
                (r for r in service.requests() if r.kind == "work_authority"), None
            ),
            15,
        )
        service.decide(authority, "approve", decision_id="new-version")
        target = eventually(lambda: self.pending_target(service), 30)
        service.decide(target, "approve", decision_id="second")
        eventually(lambda: work.snapshot().state == "delivered", 20)
        self.assertEqual((self.workspace / "src/value.txt").read_text(), "3\n")
