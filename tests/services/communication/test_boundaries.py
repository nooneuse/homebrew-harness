"""Version, source, policy and execution boundaries of multi-round communication."""

import time
from unittest.mock import patch

from homebrew_harness.platforms.contracts import ObjectRef, SourceEvent, SourcePage
from homebrew_harness.services.communication.spec import validate_plan
from tests.services.communication import test_flow as fixture
import unittest
from tests.runtime.test_interaction import eventually


class CommunicationBoundaries(unittest.TestCase):
    setUp = fixture.CommunicationFlow.setUp
    start = fixture.CommunicationFlow.start
    discussion = fixture.CommunicationFlow.discussion

    # Keep these tests focused on the record/controller boundary.
    def managed(self):
        self.peer.issue()
        service = self.start()
        did = self.discussion(service)
        eventually(lambda: service.discussion(did)["processed_seq"] > 0, 10)
        service.pause("all")
        communication = service.runtime.maintenance.communication
        communication.stop.set()
        self.assertTrue(communication.finished.wait(5))
        communication.stop.clear()
        return service, did, communication

    def source(self, store, subject, identity, body, actor=11):
        item = SourceEvent(
            ObjectRef("github", "primary", "example/project", "comment", str(identity)),
            ObjectRef(**subject),
            str(identity),
            {
                "title": "",
                "body": body,
                "actor_id": actor,
                "state": None,
                "metadata": {"api_object_id": identity},
            },
        )
        store.ingest("project", [item], item.subject.scope())
        return item

    def test_source_cursor_and_events_commit_together(self):
        service, did, communication = self.managed()
        store = communication.store
        ref = ObjectRef(**service.discussion(did)["subject"])
        item = SourceEvent(
            ObjectRef("github", "primary", "example/project", "comment", "100"),
            ref,
            "v100",
            {"body": "New fact", "title": "", "state": None, "actor_id": 11},
        )
        page = SourcePage((item,), {"position": 100}, time.time() + 43200, "ready")
        before = service.discussion(did)["seq"]
        with patch.object(
            store.ms, "changed", side_effect=RuntimeError("storage fault")
        ):
            with self.assertRaises(RuntimeError):
                store.ingest("project", [item], ref.scope(), page=page)
        self.assertEqual(service.discussion(did)["seq"], before)
        with store.db.connection() as conn:
            self.assertIsNone(
                store.db._get(conn, "service_events", store.event_identity(item))
            )
        store.ingest("project", [item], ref.scope(), page=page)
        self.assertEqual(service.discussion(did)["seq"], before + 1)
        self.assertEqual(
            store.ms.get("sources", service.id + ":project")["cursor"],
            {"position": 100},
        )

    def test_own_marker_from_another_author_is_not_an_echo(self):
        service, did, communication = self.managed()
        store = communication.store
        with store.db.write() as conn:
            row = store.get("discussions", did, conn)
            action = store.plan(
                conn,
                row,
                {"kind": "post_comment", "intent": "clarify", "body": "Question?"},
            )
            action.update(
                state="unknown",
                authorization="operator:test",
                attempts=1,
                generation=service.runtime.header["generation"],
            )
            store.put(conn, "communication_actions", action)
        body = "Question?\n\n<!-- harness-communication:" + action["id"] + " -->"
        before = service.discussion(did)["input_revision"]
        item = self.source(
            store, service.discussion(did)["subject"], 101, body, actor=99
        )
        self.assertEqual(service.discussion(did)["input_revision"], before + 1)
        with store.db.connection() as conn:
            self.assertIsNone(
                store.db._get(conn, "service_events", store.event_identity(item))[
                    "echo"
                ]
            )

    def test_same_publisher_manual_message_is_not_suppressed(self):
        service, did, communication = self.managed()
        before = service.discussion(did)["input_revision"]
        self.source(
            communication.store,
            service.discussion(did)["subject"],
            102,
            "A manual operator comment.",
            actor=7,
        )
        self.assertEqual(service.discussion(did)["input_revision"], before + 1)

    def test_long_discussion_uses_bounded_windows(self):
        service, did, communication = self.managed()
        store = communication.store
        subject = service.discussion(did)["subject"]
        for identity in range(200, 315):
            self.source(store, subject, identity, "Fact " + str(identity))
        row = store.get("discussions", did)
        # Resume the discussion's analysis directly, without a racing model worker.
        with store.db.write() as conn:
            row = store.get("discussions", did, conn)
            row.update(state="ready_to_assess", ready_after=0)
            store.put(conn, "discussions", row)
        job = store.queue_round(row, force=True)
        self.assertEqual(len(job["input"]["events"]), 100)
        with store.db.write() as conn:
            store.finish(
                conn,
                job,
                {
                    "next_step": "wait",
                    "reason": "Process this window.",
                    "alignment": "Within scope.",
                },
            )
        current = store.get("discussions", did)
        self.assertLess(current["processed_seq"], current["seq"])
        self.assertEqual(current["state"], "ready_to_assess")
        with store.db.write() as conn:
            queued = store.db._get(conn, "service_jobs_model", job["id"])
            queued["state"] = "completed"
            store.db._put(conn, "service_jobs_model", job["id"], queued)
        second = store.queue_round(current, force=True)
        self.assertEqual(len(second["input"]["events"]), 15)

    def test_late_round_never_publishes_an_answered_question(self):
        service, did, communication = self.managed()
        store = communication.store
        self.source(store, service.discussion(did)["subject"], 103, "New question.")
        with store.db.write() as conn:
            row = store.get("discussions", did, conn)
            row.update(state="ready_to_assess", ready_after=0)
            store.put(conn, "discussions", row)
        job = store.queue_round(row, force=True)
        self.source(store, row["subject"], 104, "The answer has arrived.")
        with store.db.write() as conn:
            store.finish(
                conn,
                job,
                {
                    "next_step": "clarify",
                    "reason": "An old question.",
                    "alignment": "Within scope.",
                    "communication": {
                        "kind": "post_comment",
                        "intent": "clarify",
                        "body": "Please answer.",
                    },
                },
            )
        self.assertFalse(store.page("communication_actions")["items"])
        self.assertEqual(
            store.get("discussions", did)["processed_seq"], job["input"]["seq"]
        )
        self.assertEqual(store.get("discussions", did)["state"], "ready_to_assess")

    def test_formal_review_intent_cannot_masquerade_as_clarify(self):
        with self.assertRaises(ValueError):
            validate_plan(
                {
                    "kind": "submit_review",
                    "intent": "clarify",
                    "event": "APPROVE",
                    "body": "Approve.",
                }
            )

    def test_disabled_action_and_unknown_target_are_rejected(self):
        service, did, communication = self.managed()
        with self.assertRaises(ValueError):
            communication.adapter(
                {
                    "provider": "github",
                    "instance": "other",
                    "project": "example/project",
                    "kind": "issue",
                    "id": "1",
                    "number": 1,
                }
            )
        with self.assertRaises(ValueError):
            communication.store.command(
                "communication_plan",
                {
                    "id": did,
                    "revision": 0,
                    "plan": {"kind": "unknown", "intent": "clarify", "body": "x"},
                },
                "bad-plan",
            )
