"""An old candidate cannot replace resuming the current paused repair Run."""

import threading
import unittest

from tests.runtime.test_interaction import eventually
from tests.services.delivery import test_target_refresh as fixture


class RepairResumeTests(unittest.TestCase):
    setUp = fixture.TargetRefreshTests.setUp
    manual = fixture.TargetRefreshTests.manual
    start = fixture.TargetRefreshTests.start
    run_git = fixture.TargetRefreshTests.run_git
    ready = fixture.TargetRefreshTests.ready
    advance_target = fixture.TargetRefreshTests.advance_target
    conflict_request = fixture.TargetRefreshTests.conflict_request

    def test_paused_repair_with_old_candidate_resumes_same_run_and_pr(self):
        service, work, old = self.manual()
        original_run = service.store.work(work.id)["run_id"]
        self.peer.repair_started = threading.Event()
        self.peer.repair_hold = threading.Event()
        self.addCleanup(self.peer.repair_hold.set)
        self.advance_target("src/value.txt", "3\n")
        request = eventually(lambda: self.conflict_request(service), 30)
        service.decide(request, "approve", decision_id="start-current-repair")
        self.assertTrue(self.peer.repair_started.wait(20))
        current = service.store.work(work.id)
        repair_run = current["run_id"]
        self.assertNotEqual(repair_run, original_run)
        child = service.runtime.child_for(current)
        work.pause(request_id="park-current-repair")
        eventually(lambda: child.run_store.get(repair_run)["status"] == "paused"
                   and child.run_store.get(repair_run)["cleanup_complete"], 20)
        self.assertEqual(work.progress()["phase"], "developing")
        self.assertEqual(work.progress()["delivery"]["state"], "repairing")
        self.peer.repair_hold.set()
        work.resume(request_id="continue-current-repair")
        repaired = eventually(lambda: self.ready(service, work), 45)
        self.assertEqual(service.store.work(work.id)["run_id"], repair_run)
        self.assertEqual(service.store.work(work.id)["ci_fix_attempts"], 1)
        self.assertEqual(repaired["change_request"], old["change_request"])
        self.assertEqual(len(self.peer.pulls), 1)
