"""Owned source versions and explicit continuations preserve unfinished code."""

import json
import unittest
from pathlib import Path
from tests.services.delivery import test_flow as fixture
from tests.runtime.test_interaction import eventually


class WorkSourcesTests(unittest.TestCase):
    setUp = fixture.FlowTests.setUp
    run_git = fixture.FlowTests.run_git
    start = fixture.FlowTests.start

    def ready(self):
        service = self.start(merge={"mode": "manual", "poll_seconds": 0.1})
        work = service.work.submit("Maintain the scoped fixture value.")
        eventually(
            lambda: next(
                (
                    r
                    for r in service.runtime.maintenance.delivery.rows("changesets")
                    if r.get("change_request")
                ),
                None,
            ),
            90,
        )
        service.pause("all")
        work.pause()
        candidate = next(
            row for row in work.sources()["sources"] if row["role"] == "candidate"
        )
        return service, work, candidate["source_ref"]

    def test_current_draft_and_frozen_candidate_can_be_compared_without_git_shell(self):
        service, work, candidate = self.ready()
        (self.workspace / "src/value.txt").write_text("2\n")
        draft = "draft:" + work.id
        compared = work.compare_sources(candidate, draft, limit=1)
        self.assertEqual(compared["total_files"], 1)
        self.assertEqual(compared["files"][0]["path"], "src/value.txt")
        frozen = work.read_source(candidate, "src/value.txt")
        current = work.read_source(draft, "src/value.txt")
        self.assertEqual(frozen["text"], "1\n")
        self.assertEqual(current["text"], "2\n")
        (self.workspace / "src/value.txt").write_text("3\n")
        with self.assertRaises(Exception):
            work.read_source(
                draft, "src/value.txt", offset=1, expected_version=current["version"]
            )
        with self.assertRaises(Exception):
            work.compare_sources(candidate, draft, expected_version=compared["version"])
        with self.assertRaises(Exception):
            work.read_source(candidate, "../model.env")

    def test_new_run_preparation_retains_current_draft_and_original_candidate(self):
        service, work, candidate = self.ready()
        before = service.store.work(work.id)
        (self.workspace / "src/value.txt").write_text("2\n")
        action = {
            "mode": "fork",
            "source_ref": "draft:" + work.id,
            "reason": "Continue retained partial implementation.",
            "start": False,
        }
        result = work.continue_from(
            action,
            revision=before["revision"],
            execution={"mode": "one_shot"},
            request_id="keep-draft",
        )
        after = service.store.work(work.id)
        self.assertEqual(
            after["requirements_revision"], before["requirements_revision"]
        )
        self.assertIn(before["run_id"], after["run_history"])
        self.assertTrue(after["pause_requested"])
        self.assertIsNone(after["run_id"])
        self.assertFalse(result["execution_resumed"])
        self.assertEqual(
            (Path(after["run_workspace"]) / "src/value.txt").read_text(), "2\n"
        )
        self.assertEqual(work.read_source(candidate, "src/value.txt")["text"], "1\n")
        self.assertEqual(
            work.read_source(result["preserved_ref"], "src/value.txt")["text"], "2\n"
        )
        self.assertEqual(
            work.continue_from(
                action,
                revision=before["revision"],
                execution={"mode": "one_shot"},
                request_id="keep-draft",
            ),
            result,
        )
        manager = service.runtime.work_executions.manager(work.id)
        execution_id = service.runtime.work_executions.identity(work.id, "keep-draft")
        interrupted = manager.rows[execution_id]
        interrupted.update(
            state="interrupted",
            result={"error": "Owner ended before result delivery."},
            cleanup={"complete": False},
        )
        manager._save(interrupted)
        replay = work.continue_from(
            action,
            revision=before["revision"],
            execution={"mode": "one_shot"},
            request_id="keep-draft",
        )
        self.assertEqual(replay, result)
        self.assertTrue(manager.get(execution_id)["recovery_history"])

    def test_candidate_source_cannot_cross_to_another_work(self):
        service, work, candidate = self.ready()
        other = service.work.submit("An independent requirement.")
        other.pause()
        with self.assertRaises(Exception):
            other.read_source(candidate, "src/value.txt")

    def test_execute_requested_plan_applies_to_reviewed_paused_work(self):
        from unittest.mock import patch
        from homebrew_harness.services.agent import ServiceAgent

        service, work, candidate = self.ready()
        before = service.store.work(work.id)
        original = ServiceAgent.propose

        def propose(agent, proposal, **kw):
            if agent.job["kind"] == "recovery_plan":
                proposal = json.dumps(
                    {
                        "text": "Continue the original feature with its evidence.",
                        "action": {
                            "mode": "fork",
                            "source_ref": candidate,
                            "reason": "Preserve the fixed feature.",
                            "start": False,
                        },
                    }
                )
            return original(agent, proposal, **kw)

        with patch.object(ServiceAgent, "propose", propose):
            accepted = work.plan_recovery(
                "Apply a selected branch without starting development.",
                execute=True,
                request_id="apply-model-plan",
            )
            service.resume()
            current = eventually(
                lambda: (
                    service.store.work(work.id)
                    if service.store.work(work.id).get("continuation")
                    else None
                ),
                25,
            )
            service.pause("all")
        self.assertEqual(service.job(accepted["job_id"])["state"], "completed")
        self.assertNotEqual(
            current["run_workspace"], before.get("run_workspace", str(self.workspace))
        )
        self.assertTrue(current["pause_requested"])
        self.assertIsNone(current["run_id"])
        self.assertEqual(
            current["requirements_revision"], before["requirements_revision"]
        )
        self.assertEqual(
            work.read_source("draft:" + work.id, "src/value.txt")["text"], "1\n"
        )

    def test_diff_rejects_file_changes_after_directory_capture(self):
        from unittest.mock import patch
        from homebrew_harness.security.safe_files import SafeFile

        service, work, candidate = self.ready()
        (self.workspace / "src/value.txt").write_text("2\n")
        original = SafeFile.snapshot

        def changed(reader, *args, **kw):
            if reader.root == self.workspace and reader.parts == ("src", "value.txt"):
                (self.workspace / "src/value.txt").write_text("3\n")
            return original(reader, *args, **kw)

        with patch.object(SafeFile, "snapshot", changed), self.assertRaisesRegex(
            Exception, "changed during comparison"
        ):
            work.compare_sources(candidate, "draft:" + work.id, path="src/value.txt")
        with self.assertRaises(Exception):
            work.compare_sources(candidate, "draft:" + work.id, path="../missing-file")
