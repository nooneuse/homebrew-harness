"""Retirement follows actual current Session completion, not synthetic old states."""

import io
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from homebrew_harness import Capabilities, HarnessSession
from homebrew_harness.runtime.runtime_state import RecoveryRequired
from homebrew_harness.storage.checkpoint_retirement import preview, retire, unretire
from homebrew_harness.tools.tool_outcome import ToolExecutionError
from tests.support.clients import call, fake_client, reply


class CurrentRetirementTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        for capture in (redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            capture.__enter__()
            self.addCleanup(capture.__exit__, None, None, None)

    def run_session(self, responses, capabilities=None):
        client, _ = fake_client(responses)
        session = HarnessSession(
            client, "fixture", self.root, capabilities=capabilities
        )
        self.addCleanup(lambda: session.wait_closed(5))
        self.addCleanup(session.close)
        return session

    def retirement_roundtrip(self, session):
        session.close()
        self.assertTrue(session.wait_closed(5))
        store = session.runtime_store
        sid = session.session_id
        store.set_meta("latest_session", "newer-fixture")
        now = time.time() + 30 * 86400
        plan = preview(self.root, selected=[sid], now=now)
        self.assertEqual(plan["sessions"][0]["reasons"], [])
        before = store.get("checkpoints", "lead:" + sid)
        retire(self.root, plan, now=now)
        self.assertEqual(store.get("session_meta", sid)["lifecycle"], "retired")
        unretire(self.root, sid, now=now + 1)
        self.assertEqual(store.get("checkpoints", "lead:" + sid), before)

    def test_default_details_disabled_session_can_retire_and_restore(self):
        s = self.run_session([reply("done")])
        s.run_user("Complete fixture")
        self.assertFalse(s.gateway.summary()["details_enabled"])
        self.retirement_roundtrip(s)

    def test_successful_subagent_is_settled_for_retirement(self):
        s = self.run_session(
            [
                reply(
                    calls=[
                        call(
                            "child",
                            name="task",
                            arguments='{"prompt":"Complete subtask"}',
                        )
                    ]
                ),
                reply("child done"),
                reply("done"),
            ],
            Capabilities({"subagents"}, details=True),
        )
        s.run_user("Complete fixture")
        self.retirement_roundtrip(s)

    def test_inspected_effect_is_settled_for_retirement(self):
        import homebrew_harness.tools.registry as registry

        original = registry.TOOL_HANDLERS["write_file"]

        def uncertain(**kwargs):
            original(**kwargs)
            raise ToolExecutionError(
                "fixture effect", outcome="unknown", details={"path": "fixture"}
            )

        s = self.run_session(
            [
                reply(
                    calls=[
                        call(
                            "write",
                            name="write_file",
                            arguments='{"path":"fixture","content":"x"}',
                        )
                    ]
                )
            ]
        )
        with (
            patch.dict(registry.TOOL_HANDLERS, {"write_file": uncertain}),
            self.assertRaises(RecoveryRequired),
        ):
            s.run_user("Write fixture")
        row = s.recovery_operations()[0]
        s.inspect_operation(row["operation_id"], row["revision"])
        self.retirement_roundtrip(s)

    def test_corrupt_optional_details_do_not_override_complete_execution(self):
        s = self.run_session([reply("done")], Capabilities(details=True))
        s.run_user("Complete fixture")
        s.gateway._state.ledger.flush(2)
        reference = s.gateway.summary()["ledger"]
        (self.root / reference).write_text("{broken optional observation")
        self.retirement_roundtrip(s)
