"""A valid request arriving at completion remains a normal wait, not an unsupported blocker."""
import unittest
from homebrew_harness.runs import RunSpec
from tests.runs import test_human_control as fixture
from tests.support.clients import reply

class LateDecision(unittest.TestCase):
    setUp = fixture.ManagedRunFixture.setUp
    session = fixture.ManagedRunFixture.session
    close = fixture.ManagedRunFixture.close

    def test_new_request_at_completion_returns_waiting_without_repeating_the_model(self):
        control, session, client, _ = self.session([reply('Finished current analysis.')], spec=RunSpec('Analyze the supplied facts.'))
        original = control.pending_work
        requests = []
        def pending():
            if not requests:
                requests.append(control.requests.create(control, 'attention', question='Choose the next direction.', reason='A current direction is needed.'))
            return original()
        control.pending_work = pending
        result = control.execute()
        self.assertEqual(result.status, 'waiting')
        self.assertEqual(result.reason_code, 'attention_required')
        self.assertEqual(control.requests.get(control.id, requests[0]['id'])['state'], 'pending')
        self.assertEqual(client.chat.completions.create.call_count, 1)
