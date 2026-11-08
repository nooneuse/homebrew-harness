import unittest
from unittest.mock import patch
from tests.runs import test_controller as fixture
from tests.support.clients import reply
from homebrew_harness.runs import RunSpec
from homebrew_harness.execution.model_protocol import ProviderError


def spec():
    goal = {
        "condition": "Output exists",
        "judge": False,
        "predicates": [{"id": "out", "type": "file_exists", "path": "out.txt"}],
    }
    return RunSpec(
        "Create out.txt",
        goal=goal,
        stages=({"key": "work", "subject": "Work", "acceptance": goal},),
    )


class LifecycleTests(unittest.TestCase):
    setUp = fixture.RunTests.setUp
    controller = fixture.RunTests.controller

    def finish(self, c, s):
        s.close()
        self.assertTrue(s.wait_closed(5))
        c.close_heartbeat()
        self.store.ended(c.header, cleanup=True)

    def test_cancel_after_claim_then_start_new_run(self):
        c, s, *_ = self.controller([], spec())
        c.prepare()
        old = s.workspace.store.list()[0]
        s.workspace.store.claim(old.id)
        self.store.control(c.id, "cancel")
        self.finish(c, s)
        self.assertTrue(self.store.finalize_terminal(c.id))
        self.assertEqual(s.workspace.store.load(old.id).status, "abandoned")
        newer, next_session, *_ = self.controller([], spec())
        newer.prepare()
        task = next(
            t
            for t in next_session.workspace.store.list()
            if t.stage["run_id"] == newer.id
        )
        self.assertEqual(
            next_session.workspace.store.claim(task.id).status, "in_progress"
        )
        self.assertEqual(
            self.store.get(c.id)["finalization"]["tasks"][0]["owner"], "agent"
        )

    def test_finalize_cannot_release_a_live_session_or_unknown_effect(self):
        c, s, *_ = self.controller([], spec())
        c.prepare()
        self.store.control(c.id, "cancel")
        self.assertFalse(self.store.finalize_terminal(c.id))
        self.finish(c, s)
        self.store.runtime.put(
            "operations",
            "unknown",
            {"checkpoint": s.checkpoint_key, "state": "unknown"},
        )
        self.assertFalse(self.store.finalize_terminal(c.id))
        self.assertIsNone(self.store.get(c.id)["finalized_at"])
        self.store.runtime.remove("operations", "unknown")
        self.assertTrue(self.store.finalize_terminal(c.id))

    def test_finalization_rolls_back_and_repeats_without_forged_completion(self):
        c, s, *_ = self.controller([], spec())
        c.prepare()
        task = s.workspace.store.list()[0]
        s.workspace.store.claim(task.id)
        self.store.control(c.id, "cancel")
        self.finish(c, s)
        db = self.store.runtime.backend()
        original = db._put

        def fail(conn, section, key, value):
            if section == "runs" and value.get("finalized_at"):
                raise OSError("commit failed")
            return original(conn, section, key, value)

        with patch.object(db, "_put", side_effect=fail), self.assertRaises(OSError):
            self.store.finalize_terminal(c.id)
        self.assertEqual(s.workspace.store.load(task.id).status, "in_progress")
        self.assertTrue(self.store.finalize_terminal(c.id))
        audit = self.store.get(c.id)["finalization"]
        self.assertTrue(self.store.finalize_terminal(c.id))
        self.assertEqual(self.store.get(c.id)["finalization"], audit)
        self.assertEqual(s.workspace.store.load(task.id).status, "abandoned")

    def test_explicit_resume_unblocks_quota_without_resetting_usage(self):
        c, s, client, *_ = self.controller([])
        client.chat.completions.create.side_effect = ProviderError(
            "quota", status_code=429
        )
        self.assertEqual(c.execute().status, "blocked")
        self.finish(c, s)
        before = self.store.get(c.id)
        usage = s.gateway.summary()
        self.store.control(c.id, "run")
        pending = self.store.get(c.id)["resume_request"]["id"]
        fresh, next_session, next_client, *_ = self.controller(
            [reply("Recovered")], identity=c.id
        )
        result = fresh.execute()
        self.assertEqual(result.status, "completed", result)
        self.assertEqual(next_client.chat.completions.create.call_count, 1)
        self.assertEqual(self.store.get(c.id)["resume_applied"], pending)
        self.assertIsNone(self.store.get(c.id)["resume_request"])
        self.assertEqual(self.store.get(c.id)["deadline_at"], before["deadline_at"])
        self.assertGreaterEqual(next_session.gateway.summary()["calls"], usage["calls"])

    def test_explicit_resume_ack_is_not_reused_for_a_new_failure(self):
        c, s, client, *_ = self.controller([])
        client.chat.completions.create.side_effect = ProviderError(
            "quota", status_code=429
        )
        c.execute()
        self.finish(c, s)
        self.store.control(c.id, "run")
        fresh, next_session, next_client, *_ = self.controller([], identity=c.id)
        next_client.chat.completions.create.side_effect = ProviderError(
            "quota", status_code=429
        )
        self.assertEqual(fresh.execute().status, "blocked")
        self.assertEqual(next_client.chat.completions.create.call_count, 1)
        self.assertIsNone(self.store.get(c.id)["resume_request"])
        self.assertTrue(next_session.gateway.blocked)

    def test_terminal_finalization_preserves_unapplied_input_and_foreign_mail(self):
        from homebrew_harness.storage.message_bus import MessageBus
        from homebrew_harness.runs.maintenance import export_run

        c, s, *_ = self.controller([], spec())
        c.prepare()
        incoming = self.store.submit(c.id, "An accepted clarification")
        bus = MessageBus(self.root)
        bus.run_id = c.id
        own = bus.send("lead", "worker", "old run instruction")
        foreign = MessageBus(self.root).send(
            "lead", "worker", "unrelated pending message"
        )
        self.store.control(c.id, "cancel")
        self.finish(c, s)
        self.assertTrue(self.store.finalize_terminal(c.id))
        self.assertFalse(self.store.runtime.mailbox("peek", "user_" + c.id))
        pending = self.store.runtime.mailbox("inspect", "worker")
        self.assertEqual([r["id"] for r in pending], [foreign])
        exported = export_run(self.store, c.id)
        receipts = exported["records"]["input_receipts"]
        self.assertEqual(receipts[incoming["input_id"]]["state"], "cancelled")
        self.assertEqual(receipts[own]["record"]["content"], "old run instruction")

    def test_lost_finalization_return_is_confirmed_by_transaction_identity(self):
        from contextlib import contextmanager

        c, s, *_ = self.controller([], spec())
        c.prepare()
        self.store.control(c.id, "cancel")
        self.finish(c, s)
        db = self.store.runtime.backend()
        original = db.write

        @contextmanager
        def lost():
            with original() as conn:
                yield conn
            raise OSError("result lost after COMMIT")

        with patch.object(db, "write", lost):
            self.assertTrue(self.store.finalize_terminal(c.id))
        self.assertIsNotNone(self.store.get(c.id)["finalized_at"])

    def test_pause_during_resume_confirmation_restores_fault_and_sends_no_request(self):
        c, s, client, *_ = self.controller([])
        client.chat.completions.create.side_effect = ProviderError(
            "quota", status_code=429
        )
        c.execute()
        self.finish(c, s)
        self.store.control(c.id, "run")
        fresh, next_session, next_client, *_ = self.controller(
            [reply("Not sent")], identity=c.id
        )
        original = next_session.gateway.acknowledge_faults

        def pause(expected):
            original(expected)
            self.store.control(c.id, "pause")

        with patch.object(
            next_session.gateway, "acknowledge_faults", side_effect=pause
        ):
            outcome = fresh.execute()
        self.assertEqual(outcome.status, "paused")
        self.assertEqual(next_client.chat.completions.create.call_count, 0)
        self.assertTrue(next_session.gateway.blocked)
