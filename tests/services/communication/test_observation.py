"""Whole observations and native review-comment ownership preserve source facts."""

import unittest

from homebrew_harness.platforms.contracts import ObjectRef, SourceEvent
from tests.runtime.test_interaction import eventually
from tests.services.communication import test_boundaries as fixture


class ObservationTests(unittest.TestCase):
    setUp = fixture.CommunicationBoundaries.setUp
    start = fixture.CommunicationBoundaries.start
    discussion = fixture.CommunicationBoundaries.discussion
    managed = fixture.CommunicationBoundaries.managed

    def test_complete_observation_records_deletion_once_and_keeps_original(self):
        service, did, communication = self.managed()
        store = communication.store
        self.peer.answer("The old constraint.")
        communication.poll(store.get("discussions", did))
        before = store.get("discussions", did)["input_revision"]
        self.peer.comments.clear()
        communication.poll(store.get("discussions", did))
        self.assertEqual(store.get("discussions", did)["input_revision"], before + 1)
        events = [
            store.ms.get("events", e["id"])
            for e in store.event_page(did, limit=100)["items"]
        ]
        self.assertTrue(
            any(e["content"]["body"] == "The old constraint." for e in events)
        )
        self.assertTrue(any(e["content"].get("state") == "deleted" for e in events))
        communication.poll(store.get("discussions", did))
        self.assertEqual(store.get("discussions", did)["input_revision"], before + 1)
        self.assertEqual(
            store.observed_events(
                service.discussion(did)["subject"], [], complete=False
            ),
            [],
        )

    def test_inline_feedback_echo_is_bound_to_native_review_and_not_manual_reply(self):
        self.peer.issue(pr=True)
        self.peer.proposals = [
            {
                "next_step": "wait",
                "reason": "Needs explanation.",
                "alignment": "Within scope.",
                "communication": {
                    "kind": "submit_review",
                    "event": "COMMENT",
                    "intent": "review_feedback",
                    "body": "Review feedback.",
                    "comments": [
                        {
                            "path": "src/value.py",
                            "line": 2,
                            "side": "RIGHT",
                            "body": "Please explain.",
                        }
                    ],
                },
            }
        ]
        service = self.start()
        did = self.discussion(service)
        request = eventually(
            lambda: next(
                (r for r in service.requests() if r.kind == "communication_authority"),
                None,
            ),
            20,
        )
        service.decide(request, "approve")
        action = eventually(
            lambda: next(
                (
                    a
                    for a in service.communication_actions()["items"]
                    if a["state"] == "confirmed"
                ),
                None,
            ),
            15,
        )
        communication = service.runtime.maintenance.communication
        store = communication.store
        service.pause("all")
        communication.poll(store.get("discussions", did))
        row = store.get("discussions", did)
        inline = self.peer.inline[0]
        events = store.event_page(did, limit=100)["items"]
        own = next(
            e
            for e in events
            if e["object"]["id"] == "review_comment:" + str(inline["id"])
        )
        self.assertEqual(own["echo"], "echo")
        before = row["input_revision"]
        self.peer.inline.append(
            {**inline, "id": inline["id"] + 100, "in_reply_to_id": inline["id"]}
        )
        communication.poll(row)
        self.assertEqual(store.get("discussions", did)["input_revision"], before + 1)
        self.assertEqual(service.communication(action["id"])["state"], "confirmed")

    def test_provisional_inline_comment_is_verified_by_review_receipt(self):
        service, did, communication = self.managed()
        store = communication.store
        subject = service.discussion(did)["subject"]
        item = SourceEvent(
            ObjectRef(
                "github", "primary", "example/project", "comment", "review_comment:5"
            ),
            ObjectRef(**subject),
            "v5",
            {
                "body": "Inline body",
                "actor_id": 7,
                "metadata": {
                    "channel": "inline",
                    "review_id": "8",
                    "reply_to": None,
                    "path": "src/value.py",
                    "side": "RIGHT",
                    "line": 2,
                    "commit": "1" * 40,
                },
            },
        )
        action = {
            "id": "communication_inline",
            "state": "unknown",
            "plan": {
                "kind": "submit_review",
                "subject": subject,
                "review_basis": {"head": "1" * 40},
                "comments": [
                    {
                        "body": "Inline body",
                        "path": "src/value.py",
                        "side": "RIGHT",
                        "line": 2,
                    }
                ],
            },
        }
        with store.db.write() as conn:
            store.put(conn, "communication_actions", action)
            self.assertEqual(store.echo(conn, item), "pending_echo")
        store.ingest("project", [item], item.subject.scope())
        before = store.get("discussions", did)["input_revision"]
        with store.db.write() as conn:
            action.update(
                state="confirmed", result={"confirmed": True, "id": "9", "actor_id": 7}
            )
            store.put(conn, "communication_actions", action)
            store.resolve_echoes(conn, action)
        self.assertEqual(store.get("discussions", did)["input_revision"], before + 1)
