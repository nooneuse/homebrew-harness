import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from homebrew_harness import HarnessSession
from homebrew_harness.storage.durable_runtime import settle_activity, track_activity
from tests.support.clients import fake_client


class OperationRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        client, _ = fake_client([])
        self.session = HarnessSession(client, 'fixture', Path(temporary.name).resolve())
        self.addCleanup(lambda: self.assertTrue(self.session.wait_closed(5)))
        self.addCleanup(self.session.close)
        self.session.start(include_clock=False)

    def operation(self, state='started', *, active=False, child=False):
        session = self.session
        identity = 'operation:' + state
        checkpoint = 'sub:' + session.session_id + ':fixture' if child else session.checkpoint_key
        session.runtime_store.put('operations', identity, {
            'checkpoint': checkpoint, 'state': state, 'revision': 0,
            'name': 'fixture', 'call_id': identity,
        })
        if active:
            key = (str(session.workdir), identity)
            resource = threading.current_thread()
            track_activity(resource, key)
            self.addCleanup(settle_activity, key, resource)
        return identity

    def test_checkpoint_keeps_live_started_operation_pending(self):
        identity = self.operation(active=True)
        self.session._save_checkpoint('yielded')
        self.assertEqual(self.session._recovery_blocked, [])
        self.assertEqual(self.session.runtime_store.get('operations', identity)['state'], 'started')
        self.assertEqual(self.session.runtime_store.get('checkpoints', self.session.checkpoint_key)['recovery_blocked'], [])

    def test_save_and_refresh_retain_an_unowned_started_operation(self):
        identity = self.operation()
        for refresh in (self.session._refresh_recovery, self.session._save_checkpoint):
            self.session._recovery_blocked.clear()
            refresh()
            self.assertEqual(self.session._recovery_blocked, [identity])

    def test_actual_unknown_result_requires_inspection_even_while_settling(self):
        identity = self.operation('unknown', active=True)
        self.session._save_checkpoint()
        self.assertEqual(self.session._recovery_blocked, [identity])

    def test_child_unknown_does_not_confuse_a_live_peer(self):
        self.operation(active=True)
        unknown = self.operation('unknown', child=True)
        self.session._save_checkpoint()
        self.assertEqual(self.session._recovery_blocked, [unknown])

    def test_result_committed_during_scan_is_not_retained_as_interrupted(self):
        identity = self.operation()
        scanned = self.session.recovery_operations()
        self.session.runtime_store.update_fields('operations', identity, {'state': 'done'})
        with patch.object(self.session, 'recovery_operations', return_value=scanned):
            self.session._save_checkpoint()
            self.session._refresh_recovery()
        self.assertEqual(self.session._recovery_blocked, [])

    def test_successful_checkpoint_retry_clears_its_previous_persistence_error(self):
        self.session._persistence_error = 'TemporaryStorageFailure'
        self.session._save_checkpoint()
        self.assertIsNone(self.session._persistence_error)
        self.assertEqual(self.session._recovery_blocked, [])
