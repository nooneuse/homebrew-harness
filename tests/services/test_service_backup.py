"""Service backup restores facts only at a verified, unchanged park boundary."""

import json
import unittest

from tests.services import test_operations as fixture
from tests.support.clients import reply


class BackupTests(unittest.TestCase):
    setUp = fixture.OperationTests.setUp
    host = fixture.OperationTests.host
    spec = fixture.OperationTests.spec
    wait_operation = fixture.OperationTests.wait_operation

    def start(self, replies=()):
        folder = self.base / "backups"
        folder.mkdir(mode=0o700)
        host, _ = self.host(list(replies))
        service = host.services.start(
            self.spec(operations={"mode": "local", "backup_directory": str(folder)})
        )
        self.addCleanup(lambda: (service.close(), service.wait_closed(10)))
        return service, folder

    def test_backup_verify_and_restore_keep_http_and_current_receipts(self):
        service, folder = self.start([reply("Done")])
        work = service.work.submit("Complete the bounded task")
        work.wait(15)
        self.wait_operation(service, service.quiesce())
        backup = self.wait_operation(service, service.backup(request_id="backup"))
        self.assertGreaterEqual(
            len(
                json.loads((folder / backup["id"] / "manifest.json").read_text())[
                    "roots"
                ]
            ),
            2,
        )
        self.wait_operation(service, service.verify_backup(backup["id"]))
        restored = self.wait_operation(
            service, service.restore_backup(backup["id"], request_id="restore")
        )
        self.assertTrue(restored["result"]["restored"])
        self.assertEqual(
            service.restore_backup(backup["id"], request_id="restore")["id"],
            restored["id"],
        )
        self.assertTrue(service.health()["live"])
        self.assertEqual(work.result().text, "Done")
        self.assertEqual(service.health()["operations_phase"], "parked")

    def test_tampered_backup_is_rejected_without_changing_business(self):
        service, folder = self.start()
        self.wait_operation(service, service.quiesce())
        backup = self.wait_operation(service, service.backup())
        (folder / backup["id"] / "control/database.sqlite3").write_bytes(b"corrupt")
        row = service.restore_backup(backup["id"])
        from tests.runtime.test_interaction import eventually

        failed = eventually(
            lambda: (
                service.operation(row["id"])
                if service.operation(row["id"])["state"] == "blocked"
                else None
            ),
            10,
        )
        self.assertEqual(failed["error"], "ValueError")
        self.assertTrue(service.health()["live"])

    def test_new_work_invalidates_an_older_backup(self):
        service, _ = self.start([reply("New")])
        self.wait_operation(service, service.quiesce())
        backup = self.wait_operation(service, service.backup())
        self.wait_operation(service, service.resume_operations())
        service.work.submit("New accepted work").wait(15)
        self.wait_operation(service, service.quiesce())
        row = service.restore_backup(backup["id"])
        from tests.runtime.test_interaction import eventually

        failed = eventually(
            lambda: (
                service.operation(row["id"])
                if service.operation(row["id"])["state"] == "blocked"
                else None
            ),
            10,
        )
        self.assertIn("New business facts", failed["error_detail"])
