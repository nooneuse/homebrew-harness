import unittest
from unittest.mock import patch
from homebrew_harness.platforms.contracts import ObjectRef
from tests.services.communication import test_flow as fixture


class DeferredIdentity(unittest.TestCase):
    setUp=fixture.CommunicationFlow.setUp
    start=fixture.CommunicationFlow.start

    def test_control_service_can_start_offline_but_remote_use_still_requires_identity(self):
        with patch('homebrew_harness.platforms.github.communication.GithubCommunication.identity',side_effect=OSError('Unavailable platform')) as identify:
            service=self.start();service.pause('all')
            identify.assert_not_called()
            self.assertEqual(service.snapshot().state,'paused')
            runtime=service.runtime.maintenance.communication
            before=runtime.identity()
            subject=ObjectRef('github','primary','example/project','issue','1',1).data()
            with self.assertRaises(OSError):runtime.adapter(subject)
            self.assertEqual(runtime.identity(),before)
            self.assertEqual(self.peer.writes,[])
