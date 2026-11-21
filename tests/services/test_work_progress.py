"""Normal completion adoption respects current requirements and scope."""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from homebrew_harness.services.work_progress import import_run, accept, route


class CompletionAdoption(unittest.TestCase):
    def setUp(self):
        self.work = {
            "id": "work_" + "1" * 32,
            "run_id": "run_" + "2" * 32,
            "requirements_revision": 3,
            "run_spec": {"write_paths": ["src"]},
            "authority": {"requirements_revision": 3},
        }
        self.native = {
            "id": self.work["run_id"],
            "session_id": "session",
            "status": "completed",
            "cleanup_complete": True,
            "finalized_at": 50,
            "spec": {"write_paths": ["src"], "snapshot_limits": {}},
            "last_outcome": {"text": "Completed source change."},
        }
        child = SimpleNamespace(
            workdir=Path("/fixture"), store=SimpleNamespace(items=lambda *a, **k: {})
        )
        self.runtime = SimpleNamespace(
            id="svc_" + "3" * 32,
            child_for=lambda work: child,
            store=SimpleNamespace(work=lambda *a: self.work),
            maintenance=SimpleNamespace(
                store=SimpleNamespace(db=SimpleNamespace(get=lambda *a: True))
            ),
        )

    def test_current_normal_completion_supersedes_saved_partial_progress(self):
        self.native["last_outcome"]["text"] = "completed result " * 1000
        with patch(
            "homebrew_harness.services.work_progress.current",
            return_value={"complete": False},
        ), patch(
            "homebrew_harness.services.work_progress.freeze",
            return_value={"snapshot": True},
        ) as freeze, patch(
            "homebrew_harness.services.work_progress.accept"
        ) as accept:
            import_run(self.runtime, self.work, self.native)
        freeze.assert_called_once()
        progress = accept.call_args.args[2]
        self.assertTrue(progress["complete"])
        self.assertEqual(progress["requirements_revision"], 3)
        self.assertEqual(len(progress["summary"]), 10000)

    def test_stale_requirements_old_run_or_scope_never_gain_current_completion(self):
        for changes in (
            {"review_required": True},
            {"authority": {"requirements_revision": 2}},
            {"run_id": "other"},
            {"run_spec": {"write_paths": ["docs"]}},
        ):
            with self.subTest(changes=changes), patch.object(
                self.runtime.store, "work", return_value=self.work | changes
            ), patch(
                "homebrew_harness.services.work_progress.current", return_value=None
            ), patch(
                "homebrew_harness.services.work_progress.freeze"
            ) as freeze, patch(
                "homebrew_harness.services.work_progress.accept"
            ) as accept:
                import_run(self.runtime, self.work | changes, self.native)
                freeze.assert_not_called()
                accept.assert_not_called()

    def test_progress_from_a_former_write_scope_remains_history(self):
        progress = {
            "requirements_revision": 3,
            "work_id": self.work["id"],
            "service_id": self.runtime.id,
            "write_paths": ["docs"],
        }
        self.assertIsNone(accept(self.runtime, self.work, progress))

    def test_repair_handoff_advances_past_the_prior_runs_candidate(self):
        self.work.update(
            progress_ref="new_handoff", changeset_id="prior_candidate", revision=0
        )
        record = {
            "id": "new_handoff",
            "run_id": self.work["run_id"],
            "complete": True,
            "requirements_revision": 3,
        }
        candidate = {
            "candidate": {"commit": "old"},
            "requirements_revision": 3,
            "policy": "policy",
            "handoff_ref": "old_handoff",
            "run_id": "old_run",
        }
        child = SimpleNamespace(
            run_store=SimpleNamespace(
                get=Mock(return_value={"cleanup_complete": True, "finalized_at": 50})
            )
        )
        self.runtime.child_for = Mock(return_value=child)
        delivery_store = SimpleNamespace(policy="policy", create=Mock(),
            policy_matches=lambda row: row["policy"] == "policy")
        self.runtime.maintenance.delivery = SimpleNamespace(store=delivery_store)
        self.runtime.maintenance.store.get = Mock(return_value=candidate)
        self.runtime.store.guard = Mock()
        self.runtime.header = {}
        self.runtime.store.db = SimpleNamespace(
            write=MagicMock(), _get=lambda *a: self.work.copy(), _put=Mock()
        )
        with patch(
            "homebrew_harness.services.work_progress.current", return_value=record
        ):
            self.assertTrue(route(self.runtime, self.work))
        delivery_store.create.assert_called_once()
        self.assertEqual(delivery_store.create.call_args.args[1]["state"], "review")
