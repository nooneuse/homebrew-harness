"""The model selects source and one target; real Git owns replay mechanics."""

import unittest
from pathlib import Path
from tests.services import test_work_sources as source_fixture


class WorkIntegrationTests(unittest.TestCase):
    setUp = source_fixture.WorkSourcesTests.setUp
    run_git = source_fixture.WorkSourcesTests.run_git
    start = source_fixture.WorkSourcesTests.start
    ready = source_fixture.WorkSourcesTests.ready

    def test_rebase_uses_selected_candidate_and_one_observed_target(self):
        service, work, candidate = self.ready()
        (self.workspace / "src/independent.txt").write_text("host addition\n")
        self.run_git("add", "src/independent.txt", cwd=self.workspace)
        self.run_git("commit", "-qm", "Independent target change", cwd=self.workspace)
        self.run_git("push", str(self.remote), "main", cwd=self.workspace)
        target = work.observe_target(
            execution={"mode": "one_shot"}, request_id="one-target"
        )
        result = work.continue_from(
            {
                "mode": "fork",
                "source_ref": candidate,
                "reason": "Rebase finished feature once.",
                "integration": {"method": "rebase", "target_ref": target["source_ref"]},
                "start": False,
            },
            execution={"mode": "one_shot"},
            request_id="one-rebase",
        )
        self.assertEqual(result["conflicts"], [])
        current = service.store.work(work.id)
        root = Path(current["run_workspace"])
        self.assertEqual((root / "src/value.txt").read_text(), "1\n")
        self.assertEqual((root / "src/independent.txt").read_text(), "host addition\n")
        self.assertNotEqual(
            self.run_git("rev-parse", "HEAD", cwd=root).decode().strip(),
            target["commit"],
        )
        self.assertTrue(current["pause_requested"])
        self.assertEqual(work.read_source(candidate, "src/value.txt")["text"], "1\n")

    def test_conflicted_rebase_can_be_resolved_and_continued(self):
        service, work, candidate = self.ready()
        (self.workspace / "src/value.txt").write_text("3\n")
        self.run_git("add", "src/value.txt", cwd=self.workspace)
        self.run_git("commit", "-qm", "Changed target value", cwd=self.workspace)
        self.run_git("push", str(self.remote), "main", cwd=self.workspace)
        target = work.observe_target(
            execution={"mode": "one_shot"}, request_id="target-conflict"
        )
        result = work.continue_from(
            {
                "mode": "fork",
                "source_ref": candidate,
                "reason": "Resolve this fixed integration.",
                "integration": {"method": "rebase", "target_ref": target["source_ref"]},
                "start": False,
            },
            execution={"mode": "one_shot"},
            request_id="conflict-rebase",
        )
        self.assertEqual(result["conflicts"], ["src/value.txt"])
        current = service.store.work(work.id)
        root = Path(current["run_workspace"])
        (root / "src/value.txt").write_text("4\n")
        final = work.control_integration(
            "continue", execution={"mode": "one_shot"}, request_id="continue-resolved"
        )
        self.assertEqual(final["state"], "settled")
        self.assertEqual(final["conflicts"], [])
        self.assertEqual((root / "src/value.txt").read_text(), "4\n")

    def test_interruption_after_real_git_apply_reuses_prepared_branch(self):
        from unittest.mock import patch
        from homebrew_harness.services.delivery.git import GitBackend

        service, work, candidate = self.ready()
        (self.workspace / "src/independent.txt").write_text("target addition\n")
        self.run_git("add", "src/independent.txt", cwd=self.workspace)
        self.run_git("commit", "-qm", "Target advanced", cwd=self.workspace)
        self.run_git("push", str(self.remote), "main", cwd=self.workspace)
        target = work.observe_target(
            execution={"mode": "one_shot"}, request_id="interrupted-target"
        )
        action = {
            "mode": "fork",
            "source_ref": candidate,
            "reason": "Apply this target once.",
            "start": False,
            "integration": {"method": "rebase", "target_ref": target["source_ref"]},
        }
        revision = work.snapshot().revision
        original = GitBackend.run
        calls = []

        def interrupted(git, args, **kw):
            result = original(git, args, **kw)
            if "rebase" in args and "--continue" not in args:
                calls.append(args)
                raise RuntimeError("Interruption after actual Git rebase.")
            return result

        with patch.object(GitBackend, "run", interrupted), self.assertRaisesRegex(
            Exception, "actual Git rebase"
        ):
            work.continue_from(
                action,
                revision=revision,
                execution={"mode": "one_shot"},
                request_id="apply-interrupted",
            )
        self.assertEqual(len(calls), 1)
        # Ordinary failed executions require a new explicit request. The prepared
        # local step is replayed under its stable operation key after owner recovery.
        manager = service.runtime.work_executions.manager(work.id)
        identity = service.runtime.work_executions.identity(
            work.id, "apply-interrupted"
        )
        manager.rows[identity].update(state="interrupted")
        manager._save(manager.rows[identity])
        with patch.object(GitBackend, "run", interrupted):
            result = work.continue_from(
                action,
                revision=revision,
                execution={"mode": "one_shot"},
                request_id="apply-interrupted",
            )
        self.assertEqual(len(calls), 1)
        current = service.store.work(work.id)
        workspace = Path(current["run_workspace"])
        self.assertEqual((workspace / "src/value.txt").read_text(), "1\n")
        self.assertEqual(
            (workspace / "src/independent.txt").read_text(), "target addition\n"
        )
        self.assertEqual(result["conflicts"], [])
        baseline = current["baseline"]
        frozen = service.runtime.maintenance.delivery.git.prepare(
            current,
            workspace,
            baseline,
            self.control / ".harness" / "selected-final-check",
        )
        self.assertEqual(frozen["target_base"], target["commit"])
        self.assertIn("src/value.txt", frozen["delta"])
        self.assertNotIn("src/independent.txt", frozen["delta"])

    def test_git_failure_exposes_real_stderr_and_configured_output_limit(self):
        from homebrew_harness.runtime.limits import limits_scope
        from homebrew_harness.runtime.errors import error_info

        service, work, candidate = self.ready()
        git = service.runtime.maintenance.delivery.git
        with self.assertRaises(ValueError) as failure:
            git.run(["rev-parse", "--verify", "missing-ref"], cwd=self.workspace)
        self.assertEqual(error_info(failure.exception)["code"], "git_command_failed")
        self.assertIn("fatal:", str(failure.exception))
        with limits_scope({"git_output_bytes": 2}), self.assertRaises(
            ValueError
        ) as overflow:
            git.run(["rev-parse", "HEAD"], cwd=self.workspace)
        self.assertEqual(error_info(overflow.exception)["details"]["limit_value"], 2)

    def test_abort_restores_original_feature_and_its_delivery_base(self):
        service, work, candidate = self.ready()
        old = next(v for v in work.sources()["sources"] if v["source_ref"] == candidate)
        (self.workspace / "src/value.txt").write_text("3\n")
        (self.workspace / "README.md").write_text("Upstream-only documentation.\n")
        self.run_git("add", "src/value.txt", "README.md", cwd=self.workspace)
        self.run_git("commit", "-qm", "Conflicting target", cwd=self.workspace)
        self.run_git("push", str(self.remote), "main", cwd=self.workspace)
        target = work.observe_target(
            execution={"mode": "one_shot"}, request_id="abort-target"
        )
        work.continue_from(
            {
                "mode": "fork",
                "source_ref": candidate,
                "reason": "Inspect this integration.",
                "start": False,
                "integration": {"method": "rebase", "target_ref": target["source_ref"]},
            },
            execution={"mode": "one_shot"},
            request_id="abort-prepare",
        )
        result = work.control_integration(
            "abort", execution={"mode": "one_shot"}, request_id="abort-once"
        )
        current = service.store.work(work.id)
        workspace = Path(current["run_workspace"])
        self.assertEqual(result["state"], "aborted")
        self.assertEqual((workspace / "src/value.txt").read_text(), "1\n")
        self.assertEqual(current["continuation"]["target_commit"], old["target_base"])
        frozen = service.runtime.maintenance.delivery.git.prepare(
            current,
            workspace,
            current["baseline"],
            self.control / ".harness" / "aborted-final-check",
        )
        self.assertEqual(frozen["target_base"], old["target_base"])
