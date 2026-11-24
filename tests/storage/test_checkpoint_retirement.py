from tests.support.paths import PROJECT_ROOT
from homebrew_harness.runtime.capabilities import Capabilities
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from homebrew_harness.storage.checkpoint_retirement import (
    preview,
    retire,
    unretire,
    purge_preview,
    purge,
    pin,
    reconcile,
)
from homebrew_harness.storage.durable_runtime import DurableStore, RecoveryRequired
from homebrew_harness.execution.execution_leases import ExecutionLeases
from homebrew_harness.runtime.session import HarnessSession
from homebrew_harness.context.compaction import ArchiveStore
from homebrew_harness.storage.artifact_inventory import inspect_artifacts
from homebrew_harness.storage.history_gc import apply as gc_apply
from tests.support.clients import fake_client, reply

NOW = 2_000_000_000
DAY = 86400


class RetirementCases:
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = DurableStore(self.root)

    def session(self, sid="old", days=20, **fields):
        self.store.start_session(sid, "fixture-host")
        snapshot = {
            "session_id": sid,
            "phase": "closed",
            "messages": [{"role": "user", "content": "Keep original requirements"}],
            "usage": {"calls": 0, "pending_requests": [], "inflight": 0},
            **fields,
        }
        self.store.checkpoint("lead:" + sid, snapshot)
        self.store.finish_session(sid, "fixture-host", complete=True)
        self.store.update_fields(
            "session_meta",
            sid,
            {"closed_at": NOW - days * DAY, "last_activity_at": NOW - days * DAY},
        )
        return snapshot

    def plan(self, sid="old"):
        return preview(self.root, selected=[sid], now=NOW)

    def do_retire(self, sid="old"):
        plan = self.plan(sid)
        self.assertEqual(len(plan["candidates"]), 1, plan)
        return retire(self.root, plan, now=NOW)

    def child(self, code):
        return subprocess.run(
            [sys.executable, "-c", code, str(self.root), str(NOW + 8 * DAY)],
            capture_output=True,
            timeout=15,
            cwd=PROJECT_ROOT,
        )

    def test_fifteen_day_boundary_and_recent_ten_default(self):
        for index in range(12):
            self.session("item" + str(index), days=30 - index)
        self.store.update_fields("session_meta", "item0", {"closed_at": NOW - 15 * DAY})
        p = preview(self.root, now=NOW)
        self.assertEqual([c["session_id"] for c in p["candidates"]], ["item1"])
        self.store.update_fields(
            "session_meta", "item0", {"closed_at": NOW - 15 * DAY - 1}
        )
        self.assertEqual(len(preview(self.root, now=NOW)["candidates"]), 2)

    def test_manual_selection_bypasses_age_and_count_but_never_latest(self):
        self.session(days=1)
        self.assertFalse(preview(self.root, now=NOW)["candidates"])
        self.assertTrue(self.plan()["candidates"])
        self.store.set_meta("latest_session", "old")
        self.assertIn("latest_resume_target", self.plan()["sessions"][0]["reasons"])

    def test_preview_does_not_change_logical_state(self):
        self.session()
        before = self.store.read()
        self.plan()
        purge_preview(self.root, now=NOW)
        self.assertEqual(before, self.store.read())

    def test_mcp_resources_block_pending_retirement_and_archive_closed_records(self):
        self.session()
        row = {
            "id": "mcp_fixture",
            "checkpoint": "lead:old",
            "session_id": "old",
            "phase": "started",
            "revision": 0,
        }
        self.store.put("mcp_resources", row["id"], row)
        self.assertIn(
            "workspace_mcp_cleanup_pending", self.plan()["sessions"][0]["reasons"]
        )
        self.store.update_fields("mcp_resources", row["id"], {"phase": "closed"})
        self.do_retire()
        self.assertEqual(self.store.items("mcp_resources"), {})
        unretire(self.root, "old", now=NOW + DAY)
        self.assertEqual(self.store.get("mcp_resources", row["id"])["phase"], "closed")

    def test_active_lease_blocks_preview_and_saved_plan_execution(self):
        self.session()
        plan = self.plan()
        leases = ExecutionLeases(self.root)
        leases.acquire("session")
        self.addCleanup(leases.close)
        self.assertIn("active_execution_lease", self.plan()["sessions"][0]["reasons"])
        with self.assertRaises(ValueError):
            retire(self.root, plan, now=NOW)
        self.assertIsNotNone(self.store.get("checkpoints", "lead:old"))

    def test_pin_blocks_retire_and_purge(self):
        self.session()
        plan = self.plan()
        pin(self.root, "old")
        with self.assertRaises(ValueError):
            retire(self.root, plan, now=NOW)
        pin(self.root, "old", False)
        self.do_retire()
        pin(self.root, "old")
        self.assertIn(
            "session_pinned",
            purge_preview(self.root, now=NOW + 8 * DAY)["sessions"][0]["reasons"],
        )

    def test_unknown_operation_partial_child_and_unsettled_mailbox_protect(self):
        self.session()
        self.store.put(
            "operations", "op", {"checkpoint": "lead:old", "state": "unknown"}
        )
        self.assertIn("uncertain_operation", self.plan()["sessions"][0]["reasons"])
        self.store.remove("operations", "op")
        self.store.checkpoint(
            "sub:old:child", {"session_id": "old", "phase": "partial", "messages": []}
        )
        self.store.finish_session("old", "fixture-host", complete=True)
        self.assertIn("unfinished_checkpoint", self.plan()["sessions"][0]["reasons"])
        self.store.remove("checkpoints", "sub:old:child")
        self.store.mailbox(
            "send", "lead", records=[{"id": "pending", "content": "event"}]
        )
        self.assertIn(
            "workspace_unacknowledged_mailbox", self.plan()["sessions"][0]["reasons"]
        )

    def test_pending_usage_blocks_but_optional_ledger_does_not_gate_retirement(self):
        self.session(
            usage={
                "calls": 1,
                "inflight": 1,
                "pending_requests": [{"request_id": "x", "sent": True}],
            }
        )
        self.assertIn("unsettled_model_request", self.plan()["sessions"][0]["reasons"])
        directory = self.root / ".harness/usage"
        directory.mkdir()
        ledger = directory / ("a" * 32 + ".jsonl")
        ledger.write_text(
            json.dumps({"event": "request_started", "request_id": "late"}) + "\n"
        )
        self.session(
            usage={
                "calls": 1,
                "inflight": 0,
                "pending_requests": [],
                "ledger": ledger.relative_to(self.root).as_posix(),
            }
        )
        self.assertNotIn(
            "unsettled_model_ledger", self.plan()["sessions"][0]["reasons"]
        )
        self.assertTrue(self.plan()["sessions"][0]["eligible"])

    def test_unfinished_external_work_and_missing_dependency_are_protected(self):
        self.session(goal_id="goal_" + "a" * 32)
        self.assertIn(
            "missing_linked_dependency", self.plan()["sessions"][0]["reasons"]
        )
        self.store.put("goals", "goal_" + "a" * 32, {"status": "suspended"})
        self.assertIn(
            "workspace_unfinished_goal", self.plan()["sessions"][0]["reasons"]
        )

    def test_group_retirement_and_undo_restore_exact_recovery_payload(self):
        original = self.session()
        child = {
            "session_id": "old",
            "phase": "executed",
            "parent_checkpoint": "lead:old",
            "messages": [],
        }
        self.store.checkpoint("sub:old:child", child)
        member = {"session_id": "old", "phase": "executed", "messages": []}
        self.store.checkpoint("member:old:worker", member)
        team = {
            "revision": 1,
            "members": {
                "worker": {
                    "revision": 1,
                    "status": "stopped",
                    "gate": "approved",
                    "pending_context": [],
                }
            },
            "protocols": [],
        }
        self.store.team_checkpoint("old", team)
        self.store.finish_session("old", "fixture-host", complete=True)
        self.do_retire()
        self.assertEqual(self.store.items("checkpoints"), {})
        self.assertIsNone(self.store.get("team", "old"))
        unretire(self.root, "old", now=NOW + 1)
        self.assertEqual(self.store.get("checkpoints", "lead:old"), original)
        self.assertEqual(self.store.get("checkpoints", "sub:old:child"), child)
        self.assertEqual(self.store.get("checkpoints", "member:old:worker"), member)
        self.assertEqual(self.store.get("team", "old"), team)

    def test_sdk_results_and_decisions_retire_and_restore_with_their_session(self):
        self.session()
        records = {
            section: {"checkpoint": "lead:old", "id": section, "state": "completed"}
            for section in ("api_sessions", "api_turns", "api_decisions")
        }
        for section, row in records.items():
            self.store.put(section, section, row)
        self.do_retire()
        for section in records:
            self.assertIsNone(self.store.get(section, section))
        unretire(self.root, "old", now=NOW + 1)
        for section, row in records.items():
            self.assertEqual(self.store.get(section, section), row)

    def test_retired_and_purged_sessions_reject_resume_and_writes(self):
        self.session()
        self.do_retire()
        for operation in (
            lambda: self.store.start_session("old", "new"),
            lambda: self.store.checkpoint(
                "lead:old", {"session_id": "old", "phase": "idle"}
            ),
            lambda: self.store.team_checkpoint("old", {}),
        ):
            with self.assertRaises(RecoveryRequired):
                operation()
        client, _ = fake_client([])
        runtime = HarnessSession(
            client,
            "fixture",
            self.root,
            resume_session="old",
            shutdown_timeout=0.2,
            capabilities=Capabilities.full(memory_mode="off"),
        )
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(RecoveryRequired):
                runtime.run_user("/usage")
            runtime.close()
            runtime.wait_closed(2)
        client.chat.completions.create.assert_not_called()
        purge(self.root, purge_preview(self.root, now=NOW + 8 * DAY), now=NOW + 8 * DAY)
        with self.assertRaises(ValueError):
            unretire(self.root, "old", now=NOW + 8 * DAY)
        with self.assertRaises(RecoveryRequired):
            self.store.start_session("old", "new")

    def test_grace_window_and_shared_gc_roots(self):
        archive = ArchiveStore(self.root, "saved")
        path = archive.save("tool-results", "historical result")
        self.store.update_fields(
            "artifacts", path, {"created_at": time.time() - 40 * DAY}
        )
        self.session(archives=archive.checkpoint())
        self.do_retire()
        self.assertEqual(inspect_artifacts(self.root)["candidates"], [])
        self.assertFalse(purge_preview(self.root, now=NOW + 7 * DAY - 1)["candidates"])
        with self.assertRaises(ValueError):
            unretire(self.root, "old", now=NOW + 7 * DAY)
        purge(self.root, purge_preview(self.root, now=NOW + 7 * DAY), now=NOW + 7 * DAY)
        plan = inspect_artifacts(self.root)
        self.assertEqual(len(plan["candidates"]), 1)
        gc_apply(self.root, plan)
        self.assertFalse((self.root / path).exists())

    def test_other_checkpoint_and_artifact_pin_keep_shared_output_after_purge(self):
        archive = ArchiveStore(self.root, "saved")
        path = archive.save("tool-results", "shared")
        self.store.update_fields(
            "artifacts", path, {"created_at": time.time() - 40 * DAY}
        )
        self.session(archives=archive.checkpoint())
        self.do_retire()
        self.store.put("checkpoints", "independent", {"messages": [{"content": path}]})
        purge(self.root, purge_preview(self.root, now=NOW + 8 * DAY), now=NOW + 8 * DAY)
        self.assertEqual(inspect_artifacts(self.root)["candidates"], [])

    def test_cold_archive_corruption_and_symlink_fail_without_state_change(self):
        self.session()
        self.do_retire()
        before = self.store.read()
        path = self.root / self.store.get("session_meta", "old")["cold_archive"]["path"]
        raw = path.read_bytes()
        path.write_bytes(b"corrupt")
        with self.assertRaises(RecoveryRequired):
            unretire(self.root, "old", now=NOW + 1)
        self.assertEqual(before, self.store.read())
        path.unlink()
        outside = self.root / "outside"
        outside.write_bytes(raw)
        path.symlink_to(outside)
        with self.assertRaises((OSError, ValueError)):
            unretire(self.root, "old", now=NOW + 1)
        self.assertEqual(outside.read_bytes(), raw)

    def test_transaction_failure_leaves_original_and_reconciles_prepared_copy(self):
        original = self.session()
        plan = self.plan()
        with patch(
            "homebrew_harness.storage.checkpoint_retirement.DurableStore.transaction",
            side_effect=RuntimeError("injected"),
        ):
            with self.assertRaises(RuntimeError):
                retire(self.root, plan, now=NOW)
        self.assertEqual(self.store.get("checkpoints", "lead:old"), original)
        result = reconcile(self.root)
        self.assertEqual(len(result["removed_prepared_orphans"]), 1)

    def test_new_pin_during_archive_preparation_invalidates_plan(self):
        self.session()
        plan = self.plan()
        import homebrew_harness.storage.checkpoint_retirement as module

        original = module._publish_archive

        def publish(root, data):
            result = original(root, data)
            (root / ".harness/artifact-pins.json").write_text("[]")
            return result

        with (
            patch.object(module, "_publish_archive", publish),
            self.assertRaises(ValueError),
        ):
            retire(self.root, plan, now=NOW)
        self.assertIsNotNone(self.store.get("checkpoints", "lead:old"))

    def test_repeated_retire_undo_and_purge_are_idempotent(self):
        self.session()
        plan = self.plan()
        retire(self.root, plan, now=NOW)
        self.assertEqual(
            retire(self.root, plan, now=NOW + 1)["already_retired"], ["old"]
        )
        unretire(self.root, "old", now=NOW + 1)
        self.assertTrue(unretire(self.root, "old", now=NOW + 2)["already_retained"])
        self.do_retire()
        plan = purge_preview(self.root, selected=["old"], now=NOW + 8 * DAY)
        purge(self.root, plan, now=NOW + 8 * DAY)
        self.assertEqual(
            purge(self.root, plan, now=NOW + 8 * DAY)["already_purged"], ["old"]
        )

    def test_close_marker_and_host_generation_are_required(self):
        self.session()
        self.store.start_session("old", "new-host")
        with self.assertRaises(RecoveryRequired):
            self.store.finish_session("old", "fixture-host", complete=True)
        self.assertIn("closure_not_finalized", self.plan()["sessions"][0]["reasons"])

    def test_real_process_exit_after_cold_publish_leaves_live_state_recoverable(self):
        self.session()
        code = """from pathlib import Path
import sys, os, homebrew_harness.storage.checkpoint_retirement as r
root=Path(sys.argv[1]);original=r._publish_archive
def crash(root,data):
 original(root,data);os._exit(77)
r._publish_archive=crash
r.retire(root,r.preview(root,selected=['old']),now=2000000000)
"""
        result = self.child(code)
        self.assertEqual(result.returncode, 77, result.stderr.decode())
        self.assertIsNotNone(self.store.get("checkpoints", "lead:old"))
        self.assertEqual(len(reconcile(self.root)["removed_prepared_orphans"]), 1)

    def test_real_process_exit_after_purge_commit_reconciles_cold_cleanup(self):
        self.session()
        self.do_retire()
        code = """from pathlib import Path
import sys, os, homebrew_harness.storage.checkpoint_retirement as r
root=Path(sys.argv[1]);now=float(sys.argv[2])
r._cleanup_cold=lambda *args:os._exit(77)
r.purge(root,r.purge_preview(root,selected=['old'],now=now),now=now)
"""
        result = self.child(code)
        self.assertEqual(result.returncode, 77, result.stderr.decode())
        self.assertEqual(self.store.get("session_meta", "old")["lifecycle"], "purged")
        self.assertTrue(self.store.get("session_meta", "old")["cleanup_pending"])
        self.assertEqual(reconcile(self.root)["cleaned_sessions"], ["old"])

    def test_actual_session_close_and_resume_update_lifecycle_without_model_call(self):
        client, _ = fake_client([reply("done")])
        runtime = HarnessSession(
            client,
            "fixture",
            self.root,
            shutdown_timeout=0.3,
            capabilities=Capabilities.full(memory_mode="off"),
        )
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            runtime.run_user("Keep the original requirement")
            sid = runtime.session_id
            runtime.close()
            self.assertTrue(runtime.wait_closed(3))
        self.assertTrue(self.store.get("session_meta", sid)["close_complete"])
        client, _ = fake_client([])
        resumed = HarnessSession(
            client,
            "fixture",
            self.root,
            resume_session=sid,
            shutdown_timeout=0.3,
            capabilities=Capabilities.full(memory_mode="off"),
        )
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            resumed.run_user("/usage")
            meta = self.store.get("session_meta", sid)
            self.assertFalse(meta["close_complete"])
            self.assertIsNone(meta["closed_at"])
            self.assertEqual(resumed.intent.original, "Keep the original requirement")
            resumed.close()
            self.assertTrue(resumed.wait_closed(3))
        client.chat.completions.create.assert_not_called()

    def test_close_error_prevents_closure_attestation(self):
        client, _ = fake_client([])
        runtime = HarnessSession(
            client,
            "fixture",
            self.root,
            shutdown_timeout=0.3,
            capabilities=Capabilities.full(memory_mode="off"),
        )
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            runtime.run_user("/usage")
            with patch.object(
                runtime.goal, "close", side_effect=ValueError("injected")
            ):
                runtime.close()
                self.assertTrue(runtime.wait_closed(3))
        self.assertFalse(
            self.store.get("session_meta", runtime.session_id)["close_complete"]
        )
        self.assertFalse(runtime.shutdown_status()["persistence_ok"])

    def test_normal_operation_writes_cannot_revive_retired_session(self):
        self.session()
        self.do_retire()
        with self.assertRaises(RecoveryRequired):
            self.store.put(
                "operations", "late", {"checkpoint": "lead:old", "state": "started"}
            )
        with self.assertRaises(RecoveryRequired):
            self.store.put(
                "checkpoints", "lead:old", {"phase": "idle", "session_id": "old"}
            )

    def test_team_mutation_invalidates_previous_close_marker(self):
        self.session()
        self.store.team_checkpoint(
            "old", {"revision": 1, "members": {}, "protocols": []}
        )
        self.assertFalse(self.store.get("session_meta", "old")["close_complete"])


class CurrentRetirementTests(RetirementCases, unittest.TestCase):
    pass
