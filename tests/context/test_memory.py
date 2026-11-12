"""Persistence, selective recall, grounded extraction, and failure recovery."""

from __future__ import annotations

from tests.support.clients import cli_model_factory

import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from homebrew_harness.runtime.loop_components import LoopComponents
import homebrew_harness.cli
import homebrew_harness.session_cli
import homebrew_harness.configuration
import homebrew_harness.context.memory
import homebrew_harness.runtime.hooks
import homebrew_harness.runtime.turns
from homebrew_harness.context.compaction import ContextCompactor
from homebrew_harness.context.memory import (
    MemoryError,
    MemoryRecord,
    MemoryRuntime,
    MemoryStore,
    RECALL_CHAR_LIMIT,
)
from homebrew_harness.context.skill_loader import SkillLoader
from tests.support.clients import call, reply
from tests.context.test_compaction import fake_client
from homebrew_harness.context.todo import TodoManager


def record(
    name="indentation",
    body="Use tabs for indentation.",
    *,
    description="Indentation preference",
    updated="2026-01-01T00:00:00+00:00",
):
    return MemoryRecord(name, "user", description, body, body[:1000], updated)


def candidate(
    name="indentation",
    body="Use tabs for indentation.",
    *,
    evidence=None,
    scope="persistent",
):
    return {
        "name": name,
        "type": "user",
        "description": "Indentation preference",
        "body": body,
        "evidence": body if evidence is None else evidence,
        "scope": scope,
    }


def json_reply(value):
    return reply(json.dumps(value, ensure_ascii=False))


class MemoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.workdir = self.root / "work"
        self.workdir.mkdir()
        self.store = MemoryStore(self.workdir)
        self.stderr = io.StringIO()
        capture = redirect_stderr(self.stderr)
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def seed(self, records):
        self.store.replace(records, self.store.snapshot().version)

    def runtime(self, responses):
        client, requests = fake_client(responses)
        return MemoryRuntime(client, "test-model", self.workdir, mode="auto"), requests

    def test_empty_store_reads_without_creating_files(self):
        runtime, requests = self.runtime([])
        self.assertEqual(runtime.recall("Any preferences?"), "")
        self.assertEqual(self.store.snapshot().records, ())
        self.assertFalse(self.store.root.exists())
        self.assertEqual(requests, [])

    def test_markdown_round_trip_and_index_contains_only_metadata(self):
        original = record(body="Prefer tabs.\nThis is the detailed body.")
        self.seed([original])
        restored = MemoryStore(self.workdir).snapshot().records
        self.assertEqual(restored, (original,))
        index = (self.store.root / "MEMORY.md").read_text()
        self.assertIn("[indentation](indentation.md)", index)
        self.assertIn("Indentation preference", index)
        self.assertNotIn("detailed body", index)

    def test_private_modes_and_backup_preserve_previous_records(self):
        self.seed([record()])
        before = (self.store.root / "indentation.md").read_text()
        self.store.replace(
            [record(body="Use spaces.")], self.store.snapshot().version, backup=True
        )
        backup = next((self.store.root / ".backups").glob("*.json"))
        self.assertEqual(json.loads(backup.read_text())["indentation.md"], before)
        self.assertEqual(stat.S_IMODE(self.store.root.stat().st_mode), 0o700)
        for path in (
            self.store.root / "indentation.md",
            self.store.root / "MEMORY.md",
            backup,
        ):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_stale_write_is_rejected_after_another_update(self):
        self.seed([record()])
        stale = self.store.snapshot()
        other = MemoryStore(self.workdir)
        other.replace([record(body="New value.")], stale.version)
        with self.assertRaisesRegex(MemoryError, "changed during analysis"):
            self.store.replace([record(body="Stale value.")], stale.version)
        self.assertEqual(self.store.snapshot().records[0].body, "New value.")

    def test_store_lock_rejects_a_second_writer(self):
        self.seed([record()])
        with self.store._lock(), self.assertRaisesRegex(MemoryError, "busy"):
            MemoryStore(self.workdir).snapshot()

    def test_index_write_failure_rolls_back_new_files_and_original_contents(self):
        self.seed([record()])
        before = self.store.snapshot()
        original_write = self.store._atomic_write
        failed = False

        def fail_index_once(path, content):
            nonlocal failed
            if path.name == "MEMORY.md" and not failed:
                failed = True
                raise OSError("simulated failure")
            original_write(path, content)

        with (
            patch.object(self.store, "_atomic_write", side_effect=fail_index_once),
            self.assertRaisesRegex(MemoryError, "restored"),
        ):
            self.store.replace(
                [record(body="Changed"), record("new-topic")],
                before.version,
                backup=True,
            )
        self.assertEqual(self.store.snapshot(), before)
        self.assertFalse((self.store.root / "new-topic.md").exists())

    def test_symlink_root_and_record_do_not_write_outside_store(self):
        outside = self.root / "outside"
        outside.mkdir()
        self.store.root.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(MemoryError):
            self.store.snapshot()
        self.store.root.unlink()
        self.seed([record()])
        (outside / "valuable.md").write_text("keep")
        (self.store.root / "linked.md").symlink_to(outside / "valuable.md")
        snapshot = self.store.snapshot()
        with self.assertRaises(MemoryError):
            self.store.replace([*snapshot.records, record("linked")], snapshot.version)
        self.assertEqual((outside / "valuable.md").read_text(), "keep")

    def test_invalid_record_is_skipped_and_never_overwritten_as_a_new_record(self):
        self.seed([record()])
        (self.store.root / "broken.md").write_text("not a memory")
        snapshot = self.store.snapshot()
        self.assertEqual(len(snapshot.records), 1)
        with self.assertRaises(MemoryError):
            self.store.replace([*snapshot.records, record("broken")], snapshot.version)
        self.assertEqual((self.store.root / "broken.md").read_text(), "not a memory")

    def test_memory_names_and_yaml_tags_are_validated(self):
        for name in ("../escape", "nested/name", "Uppercase", "MEMORY"):
            with self.subTest(name=name), self.assertRaises(MemoryError):
                MemoryRecord.validate({**record().__dict__, "name": name})
        with self.assertRaises(MemoryError):
            MemoryRecord.parse(
                "---\n!!python/object/apply:os.system ['false']\n---\nBody"
            )

    def test_persistent_candidate_is_saved_and_recalled_in_a_new_runtime(self):
        request = "Use tabs for indentation."
        runtime, _ = self.runtime([json_reply([candidate()])])
        self.assertEqual(runtime.extract(request), 1)
        new_runtime, requests = self.runtime([json_reply([0])])
        recalled = json.loads(new_runtime.recall("How should code be indented?"))
        self.assertEqual(recalled[0]["body"], request)
        self.assertEqual(new_runtime.last_recalled, ("indentation.md",))
        selector = json.loads(requests[0]["messages"][1]["content"])
        self.assertNotIn("body", selector["catalog"][0])
        self.assertNotIn("tools", requests[0])

    def test_current_task_and_ungrounded_candidates_are_not_saved(self):
        proposed = [
            candidate(scope="current_task"),
            candidate(evidence="Invented evidence"),
            {key: value for key, value in candidate().items() if key != "scope"},
        ]
        runtime, _ = self.runtime([json_reply(proposed)])
        self.assertEqual(runtime.extract("Use tabs for indentation."), 0)
        self.assertFalse(self.store.root.exists())

    def test_temporal_qualifier_cannot_be_removed_from_evidence_to_make_it_persistent(
        self,
    ):
        runtime, _ = self.runtime([json_reply([candidate(evidence="use tabs")])])
        self.assertEqual(runtime.extract("For this session, use tabs."), 0)
        self.assertFalse(self.store.root.exists())

    def test_mixed_temporary_and_durable_sentences_keep_only_the_durable_fact(self):
        source = "This session use tabs. For future work, prefer spaces."
        values = [
            candidate(evidence="use tabs"),
            candidate(
                "spaces",
                "Prefer spaces for indentation.",
                evidence="For future work, prefer spaces.",
            ),
        ]
        runtime, _ = self.runtime([json_reply(values)])
        self.assertEqual(runtime.extract(source), 1)
        self.assertEqual(
            [record.name for record in self.store.snapshot().records], ["spaces"]
        )

    def test_duplicate_body_is_ignored_and_same_topic_correction_updates_one_file(self):
        self.seed([record()])
        runtime, _ = self.runtime([json_reply([candidate("different-name")])])
        version = self.store.snapshot().version
        self.assertEqual(runtime.extract("Use tabs for indentation."), 0)
        self.assertEqual(self.store.snapshot().version, version)
        runtime, _ = self.runtime(
            [json_reply([candidate(body="Use spaces for indentation.")])]
        )
        self.assertEqual(runtime.extract("Use spaces for indentation."), 1)
        records = self.store.snapshot().records
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].body, "Use spaces for indentation.")

    def test_credentials_are_redacted_before_extraction_and_rejected_from_storage(self):
        secret = "private-credential-test-value"
        with patch.dict(os.environ, {"OPENAI_API_KEY": secret}):
            runtime, requests = self.runtime(
                [json_reply([candidate(body=secret, evidence=secret)])]
            )
        self.assertEqual(runtime.extract("Remember " + secret), 0)
        self.assertNotIn(secret, requests[0]["messages"][1]["content"])
        self.assertFalse(self.store.root.exists())
        self.assertNotIn(secret, self.stderr.getvalue())

    def test_manually_added_credential_record_is_not_sent_to_selection(self):
        self.seed([record(body="api_key=unpublished-test-value")])
        runtime, requests = self.runtime([])
        self.assertEqual(runtime.recall("What credentials are saved?"), "")
        self.assertEqual(requests, [])

    def test_existing_sensitive_record_is_not_copied_into_a_new_backup(self):
        self.seed([record(body="api_key=unpublished-test-value")])
        runtime, requests = self.runtime([])
        self.assertEqual(runtime.extract("Use tabs for indentation."), 0)
        self.assertFalse((self.store.root / ".backups").exists())
        self.assertEqual(requests, [])

    def test_all_four_supported_types_can_be_saved_with_user_evidence(self):
        for mem_type in ("user", "feedback", "project", "reference"):
            values = candidate(name=mem_type)
            values["type"] = mem_type
            values["body"] = "Durable " + mem_type
            values["evidence"] = values["body"]
            runtime, _ = self.runtime([json_reply([values])])
            self.assertEqual(runtime.extract(values["body"]), 1)
        self.assertEqual(
            {entry.type for entry in self.store.snapshot().records},
            {"user", "feedback", "project", "reference"},
        )

    def test_recall_only_loads_selected_records_and_current_context_replaces_old_attachment(
        self,
    ):
        self.seed(
            [
                record("alpha", body="Selected detail"),
                record("beta", body="UNRELATED_BODY"),
            ]
        )
        runtime, requests = self.runtime([json_reply([0])])
        recalled = runtime.recall("Relevant preference")
        self.assertIn("Selected detail", recalled)
        self.assertNotIn("UNRELATED_BODY", recalled)
        messages = [{"role": "system", "content": "Base policy"}]
        homebrew_harness.runtime.turns.set_memory_context(messages, recalled)
        homebrew_harness.runtime.turns.set_memory_context(messages, "NEW_CONTEXT")
        self.assertEqual(len(messages), 2)
        self.assertNotIn("Selected detail", str(messages))
        self.assertIn("current user request takes precedence", messages[1]["content"])
        homebrew_harness.runtime.turns.set_memory_context(messages, "")
        self.assertEqual(messages, [{"role": "system", "content": "Base policy"}])
        self.assertNotIn("Selected detail", requests[0]["messages"][1]["content"])

    def test_selection_failure_uses_keyword_fallback_without_private_error_details(
        self,
    ):
        self.seed(
            [
                record("indentation"),
                record("snack", description="Snack preference", body="Prefer fruit."),
            ]
        )
        runtime, _ = self.runtime([RuntimeError("private-test-error")])
        recalled = json.loads(runtime.recall("indentation preference"))
        self.assertEqual([entry["source"] for entry in recalled], ["indentation.md"])
        self.assertNotIn("private-test-error", self.stderr.getvalue())

    def test_recall_limits_record_count_and_character_budget(self):
        self.seed(
            [
                record(f"item-{index}", body=f"Record {index}: " + "x" * 1800)
                for index in range(8)
            ]
        )
        runtime, _ = self.runtime([json_reply(list(range(8)))])
        recalled = runtime.recall("All preferences")
        self.assertLessEqual(len(json.loads(recalled)), 5)
        self.assertLessEqual(len(recalled), RECALL_CHAR_LIMIT)
        runtime, requests = self.runtime([])
        self.assertEqual(runtime.recall("Task", max_chars=0), "")
        self.assertEqual(requests, [])

    def test_consolidation_merges_sources_and_preserves_unrelated_records(self):
        self.seed(
            [
                record("old", body="Use tabs.", updated="2025-01-01T00:00:00+00:00"),
                record("new", body="Use spaces.", updated="2026-01-01T00:00:00+00:00"),
                record("unrelated", body="Prefer concise reports."),
            ]
        )
        proposals = [
            {
                "name": "new",
                "type": "user",
                "description": "Indentation",
                "body": "Use spaces.",
                "source_names": ["old", "new"],
            },
            {
                "name": "unrelated",
                "type": "user",
                "description": "Reporting",
                "body": "Prefer concise reports.",
                "source_names": ["unrelated"],
            },
        ]
        runtime, requests = self.runtime([json_reply(proposals)])
        with patch("homebrew_harness.context.memory.CONSOLIDATE_THRESHOLD", 3):
            self.assertEqual(runtime.consolidate(), 2)
        records = {entry.name: entry for entry in self.store.snapshot().records}
        self.assertEqual(set(records), {"new", "unrelated"})
        self.assertEqual(records["new"].evidence, "Use spaces.")
        self.assertTrue(list((self.store.root / ".backups").glob("*.json")))
        self.assertNotIn("tools", requests[0])

    def test_invalid_consolidation_cannot_silently_drop_records(self):
        self.seed([record("one"), record("two", body="Another preference.")])
        original = self.store.snapshot()
        variants = [
            [],
            [
                {
                    "name": "one",
                    "type": "user",
                    "description": "x",
                    "body": "x",
                    "source_names": ["one"],
                }
            ],
            [
                {
                    "name": "one",
                    "type": "user",
                    "description": "x",
                    "body": "x",
                    "source_names": ["one", "unknown"],
                }
            ],
        ]
        for proposed in variants:
            runtime, _ = self.runtime([json_reply(proposed)])
            with patch("homebrew_harness.context.memory.CONSOLIDATE_THRESHOLD", 2):
                self.assertEqual(runtime.consolidate(), 0)
            self.assertEqual(self.store.snapshot(), original)

    def test_consolidation_only_runs_after_a_successful_extraction(self):
        runtime, _ = self.runtime([])
        with (
            patch.object(runtime, "extract", return_value=0),
            patch.object(runtime, "consolidate") as consolidate,
        ):
            runtime.finish_turn("Task")
        consolidate.assert_not_called()

    def test_original_user_request_is_used_after_manual_context_compaction(self):
        query = "Use tabs for indentation."
        client, requests = fake_client(
            [
                reply(calls=[call("compact", name="compact", arguments="{}")]),
                reply("SUMMARY_IS_NOT_EVIDENCE"),
                reply("I have finished."),
                json_reply([candidate()]),
            ]
        )
        runtime = MemoryRuntime(client, "test-model", self.workdir, mode="auto")
        messages = [{"role": "system", "content": "Base policy"}]
        result = homebrew_harness.runtime.turns.run_user_turn(
            client,
            "test-model",
            messages,
            query,
            self.workdir,
            memory_runtime=runtime,
            components=LoopComponents(
                hooks=homebrew_harness.runtime.hooks.create_hooks(),
                todo_manager=TodoManager(),
                skill_loader=SkillLoader(self.workdir / "skills"),
                compactor=ContextCompactor(client, "test-model", self.workdir),
            ),
        )
        self.assertEqual(result, "I have finished.")
        extraction = requests[-1]["messages"][1]["content"]
        self.assertEqual(json.loads(extraction)["user_input"], query)
        self.assertNotIn("SUMMARY_IS_NOT_EVIDENCE", extraction)
        self.assertNotIn("I have finished", extraction)
        self.assertEqual(self.store.snapshot().records[0].body, query)

    def test_user_turn_failure_does_not_extract_and_memory_failure_does_not_hide_success(
        self,
    ):
        client, _ = fake_client([RuntimeError("working failure")])
        runtime = MemoryRuntime(client, "test-model", self.workdir, mode="auto")
        with (
            patch.object(runtime, "finish_turn") as finish,
            self.assertRaises(RuntimeError),
        ):
            homebrew_harness.runtime.turns.run_user_turn(
                client,
                "test-model",
                [],
                "Task",
                self.workdir,
                memory_runtime=runtime,
                components=LoopComponents(
                    hooks=homebrew_harness.runtime.hooks.create_hooks(),
                    todo_manager=TodoManager(),
                    skill_loader=SkillLoader(self.workdir / "skills"),
                    compactor=ContextCompactor(client, "test-model", self.workdir),
                ),
            )
        finish.assert_not_called()
        client, _ = fake_client(
            [reply("Primary result"), RuntimeError("private-maintenance-error")]
        )
        runtime = MemoryRuntime(client, "test-model", self.workdir, mode="auto")
        result = homebrew_harness.runtime.turns.run_user_turn(
            client,
            "test-model",
            [],
            "Task",
            self.workdir,
            memory_runtime=runtime,
            components=LoopComponents(
                hooks=homebrew_harness.runtime.hooks.create_hooks(),
                todo_manager=TodoManager(),
                skill_loader=SkillLoader(self.workdir / "skills"),
                compactor=ContextCompactor(client, "test-model", self.workdir),
            ),
        )
        self.assertEqual(result, "Primary result")
        self.assertNotIn("private-maintenance-error", self.stderr.getvalue())

    def test_no_memory_cli_does_not_construct_or_use_the_runtime(self):
        client, requests = fake_client([reply("Done.")])
        config = {
            name: "fixture-model" if name == "OPENAI_MODEL" else "test-value"
            for name in homebrew_harness.configuration.CONFIG_VARIABLES
        }
        with (
            patch.dict(os.environ, config),
            patch("homebrew_harness.session_cli.load_dotenv"),
            cli_model_factory() as factory,
            patch("homebrew_harness.context.memory.MemoryRuntime") as runtime,
            redirect_stdout(io.StringIO()),
        ):
            factory.return_value.__enter__.return_value = client
            self.assertEqual(
                homebrew_harness.cli.main(["session"] +
                    ["--profile", "full"]
                    + ["--cwd", str(self.workdir), "--memory-mode", "off", "Task"]
                ),
                0,
            )
        runtime.assert_not_called()
        self.assertEqual(len(requests), 1)
        self.assertFalse(self.store.root.exists())

    def test_cli_recall_is_visible_to_model_but_not_inherited_by_child(self):
        self.seed([record()])
        client, requests = fake_client(
            [
                json_reply([0]),
                reply(
                    calls=[
                        call("task", name="task", arguments='{"prompt":"Say hello."}')
                    ]
                ),
                reply("Child hello"),
                reply("Parent hello"),
                json_reply([]),
            ]
        )
        config = {
            name: "fixture-model" if name == "OPENAI_MODEL" else "test-value"
            for name in homebrew_harness.configuration.CONFIG_VARIABLES
        }
        with (
            patch.dict(os.environ, config),
            patch("homebrew_harness.session_cli.load_dotenv"),
            cli_model_factory() as factory,
            redirect_stdout(io.StringIO()),
        ):
            factory.return_value.__enter__.return_value = client
            self.assertEqual(
                homebrew_harness.cli.main(["session"] +
                    ["--profile", "full", "--memory-mode", "auto"]
                    + ["--cwd", str(self.workdir), "Delegate a greeting."]
                ),
                0,
            )
        self.assertTrue(
            any(
                message.get("name") == "memory_context"
                for message in requests[1]["messages"]
            )
        )
        self.assertFalse(
            any(
                message.get("name") == "memory_context"
                for message in requests[2]["messages"]
            )
        )
        self.assertNotIn(
            "Use tabs for indentation.", json.dumps(requests[2]["messages"])
        )
        extraction_requests = [
            request
            for request in requests
            if request["messages"][0]["content"].startswith("Memory extraction.")
        ]
        self.assertEqual(len(extraction_requests), 1)


if __name__ == "__main__":
    unittest.main()
