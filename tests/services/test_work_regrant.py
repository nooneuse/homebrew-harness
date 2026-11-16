"""Reauthorizing unfinished work retains its original candidate baseline."""

import unittest
from pathlib import Path

from homebrew_harness.execution.source_snapshot import snapshot
from tests.services.delivery import test_flow as fixture


class WorkRegrantTests(unittest.TestCase):
    setUp = fixture.FlowTests.setUp
    run_git = fixture.FlowTests.run_git
    start = fixture.FlowTests.start

    def test_regrant_includes_outputs_written_before_the_interruption(self):
        service = self.start()
        service.pause("all")
        handle = service.work.submit("Complete the scoped change.")
        delivery = service.runtime.maintenance.delivery
        baseline = {"files": snapshot(self.workspace), "head": delivery.git.head(self.workspace)}
        (self.workspace / "src/value.txt").write_text("2\n")
        (self.workspace / "src/extra.txt").write_text("already implemented\n")
        work = service.store.update_work(service.runtime.header, handle.id, baseline=baseline)
        proposal = {"scope": "code", "plan": "Complete the change and run the fixed tests."}
        with service.store.db.write() as conn:
            service.runtime.maintenance.store.grant(conn, work, proposal, "operator:resume")
        restored = service.store.work(handle.id)
        self.assertEqual(restored["baseline"], baseline)
        candidate = delivery.git.prepare(restored, self.workspace, restored["baseline"], self.base / "regranted-candidate")
        self.assertEqual(set(candidate["delta"]), {"src/value.txt", "src/extra.txt"})
        self.assertEqual((Path(candidate["workspace"]) / "src/extra.txt").read_text(), "already implemented\n")

    def test_prior_candidate_starts_a_new_baseline_instead_of_reusing_delivered_input(self):
        service = self.start()
        service.pause("all")
        handle = service.work.submit("Revise the previously delivered feature.")
        work = service.store.update_work(service.runtime.header, handle.id, baseline={"files": {}, "head": "previous"}, changeset_id="changeset_previous")
        proposal = {"scope": "code", "plan": "Implement the accepted new revision."}
        with service.store.db.write() as conn:
            service.runtime.maintenance.store.grant(conn, work, proposal, "operator:new-revision")
        self.assertIsNone(service.store.work(handle.id)["baseline"])


if __name__ == "__main__":
    unittest.main()
