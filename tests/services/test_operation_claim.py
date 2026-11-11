"""Queued audits wait for writers without spending their execution window."""

import time
import unittest
from types import SimpleNamespace

from homebrew_harness.services.operations.runtime import Operations
from tests.services.communication import test_boundaries as fixture


class AuditClaim(unittest.TestCase):
    setUp = fixture.CommunicationBoundaries.setUp
    start = fixture.CommunicationBoundaries.start
    discussion = fixture.CommunicationBoundaries.discussion
    managed = fixture.CommunicationBoundaries.managed

    def test_old_queued_audit_gets_deadline_only_when_writer_finishes(self):
        service, did, c = self.managed()
        runtime = service.runtime
        busy = [True]
        store = SimpleNamespace(
            db=runtime.store.db,
            guard=runtime.store.guard,
            get=runtime.store.get,
            next_work=lambda _: {"state": "in_progress"} if busy[0] else None,
        )
        manager = Operations(
            SimpleNamespace(
                store=store,
                header=runtime.header,
                id=runtime.id,
                spec=runtime.spec,
                maintenance=runtime.maintenance,
            )
        )
        runtime.operations.stop.set()
        self.assertTrue(runtime.operations.finished.wait(5))
        with runtime.store.db.write() as conn:
            meta = runtime.store.db._get(conn, "service_meta", runtime.id)
            meta["operations_phase"] = "active"
            runtime.store.db._put(conn, "service_meta", runtime.id, meta)
            manager.put(
                conn,
                {
                    "id": "op_" + "1" * 32,
                    "kind": "audit",
                    "payload": {"force": False},
                    "state": "queued",
                    "revision": 0,
                    "created_at": time.time() - 10000,
                    "deadline_at": None,
                    "eligible_at": 0,
                },
            )
        # Park the real operations worker; claim this isolated owner's queue directly.
        self.assertIsNone(manager.claim())
        busy[0] = False
        claimed = manager.claim()
        self.assertEqual(claimed["state"], "running")
        self.assertGreater(claimed["deadline_at"], time.time() + 1)
