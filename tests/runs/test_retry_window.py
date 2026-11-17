"""Explicit retry windows preserve Run identity, checkpoints and completed effects."""

import copy
import time
import unittest

from homebrew_harness.execution.model_protocol import ProviderError
from homebrew_harness.runs import RunSpec
from tests.runs import test_controller as fixture
from tests.support.clients import reply


class RetryWindowTests(unittest.TestCase):
    setUp = fixture.RunTests.setUp
    controller = fixture.RunTests.controller
    write = fixture.RunTests.write

    def stopped(self):
        control, session, client, _ = self.controller([], RunSpec("Write once then finish", total_seconds=60))
        values = iter([self.write("written", "result.txt"), ProviderError("invalid_request", code="invalid_parameter", message="Request parameter needs correction.")])
        def respond(**kwargs):
            value = next(values)
            if isinstance(value, Exception):
                raise value
            return value
        client.chat.completions.create.side_effect = respond
        self.assertEqual(control.execute().status, "blocked")
        session.close()
        self.assertTrue(session.wait_closed(5))
        control.close_heartbeat()
        self.store.ended(control.header, cleanup=True)
        return control

    def test_explicit_retry_renews_expired_window_without_replaying_write(self):
        control = self.stopped()
        old = self.store.update(control.header, {"deadline_at": time.time() - 2})
        checkpoint = copy.deepcopy(self.store.runtime.get("checkpoints", old["checkpoint"]))
        renewed = self.store.retry(control.id, seconds=30, request_id="one")
        self.assertGreater(renewed["deadline_at"], time.time())
        self.assertEqual(renewed["retry_windows"][0]["previous_deadline_at"], old["deadline_at"])
        self.assertEqual(renewed["session_id"], old["session_id"])
        self.assertEqual(self.store.runtime.get("checkpoints", old["checkpoint"]), checkpoint)
        next_control, session, client, _ = self.controller([reply("done")], identity=control.id)
        self.assertEqual(next_control.execute().status, "completed")
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertEqual((self.root / "result.txt").read_text(), "ok")
        self.assertEqual(sum(c["function"]["name"] == "write_file" for m in session.messages for c in m.get("tool_calls", [])), 1)

    def test_retry_is_idempotent_and_rejects_conflicting_request_id(self):
        control = self.stopped()
        first = self.store.retry(control.id, seconds=30, request_id="same")
        self.assertEqual(self.store.retry(control.id, seconds=30, request_id="same"), first)
        with self.assertRaisesRegex(ValueError, "another duration"):
            self.store.retry(control.id, seconds=20, request_id="same")

    def test_retry_rejects_bad_windows_and_preserves_state(self):
        control = self.stopped()
        before = self.store.get(control.id)
        for value in (True, 0, -1, 61, float("inf"), float("nan"), "30"):
            with self.subTest(seconds=value), self.assertRaises(ValueError):
                self.store.retry(control.id, seconds=value)
            self.assertEqual(self.store.get(control.id), before)

    def test_retry_requires_completed_cleanup(self):
        control = self.stopped()
        self.store.update(control.header, {"cleanup_complete": False})
        with self.assertRaisesRegex(ValueError, "cleanup"):
            self.store.retry(control.id, seconds=30)

    def test_retry_is_capped_by_authority_expiry_and_replay_stays_idempotent(self):
        control = self.stopped()
        expiry = time.time() + 15
        first = self.store.retry(control.id, seconds=30, expires_at=expiry, request_id="capped")
        self.assertEqual(first["deadline_at"], expiry)
        self.assertLessEqual(first["retry_windows"][0]["seconds"], 15)
        self.assertEqual(self.store.retry(control.id, seconds=30, expires_at=expiry, request_id="capped"), first)

    def test_expired_authority_does_not_mutate_run(self):
        control = self.stopped()
        before = self.store.get(control.id)
        with self.assertRaisesRegex(ValueError, "expired"):
            self.store.retry(control.id, seconds=30, expires_at=time.time() - 1)
        self.assertEqual(self.store.get(control.id), before)

    def test_ordinary_resume_still_preserves_expired_deadline(self):
        control = self.stopped()
        before = self.store.update(control.header, {"deadline_at": time.time() - 2})
        resumed = self.store.control(control.id, "run")
        self.assertEqual(resumed["deadline_at"], before["deadline_at"])
        self.assertFalse(resumed.get("retry_windows"))


if __name__ == "__main__":
    unittest.main()
