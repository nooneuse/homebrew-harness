import copy
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from homebrew_harness.execution.execution_control import cleanup_scope, persistence_scope, control_scope
from homebrew_harness.execution.model_gateway import Deadline
from homebrew_harness.storage.durable_runtime import DurableStore
from homebrew_harness.storage.persistence import StorageTimeout
from homebrew_harness.runtime.errors import error_info
from homebrew_harness.storage.sqlite_store import BoundConnection
from homebrew_harness.runtime.runtime_state import RecoveryRequired


class PersistenceTimeouts(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.store = DurableStore(self.root)
        self.db = self.store.backend()
        self.value = {'session_id': 'fixture', 'phase': 'running', 'messages': [{'role': 'user', 'content': 'Original task'}]}
        self.store.checkpoint('lead:fixture', self.value)

    def test_transaction_longer_than_old_two_seconds_commits(self):
        with self.db.write() as conn:
            self.db._put(conn, 'cron', 'long', {'state': 'done'})
            time.sleep(2.05)
        self.assertEqual(self.store.get('cron', 'long')['state'], 'done')

    def test_lock_wait_longer_than_old_window_then_checkpoint_commits(self):
        ready = threading.Event()
        errors = []
        def hold():
            try:
                with self.db.connection() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    ready.set()
                    time.sleep(2.05)
                    conn.execute('ROLLBACK')
            except Exception as error:
                errors.append(error)
        worker = threading.Thread(target=hold)
        worker.start()
        self.assertTrue(ready.wait(2))
        self.store.checkpoint('lead:fixture', {**self.value, 'phase': 'idle'})
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.store.get('checkpoints', 'lead:fixture')['phase'], 'idle')

    def test_explicit_transaction_timeout_rolls_back_with_diagnostics(self):
        self.db.transaction_timeout = .02
        with self.assertRaises(StorageTimeout) as failure:
            with self.db.write() as conn:
                self.db._put(conn, 'cron', 'timeout', {'state': 'done'})
                time.sleep(.04)
        self.assertIsNone(self.store.get('cron', 'timeout'))
        info = error_info(failure.exception)
        self.assertEqual(info['code'], 'storage_commit_timeout')
        self.assertEqual(info['details']['phase'], 'commit')
        self.assertTrue(info['details']['rollback_confirmed'])
        self.assertEqual(info['details']['limit_value'], .02)
        self.assertTrue(failure.exception.retryable)

    def test_checkpoint_retries_rolled_back_attempt_once(self):
        original = self.db._save_doc
        self.db.transaction_timeout = .02
        calls = []
        def delayed(*args, **kwargs):
            result = original(*args, **kwargs)
            calls.append(True)
            if len(calls) == 1:
                time.sleep(.04)
            return result
        proposed = copy.deepcopy(self.value)
        proposed['messages'].append({'role': 'assistant', 'content': 'Verified result'})
        previous = self.store.checkpoint_revision('lead:fixture')
        with patch.object(self.db, '_save_doc', side_effect=delayed):
            self.store.checkpoint('lead:fixture', proposed, expected_revision=previous)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.store.checkpoint_revision('lead:fixture'), previous + 1)
        self.assertEqual(self.store.get('checkpoints', 'lead:fixture'), proposed)

    def test_result_retry_preserves_one_output_and_receipt(self):
        self.store.put('operations', 'attempt:call', {'checkpoint': 'lead:fixture', 'call_id': 'call', 'state': 'started'})
        original = self.db._save_doc
        self.db.transaction_timeout = .02
        calls = []
        def delayed(*args, **kwargs):
            result = original(*args, **kwargs);calls.append(True)
            if len(calls) == 1:time.sleep(.04)
            return result
        with patch.object(self.db, '_save_doc', side_effect=delayed):
            self.store.record_result('lead:fixture', 'attempt:call', '{"result":"done"}', self.value)
        with self.db.connection() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM inc_results WHERE id=?', ('attempt:call',)).fetchone()[0], 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.store.get('operations', 'attempt:call')['state'], 'done')

    def test_retry_exhaustion_preserves_previous_checkpoint(self):
        original = self.db._save_doc
        self.db.transaction_timeout = .02
        calls = []
        def slow(*args, **kwargs):
            result = original(*args, **kwargs);calls.append(True);time.sleep(.04);return result
        with patch.object(self.db, '_save_doc', side_effect=slow), self.assertRaises(StorageTimeout) as failure:
            self.store.checkpoint('lead:fixture', {**self.value, 'phase': 'idle'})
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.store.get('checkpoints', 'lead:fixture'), self.value)
        self.assertEqual(failure.exception.details['attempts'], 3)
        self.assertEqual(failure.exception.details['retry_stop'], 'attempt_limit')

    def test_existing_close_window_is_not_extended_by_persistence(self):
        with cleanup_scope(.03), persistence_scope(60, operation='close_checkpoint'):
            with self.assertRaises(StorageTimeout) as failure:
                with self.db.write() as conn:
                    self.db._put(conn, 'cron', 'close', {'state': 'done'})
                    time.sleep(.05)
        self.assertIsNone(self.store.get('cron', 'close'))
        self.assertEqual(failure.exception.code, 'storage_cleanup_timeout')
        self.assertEqual(failure.exception.details['operation'], 'close_checkpoint')

    def test_expired_business_deadline_still_allows_fact_persistence(self):
        with control_scope(Deadline.after(0, source='run')):
            self.store.checkpoint('lead:fixture', {**self.value, 'phase': 'idle'})
        self.assertEqual(self.store.get('checkpoints', 'lead:fixture')['phase'], 'idle')

    def test_unknown_commit_or_binding_errors_are_not_retried(self):
        with patch.object(self.db, 'checkpoint', side_effect=sqlite3.OperationalError('uncertain commit')) as write, self.assertRaises(sqlite3.OperationalError):
            self.store.checkpoint('lead:fixture', self.value)
        write.assert_called_once()

    def test_lost_commit_confirmation_does_not_replay_checkpoint_body(self):
        execute = BoundConnection.execute
        original_save = self.db._save_doc
        calls = []
        def saved(*args, **kwargs):
            calls.append(True)
            return original_save(*args, **kwargs)
        def lost(connection, sql, *args, **kwargs):
            result = execute(connection, sql, *args, **kwargs)
            if sql == 'COMMIT':
                raise sqlite3.OperationalError('lost confirmation after commit')
            return result
        with patch.object(self.db, '_save_doc', side_effect=saved), patch.object(BoundConnection, 'execute', lost), self.assertRaises(RecoveryRequired) as failure:
            self.store.checkpoint('lead:fixture', {**self.value, 'phase': 'idle'})
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.store.get('checkpoints', 'lead:fixture')['phase'], 'idle')
        self.assertEqual(error_info(failure.exception)['code'], 'storage_commit_unknown')
        self.assertEqual(error_info(failure.exception)['details']['commit_state'], 'unknown')

    def test_checkpoint_retry_commits_inbox_ack_once(self):
        self.store.mailbox('send', 'worker', records=[{'id': 'message', 'content': 'work'}])
        self.store.mailbox('claim', 'worker', owner='receiver')
        original = self.db._save_doc
        self.db.transaction_timeout = .02
        calls = []
        def delayed(*args, **kwargs):
            result = original(*args, **kwargs);calls.append(True)
            if len(calls) == 1:time.sleep(.04)
            return result
        with patch.object(self.db, '_save_doc', side_effect=delayed):
            self.store.checkpoint('lead:fixture', {**self.value, 'phase': 'idle'}, ack=((name, owner, (item for item in ['message'])) for name, owner in [('worker', 'receiver')]))
        self.assertEqual(self.store.mailbox('inspect','worker'), [])
        self.assertEqual(len(calls), 2)

    def test_nested_persistence_does_not_renew_total_window(self):
        with persistence_scope(.02):
            time.sleep(.03)
            with self.assertRaises(StorageTimeout) as failure:
                self.store.checkpoint('lead:fixture', self.value)
        self.assertEqual(failure.exception.details['limit_value'], .02)
        self.assertGreater(failure.exception.details['elapsed_seconds'], .02)

    def test_nested_cleanup_uses_the_shorter_window(self):
        with cleanup_scope(60), cleanup_scope(.02), persistence_scope(60):
            time.sleep(.03)
            with self.assertRaises(StorageTimeout) as failure:
                self.store.checkpoint('lead:fixture', self.value)
        self.assertEqual(failure.exception.code, 'storage_cleanup_timeout')

    def test_commit_busy_retries_sql_without_replaying_transaction_body(self):
        execute = BoundConnection.execute
        commits = []
        bodies = []
        def busy_once(connection, sql, *args, **kwargs):
            if sql == 'COMMIT':
                commits.append(True)
                if len(commits) == 1:
                    raise sqlite3.OperationalError('database is busy')
            return execute(connection, sql, *args, **kwargs)
        with patch.object(BoundConnection, 'execute', busy_once):
            with self.db.write() as conn:
                bodies.append(True)
                self.db._put(conn, 'cron', 'busy_commit', {'state': 'done'})
        self.assertEqual(len(commits), 2)
        self.assertEqual(len(bodies), 1)
        self.assertEqual(self.store.get('cron', 'busy_commit')['state'], 'done')

    def test_failed_rollback_does_not_replay_write_and_reports_unknown_state(self):
        execute = BoundConnection.execute
        original = self.db._save_doc
        self.db.transaction_timeout = .02
        calls = []
        def delayed(*args, **kwargs):
            calls.append(True)
            result = original(*args, **kwargs)
            time.sleep(.04)
            return result
        def no_rollback(connection, sql, *args, **kwargs):
            if sql == 'ROLLBACK':
                raise sqlite3.OperationalError('rollback unavailable')
            return execute(connection, sql, *args, **kwargs)
        with patch.object(self.db, '_save_doc', side_effect=delayed), patch.object(BoundConnection, 'execute', no_rollback), self.assertRaises(RecoveryRequired) as failure:
            self.store.checkpoint('lead:fixture', {**self.value, 'phase': 'idle'})
        self.assertEqual(len(calls), 1)
        info = error_info(failure.exception)
        self.assertEqual(info['code'], 'storage_rollback_failed')
        self.assertEqual(info['details']['commit_state'], 'unknown')
        self.assertFalse(info['details']['rollback_confirmed'])
        self.assertEqual(self.store.get('checkpoints', 'lead:fixture'), self.value)
