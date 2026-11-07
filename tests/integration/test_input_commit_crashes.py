"""Actual process exits around the input/checkpoint/delivery commit."""

import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from homebrew_harness import HarnessSession, Capabilities
from homebrew_harness.execution.model_protocol import ProviderError
from tests.support.clients import fake_client

SCRIPT = r"""
import os,sys
from pathlib import Path
from unittest.mock import patch
from homebrew_harness import HarnessSession,Capabilities
from homebrew_harness.orchestration.teams import TeammateRuntime
from homebrew_harness.storage.durable_runtime import checkpoint_scope
from tests.support.clients import fake_client
client,_=fake_client([])
s=HarnessSession(client,'fixture',Path(sys.argv[1]),capabilities=Capabilities({'teams'}))
s.run_user('/usage')
s.team.members['reader']=TeammateRuntime(s.team,'reader','reader','Read',False)
s.inputs.submit('Keep this revision',target='reader')
db=s.runtime_store.backend()
original_send=db._send
original_checkpoint=db.checkpoint
def send(conn,target,record):
 if record['type']=='user_steer' and sys.argv[2]=='before':os._exit(77)
 return original_send(conn,target,record)
def checkpoint(*args,**kwargs):
 result=original_checkpoint(*args,**kwargs)
 if (kwargs.get('boundary') or {}).get('deliveries'):os._exit(78)
 return result
with patch.object(db,'_send',send),patch.object(db,'checkpoint',checkpoint),s.inputs.turn(),checkpoint_scope(s._checkpoint_writer):
 s.inputs.apply(s.messages,s.todo,s.compactor)
"""


class InputCommitCrashes(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        for cap in (redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            cap.__enter__()
            self.addCleanup(cap.__exit__, None, None, None)

    def session(self, **kwargs):
        client, _ = fake_client([])
        session = HarnessSession(
            client, "fixture", self.root, shutdown_timeout=0.02, **kwargs
        )
        self.addCleanup(lambda: self.assertTrue(session.wait_closed(5)))
        self.addCleanup(session.close)
        return session, client

    def test_crash_before_commit_retains_input_without_partial_delivery(self):
        child = subprocess.run(
            [sys.executable, "-B", "-c", SCRIPT, str(self.root), "before"],
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(child.returncode, 77, child.stderr.decode())
        session, client = self.session(
            capabilities=Capabilities({"teams"}), resume_session="latest"
        )
        session.run_user("/recovery")
        self.assertEqual(len(session.inputs.pending()), 1)
        self.assertFalse(session.inputs.applied)
        self.assertFalse(session.team.bus.peek("reader"))
        client.chat.completions.create.assert_not_called()

    def test_crash_after_commit_retains_history_ack_and_single_target_delivery(self):
        child = subprocess.run(
            [sys.executable, "-B", "-c", SCRIPT, str(self.root), "after"],
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(child.returncode, 78, child.stderr.decode())
        session, client = self.session(
            capabilities=Capabilities({"teams"}), resume_session="latest"
        )
        session.run_user("/recovery")
        self.assertFalse(session.inputs.pending())
        self.assertEqual(len(session.inputs.applied), 1)
        self.assertEqual(
            sum(
                "Keep this revision" in str(m.get("content", ""))
                for m in session.messages
            ),
            1,
        )
        records = session.team.bus.claim("reader")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["content"], "Keep this revision")
        session.team.bus.acknowledge("reader", records)
        client.chat.completions.create.assert_not_called()

    def test_automatic_input_retry_is_bounded_and_explicit_retry_gets_new_identity(
        self,
    ):
        session, client = self.session()
        session.run_user("/usage")
        with patch.object(session, "start"):
            receipt = session.submit_user_input("Work", mode="queued")
        with patch.object(
            session, "run_user", side_effect=ProviderError("transport")
        ) as run:
            for attempt in range(3):
                self.assertTrue(session.process_once())
                value = session.runtime_store.get("input_receipts", receipt["input_id"])
                if attempt < 2:
                    self.assertEqual(value["state"], "retry_wait")
                    self.assertFalse(session.process_once())
                    session.runtime_store.update_fields(
                        "input_receipts", receipt["input_id"], {"next_retry": 0}
                    )
            self.assertEqual(run.call_count, 3)
        self.assertEqual(value["state"], "failed")
        self.assertFalse(session.inputs.pending())
        with patch.object(session, "start"):
            retry = session.inputs.manage("retry", receipt["input_id"])
        self.assertNotEqual(retry["input_id"], receipt["input_id"])
        client.chat.completions.create.assert_not_called()
