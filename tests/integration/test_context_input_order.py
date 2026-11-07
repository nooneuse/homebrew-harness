"""Accepted steering must participate in preparation before the next request."""

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from homebrew_harness import HarnessSession
from homebrew_harness.context.compaction import CompactionError
from tests.support.clients import fake_client


class ContextInputOrderTests(unittest.TestCase):
    def test_large_pending_steering_is_checked_before_any_model_request(self):
        with tempfile.TemporaryDirectory() as directory:
            client, _ = fake_client([])
            session = HarnessSession(
                client,
                "fixture",
                Path(directory).resolve(),
                context_chars=50000,
                shutdown_timeout=0.02,
            )
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                try:
                    session.run_user("/usage")
                    receipt = session.inputs.submit(
                        "Keep this requirement: " + "x" * 60000
                    )
                    with self.assertRaises(CompactionError):
                        session.run_user("Summarize the requirements")
                    client.chat.completions.create.assert_not_called()
                    self.assertFalse(session.inputs.pending())
                    self.assertEqual(
                        session.inputs.applied[-1]["input_id"], receipt["input_id"]
                    )
                finally:
                    session.close()
                    self.assertTrue(session.wait_closed(5))
