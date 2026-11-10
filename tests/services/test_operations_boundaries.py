"""Ownership, replay and recovery boundaries of queued service operations."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from homebrew_harness.execution.execution_leases import ExecutionLeases
from homebrew_harness.contracts import SDKError
from tests.services import test_service_backup as fixture
from tests.runtime.test_interaction import eventually


class BoundaryTests(unittest.TestCase):
    setUp = fixture.BackupTests.setUp
    host = fixture.BackupTests.host
    spec = fixture.BackupTests.spec
    start = fixture.BackupTests.start
    wait_operation = fixture.BackupTests.wait_operation

    def blocked(self, service, row):
        return eventually(
            lambda: (
                service.operation(row["id"])
                if service.operation(row["id"])["state"] == "blocked"
                else None
            ),
            15,
        )

    def test_invalid_controls_do_not_create_operation_records(self):
        service, _ = self.start()
        with self.assertRaises(SDKError):
            service.audit(force="yes")
        with self.assertRaises(SDKError):
            service.prepare_upgrade("not-registered")
        with self.assertRaises(SDKError):
            service.retry_operation("../operation")
        self.assertFalse(service.operations()["items"])

    def test_live_foreign_writer_blocks_backup_and_retry_uses_same_id(self):
        service, folder = self.start()
        self.wait_operation(service, service.quiesce())
        foreign = ExecutionLeases(self.workspace)
        try:
            foreign.acquire("session")
            row = service.backup(request_id="backup")
            self.blocked(service, row)
            self.assertFalse((folder / row["id"]).exists())
        finally:
            foreign.close()
        service.retry_operation(row["id"], request_id="retry")
        self.assertEqual(self.wait_operation(service, row)["id"], row["id"])

    def test_restore_failure_retains_ownership_and_resumes_without_losing_receipt(self):
        from homebrew_harness.services.operations import backup as module

        service, _ = self.start()
        self.wait_operation(service, service.quiesce())
        stored = self.wait_operation(service, service.backup())
        original = module.write
        failed = []

        def write(root, path, raw, *args, **kw):
            if (
                "/restore/" in path
                and b'"completed": ["control"]' in raw
                and not failed
            ):
                failed.append(True)
                raise OSError("Fixture interrupted restore journal publication.")
            return original(root, path, raw, *args, **kw)

        with patch.object(module, "write", side_effect=write):
            row = service.restore_backup(stored["id"], request_id="restore")
            self.assertEqual(self.blocked(service, row)["error"], "OSError")
        self.assertTrue(service.health()["live"])
        service.retry_operation(row["id"], request_id="restore-retry")
        self.assertTrue(self.wait_operation(service, row)["result"]["restored"])
        self.assertEqual(
            service.restore_backup(stored["id"], request_id="restore")["id"], row["id"]
        )

    def test_health_incident_is_deduplicated_and_resolved_without_model_calls(self):
        from homebrew_harness.services.operations import health

        service, _ = self.start()
        manager = service.runtime.operations
        with patch.object(
            health.shutil, "disk_usage", return_value=SimpleNamespace(free=0)
        ):
            manager.observe()
            manager.observe()
            self.assertFalse(service.health()["ready"])
        rows = service.store.runtime.items("service_incidents")
        self.assertEqual(
            sum(r["issue"]["code"] == "storage_space_low" for r in rows.values()), 1
        )
        manager.observe()
        self.assertEqual(
            next(iter(service.store.runtime.items("service_incidents").values()))[
                "state"
            ],
            "resolved",
        )
