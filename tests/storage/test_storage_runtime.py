from tests.support.paths import PROJECT_ROOT
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr

from homebrew_harness.storage.durable_runtime import DurableStore, RecoveryRequired
from homebrew_harness.storage.message_bus import MessageBus
from homebrew_harness.storage.storage_admin import backup
from homebrew_harness.storage.storage_baseline import run_baseline
from homebrew_harness.admin import main


class CurrentStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = DurableStore(self.root)

    def test_empty_reads_have_no_filesystem_side_effect(self):
        self.assertEqual(self.store.read()["checkpoints"], {})
        self.assertEqual(self.store.items("operations"), {})
        self.assertIsNone(self.store.get_meta("latest_session"))
        self.assertEqual(self.store.checkpoint_revision("lead:empty"), 0)
        self.assertFalse(self.store.backend().health()["initialized"])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_first_write_creates_only_current_private_database(self):
        self.store.put("cron", "first", {"state": "executed"})
        self.assertEqual(self.store.backend().health()["schema"], 1)
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.root / ".harness/runtime/backend.json").exists())
        self.assertFalse((self.root / ".harness/runtime/state.json").exists())

    def test_obsolete_runtime_files_are_rejected_without_conversion(self):
        directory = self.root / ".harness/runtime"
        directory.mkdir(parents=True)
        file = directory / "state.json"
        file.write_text('{"format":1}')
        with self.assertRaises(RecoveryRequired):
            self.store.read()
        with self.assertRaises(RecoveryRequired):
            self.store.put("cron", "no", {})
        self.assertEqual(file.read_text(), '{"format":1}')
        self.assertFalse(self.store.path.exists())

    def test_unsupported_sqlite_schema_is_not_upgraded(self):
        directory = self.store.path.parent
        directory.mkdir(parents=True, mode=0o700)
        connection = sqlite3.connect(self.store.path)
        connection.execute("PRAGMA user_version=3")
        connection.close()
        self.store.path.chmod(0o600)
        with self.assertRaises(RecoveryRequired):
            self.store.put("cron", "no", {})
        connection = sqlite3.connect(self.store.path)
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
        connection.close()

    def test_unrecognized_zero_version_database_is_not_modified(self):
        self.store.path.parent.mkdir(parents=True, mode=0o700)
        connection = sqlite3.connect(self.store.path)
        connection.execute("CREATE TABLE unrelated(value TEXT)")
        connection.execute("INSERT INTO unrelated VALUES('keep')")
        connection.commit()
        connection.close()
        self.store.path.chmod(0o600)
        before = self.store.path.read_bytes()
        with self.assertRaises(RecoveryRequired):
            self.store.put("cron", "no", {})
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_backup_preserves_current_schema_and_data(self):
        self.store.put("cron", "keep", {"state": "executed"})
        result = backup(self.root)
        path = self.root / result["backup"]
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        connection = sqlite3.connect(path)
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertEqual(
            connection.execute(
                "SELECT value FROM records WHERE section='cron' AND key='keep'"
            ).fetchone()[0],
            '{"state":"executed"}',
        )
        connection.close()

    def test_concurrent_initialization_and_senders_do_not_lose_messages(self):
        code = "from pathlib import Path;from homebrew_harness.storage.message_bus import MessageBus;import sys;b=MessageBus(Path(sys.argv[1]));[b.send('worker','lead',str(i)) for i in range(8)]"
        children = [
            subprocess.Popen(
                [sys.executable, "-c", code, str(self.root)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=PROJECT_ROOT,
            )
            for _ in range(3)
        ]
        for child in children:
            out, err = child.communicate(timeout=15)
            self.assertEqual(child.returncode, 0, err.decode())
        self.assertEqual(len(MessageBus(self.root).read_inbox("lead")), 24)

    def test_jsonl_sidecar_is_not_an_inbox(self):
        directory = self.root / ".mailboxes"
        directory.mkdir()
        path = directory / "lead.jsonl"
        path.write_text("old experimental file")
        self.assertEqual(MessageBus(self.root).read_inbox("lead"), [])
        self.assertEqual(path.read_text(), "old experimental file")

    def test_removed_migration_and_backend_switch_commands_are_rejected(self):
        for command in ("migrate-sqlite", "migrate-incremental", "export-json"):
            with (
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as result,
            ):
                main([command, "--cwd", str(self.root)])
            self.assertEqual(result.exception.code, 2)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_current_baseline_measures_only_current_runtime(self):
        result = run_baseline(20, 1024, 3)
        self.assertEqual(len(result["runs"]), 2)
        self.assertTrue(
            all(
                row["messages_created"] == 3 and row["history_body_reads"] == 0
                for row in result["runs"]
            )
        )

    def test_invalid_session_metadata_is_not_adopted(self):
        self.store.put(
            "session_meta", "invalid", {"format": 0, "lifecycle": "retained"}
        )
        with self.assertRaises(RecoveryRequired):
            self.store.start_session("invalid", "host")

    def test_missing_session_record_is_not_recreated_by_resume(self):
        self.store.checkpoint("lead:sample", {"session_id": "sample", "messages": []})
        self.store.remove("session_meta", "sample")
        with self.assertRaises(RecoveryRequired):
            self.store.load_session("sample")
