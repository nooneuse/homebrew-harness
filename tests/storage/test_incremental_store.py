import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from homebrew_harness.storage.durable_runtime import (
    DurableStore,
    RecoveryRequired,
    CheckpointWriter,
)
from homebrew_harness.storage.incremental_gc import preview as prune_preview, prune
from homebrew_harness.storage.incremental_store import IncrementalStore
from homebrew_harness.storage.storage_metrics import metrics_for
from homebrew_harness.context.tracked_history import (
    ConversationHistory,
    TrackedDict,
    plain,
)
from homebrew_harness.security.data_boundary import register_private_values
from homebrew_harness.orchestration.teams import TeamManager


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = DurableStore(self.root)

    def snapshot(self, messages=None, **fields):
        return {
            "session_id": "fixture",
            "phase": "running",
            "messages": ConversationHistory(messages or []),
            **fields,
        }

    def save(self, value):
        self.store.checkpoint("lead:fixture", value)

    def test_append_does_not_read_or_rewrite_old_message_bodies(self):
        messages = ConversationHistory(
            [{"role": "user", "content": str(i) + "x" * 2000} for i in range(100)]
        )
        self.save(self.snapshot(messages))
        metrics = metrics_for(self.root)
        before = metrics.snapshot()
        messages.append({"role": "assistant", "content": "new"})
        self.save(self.snapshot(messages))
        after = metrics.snapshot()
        self.assertEqual(after["messages_created"] - before["messages_created"], 1)
        self.assertEqual(after["history_body_reads"], before["history_body_reads"])
        self.assertLess(after["bytes_written"] - before["bytes_written"], 5000)

    def test_noop_does_not_publish_new_versions(self):
        value = self.snapshot([{"role": "user", "content": "hello"}])
        self.save(value)
        before = self.store.backend().health()
        revision = self.store.checkpoint_revision("lead:fixture")
        self.save(value)
        self.assertEqual(self.store.checkpoint_revision("lead:fixture"), revision)
        self.assertEqual(
            self.store.backend().health()["document_versions"],
            before["document_versions"],
        )

    def test_dynamic_header_change_keeps_core_view(self):
        value = self.snapshot(
            [
                {"role": "system", "name": "goal_context", "content": "first"},
                {"role": "user", "content": "task"},
            ]
        )
        self.save(value)
        metrics = metrics_for(self.root)
        before = metrics.snapshot()
        value["messages"][0]["content"] = "second"
        self.save(value)
        after = metrics.snapshot()
        self.assertEqual(after["views_created"], before["views_created"])
        self.assertEqual(self.store.get("checkpoints", "lead:fixture"), value)

    def test_nested_mutation_and_slice_replacement_preserve_payload(self):
        value = self.snapshot(
            [
                {
                    "role": "assistant",
                    "reasoning_content": "reason",
                    "tool_calls": [
                        {
                            "id": "x",
                            "function": {"name": "read_file", "arguments": "old"},
                        }
                    ],
                }
            ]
        )
        self.save(value)
        value["messages"][0]["tool_calls"][0]["function"]["arguments"] = "new"
        self.save(value)
        self.assertEqual(self.store.get("checkpoints", "lead:fixture"), value)
        value["messages"][:] = [{"role": "user", "content": "summary"}]
        self.save(value)
        self.assertEqual(self.store.get("checkpoints", "lead:fixture"), value)

    def test_redaction_policy_invalidates_message_cache(self):
        message = TrackedDict(
            {"role": "user", "content": "private-fixture-value-unique"}
        )
        first = message.stored_json()
        register_private_values("private-fixture-value-unique")
        self.assertNotEqual(first, message.stored_json())
        self.assertIn("[redacted]", message.stored_json()[0])

    def test_component_change_does_not_write_messages(self):
        value = self.snapshot(
            [{"role": "user", "content": "task"}],
            todo=[{"content": "test", "status": "pending"}],
        )
        self.save(value)
        metrics = metrics_for(self.root)
        before = metrics.snapshot()
        value["todo"][0]["status"] = "completed"
        self.save(value)
        after = metrics.snapshot()
        self.assertEqual(after["messages_created"], before["messages_created"])
        self.assertEqual(
            after["component_versions_created"] - before["component_versions_created"],
            1,
        )

    def test_stale_revision_rolls_back_ack_and_new_message(self):
        value = self.snapshot([{"role": "user", "content": "task"}])
        self.save(value)
        revision = self.store.checkpoint_revision("lead:fixture")
        value["messages"].append({"role": "assistant", "content": "changed"})
        self.save(value)
        self.store.mailbox(
            "send", "lead", records=[{"id": "event", "content": "pending"}]
        )
        records = self.store.mailbox("claim", "lead", owner="consumer")
        with self.assertRaises(RecoveryRequired):
            self.store.checkpoint(
                "lead:fixture",
                self.snapshot(),
                expected_revision=revision,
                ack=[("lead", "consumer", ["event"])],
            )
        self.assertTrue(self.store.mailbox("peek", "lead"))
        self.assertEqual(self.store.get("checkpoints", "lead:fixture"), value)

    def test_result_and_tool_message_are_atomic_and_idempotent(self):
        calls = [
            {
                "id": "call",
                "type": "function",
                "function": {"name": "write_file", "arguments": "{}"},
            }
        ]
        value = self.snapshot(
            [{"role": "assistant", "tool_calls": calls, "content": None}]
        )
        self.save(value)
        self.store.put(
            "operations",
            "attempt",
            {"checkpoint": "lead:fixture", "call_id": "call", "state": "started"},
        )
        completed = copy.deepcopy(value)
        completed["messages"].append(
            {"role": "tool", "tool_call_id": "call", "content": "done"}
        )
        self.store.record_result("lead:fixture", "attempt", "done", completed)
        self.assertEqual(self.store.get("operations", "attempt")["state"], "done")
        self.assertEqual(self.store.get("checkpoints", "lead:fixture"), completed)
        revision = self.store.checkpoint_revision("lead:fixture")
        self.store.record_result("lead:fixture", "attempt", "done", completed)
        self.assertEqual(self.store.checkpoint_revision("lead:fixture"), revision)
        with self.assertRaises(RecoveryRequired):
            self.store.record_result("lead:fixture", "attempt", "different", completed)

    def test_failed_result_commit_retains_started_attempt(self):
        value = self.snapshot(
            [{"role": "assistant", "content": None, "tool_calls": [{"id": "x"}]}]
        )
        self.save(value)
        self.store.put(
            "operations", "attempt", {"checkpoint": "lead:fixture", "state": "started"}
        )
        engine = self.store.backend()
        original = engine._save_doc

        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("injected")

        with patch.object(engine, "_save_doc", fail), self.assertRaises(RuntimeError):
            engine.record_result("lead:fixture", "attempt", "done", self.snapshot())
        self.assertEqual(self.store.get("operations", "attempt")["state"], "started")
        self.assertEqual(self.store.get("checkpoints", "lead:fixture"), value)

    def test_snapshot_team_keeps_old_version_and_reuses_member_messages(self):
        member = {
            "revision": 1,
            "messages": ConversationHistory([{"role": "user", "content": "member"}]),
            "status": "stopped",
        }
        old_team = {"revision": 1, "members": {"worker": member}}
        value = self.snapshot([], team=old_team)
        self.save(value)
        self.store.checkpoint(
            "member:fixture:worker",
            {"session_id": "fixture", "messages": member["messages"]},
        )
        engine = self.store.backend()
        with engine.connection() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM inc_messages WHERE owner=?",
                    ("member:fixture:worker",),
                ).fetchone()[0],
                1,
            )
        newer = {
            "revision": 2,
            "members": {
                "worker": {
                    "revision": 2,
                    "messages": member["messages"],
                    "status": "failed",
                }
            },
        }
        self.store.team_checkpoint("fixture", newer)
        self.assertEqual(
            self.store.get("checkpoints", "lead:fixture")["team"], old_team
        )
        self.assertEqual(self.store.get("team", "fixture"), newer)
        prune(self.root, prune_preview(self.root))
        self.assertEqual(
            self.store.get("checkpoints", "lead:fixture")["team"], old_team
        )

    def test_view_cleanup_preserves_current_context_and_is_stale_checked(self):
        self.save(self.snapshot([{"role": "user", "content": "old"}]))
        self.save(self.snapshot([{"role": "user", "content": "new"}]))
        plan = prune_preview(self.root)
        self.assertGreater(plan["messages"], 0)
        prune(self.root, plan)
        self.assertEqual(
            self.store.get("checkpoints", "lead:fixture")["messages"][0]["content"],
            "new",
        )
        with self.assertRaises(ValueError):
            prune(self.root, plan)

    def test_release_and_reactivate_document_keeps_gc_root(self):
        value = self.snapshot([{"role": "user", "content": "keep"}])
        self.save(value)
        self.store.remove("checkpoints", "lead:fixture")
        self.save(value)
        prune(self.root, prune_preview(self.root))
        self.assertEqual(self.store.get("checkpoints", "lead:fixture"), value)

    def test_context_message_corruption_fails_closed(self):
        self.save(self.snapshot([{"role": "user", "content": "safe"}]))
        engine = self.store.backend()
        with engine.write() as conn:
            conn.execute("UPDATE inc_messages SET value='{}'")
        with self.assertRaises(RecoveryRequired):
            self.store.get("checkpoints", "lead:fixture")

    def test_scoped_restore_does_not_load_unrelated_context(self):
        self.save(self.snapshot([{"role": "user", "content": "target"}]))
        self.store.checkpoint(
            "lead:other",
            {
                "session_id": "other",
                "messages": [{"role": "user", "content": "unrelated"}],
            },
        )
        with self.store.backend().write() as conn:
            conn.execute("UPDATE inc_messages SET value='{}' WHERE owner='lead:other'")
        state = self.store.load_session("fixture")
        self.assertEqual(set(state["checkpoints"]), {"lead:fixture"})

    def test_team_snapshot_reads_do_not_increment_revision(self):
        manager = TeamManager(self.root, lambda member: None)
        before = manager.checkpoint()
        self.assertEqual(manager.checkpoint(), before)
