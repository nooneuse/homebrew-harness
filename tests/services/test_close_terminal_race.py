"""A repeated close between terminal persistence and lease release is harmless."""

import unittest
from unittest.mock import patch
from tests.services import test_runtime as fixture


class TerminalClose(unittest.TestCase):
    setUp = fixture.ServiceRuntimeTests.setUp
    host = fixture.ServiceRuntimeTests.host
    spec = fixture.ServiceRuntimeTests.spec

    def test_close_reentry_after_stopped_record_keeps_original_owner_cleanup(self):
        host, _ = self.host([])
        service = host.services.start(self.spec())
        self.addCleanup(lambda: (service.close(), service.wait_closed(10)))
        runtime = service.runtime
        original = runtime.store.update
        observed = []

        def update(header, **changes):
            result = original(header, **changes)
            if changes.get("state") == "stopped":
                observed.append(service.close())
            return result

        with patch.object(runtime.store, "update", side_effect=update):
            service.close()
            self.assertTrue(service.wait_closed(10))
        self.assertEqual(len(observed), 1)
        self.assertEqual(service.snapshot().state, "stopped")
