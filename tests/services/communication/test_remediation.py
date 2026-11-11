"""Accepted constraints, current observations and long-lived controls advance safely."""

import copy
import json
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from homebrew_harness.contracts import SDKError
from homebrew_harness.platforms.contracts import ObjectRef, SourceEvent
from homebrew_harness.services.requirements import install
from homebrew_harness.platforms.github.review_coverage import coverage
from tests.runtime.test_interaction import eventually
from tests.services.communication import test_boundaries as boundaries
from tests.services.communication import test_flow as flow


class Remediation(unittest.TestCase):
    setUp = flow.CommunicationFlow.setUp
    start = flow.CommunicationFlow.start
    discussion = flow.CommunicationFlow.discussion
    managed = boundaries.CommunicationBoundaries.managed

    def pr(self, **options):
        self.peer.issue(pr=True)
        service = self.start(**options)
        did = self.discussion(service)
        eventually(
            lambda: service.discussion(did)["state"] == "waiting_participant", 20
        )
        service.pause("all")
        c = service.runtime.maintenance.communication
        c.stop.set()
        self.assertTrue(c.finished.wait(5))
        c.stop.clear()
        return service, did, c

    def test_accepted_exclusions_and_source_are_in_run_input(self):
        service, did, c = self.managed()
        row = c.store.get("discussions", did)
        draft = {
            "text": "Implement local export.",
            "scope": "code",
            "acceptance": "Round trip the export.",
            "out_of_scope": "Do not add upload or network synchronization.",
        }
        with c.ms.db.write() as conn:
            row.update(
                reason="Scope accepted.", operator_note="Preserve the existing format."
            )
            work = c.store.accept_requirements(
                conn, row, draft, "feature", automatic=True
            )
            c.store.put(conn, "discussions", row)
        actual = service.runtime.store.work(work["id"], service.id)
        self.assertIn(draft["out_of_scope"], actual["run_spec"]["prompt"])
        self.assertIn(row["operator_note"], actual["run_spec"]["prompt"])
        record = c.ms.get("requirements", actual["requirements_id"])
        self.assertTrue(record["sources"])
        self.assertEqual(record["draft"], draft)
        install(c.ms, actual, self.project)
        prefix = self.project / ".harness/service-inputs" / record["id"]
        self.assertTrue(
            (prefix / "sources" / (record["sources"][0] + ".json")).is_file()
        )
        self.assertEqual(
            json.loads((prefix / "requirements.json").read_text())["draft"], draft
        )
        with self.assertRaises(ValueError):
            (prefix / "requirements.json").chmod(0o600)
            (prefix / "requirements.json").write_text("changed")
            install(c.ms, actual, self.project)

    def test_return_to_old_head_reuses_snapshot_and_updates_current_basis(self):
        service, did, c = self.pr()
        original = self.peer.head
        old_snapshot = service.discussion(did)["review_id"]
        self.peer.head = "a" * 40
        c.poll(c.store.get("discussions", did))
        self.assertEqual(
            service.discussion(did)["observed_basis"]["head"], self.peer.head
        )
        self.peer.head = original
        c.poll(c.store.get("discussions", did))
        self.assertEqual(service.discussion(did)["observed_basis"]["head"], original)
        result = c.prepare_review(c.store.get("discussions", did))
        self.assertEqual(result["snapshot_id"], old_snapshot)
        before = service.discussion(did)["input_revision"]
        c.poll(c.store.get("discussions", did))
        self.assertEqual(service.discussion(did)["input_revision"], before)

    def test_late_preparation_cannot_restore_paused_discussion(self):
        service, did, c = self.pr()
        captured = c.store.get("discussions", did)
        adapter = c.adapter(captured["subject"])
        original = adapter.snapshot

        def late(subject, policy):
            result = original(subject, policy)
            service.control_discussion(did, "pause")
            return result

        with patch.object(adapter, "snapshot", side_effect=late):
            result = c.prepare_review(captured)
        self.assertEqual(result["state"], "superseded")
        self.assertEqual(service.discussion(did)["state"], "paused")

    def test_changed_side_coverage_excludes_unmodified_context_and_missing_deletion(
        self,
    ):
        policy = {"review_globs": ["src/**"]}
        changed = [{"filename": "src/value.py", "status": "modified", "patch": "@@"}]
        valid = coverage(
            changed,
            {"src/value.py": "new"},
            {"src/value.py": "old"},
            [{"path": "README.md", "reason": "outside_review_scope"}],
            policy,
            6,
        )
        self.assertTrue(valid["complete"])
        deleted = coverage(
            [{"filename": "hidden/old.py", "status": "removed", "patch": "@@"}],
            {},
            {},
            [],
            policy,
            0,
        )
        self.assertFalse(deleted["complete"])
        renamed = coverage(
            [
                {
                    "filename": "src/new.py",
                    "previous_filename": "hidden/old.py",
                    "status": "renamed",
                    "patch": "@@",
                }
            ],
            {"src/new.py": "x"},
            {},
            [],
            policy,
            1,
        )
        self.assertFalse(renamed["complete"])
        added = coverage(
            [{"filename": "src/new.py", "status": "added", "patch": "@@"}],
            {"src/new.py": "x"},
            {},
            [],
            policy,
            1,
        )
        self.assertTrue(added["complete"])

    def test_explicit_same_head_preparation_creates_new_assessment(self):
        service, did, c = self.pr()
        epoch = service.discussion(did)["analysis_epoch"]
        operation = service.control_discussion(
            did, "prepare_review", request_id="review"
        )
        c.prepare_review(c.store.get("discussions", did))
        job = c.store.queue_round(c.store.get("discussions", did))
        self.assertIsNotNone(job)
        self.assertEqual(job["input"]["epoch"], epoch + 1)
        self.assertEqual(
            c.store.get("review_operations", operation["id"])["state"], "completed"
        )

    def test_snapshot_preserves_exec_bit_and_rejects_mode_changes(self):
        from homebrew_harness.services.communication.snapshots import (
            materialize,
            verify,
        )

        service, did, c = self.pr()
        captured = c.adapter(service.discussion(did)["subject"]).snapshot(
            service.discussion(did)["subject"], c.policy
        )
        captured["modes"]["src/value.py"] = "100755"
        snapshot = materialize(
            self.control, service.discussion(did)["subject"], captured, c.policy
        )
        path = Path(snapshot["workspace"]) / "src/value.py"
        self.assertTrue(path.stat().st_mode & 0o100)
        verify(snapshot, c.policy)
        path.chmod(0o400)
        with self.assertRaises(ValueError):
            verify(snapshot, c.policy)

    def test_noop_rows_and_expired_requests_do_not_hide_current_work(self):
        service, did, c = self.managed()
        original = c.store.get("discussions", did)
        with c.ms.db.write() as conn:
            for i in range(101):
                value = {
                    **copy.deepcopy(original),
                    "id": "discussion_fixture_" + str(i),
                    "state": "ready_to_assess",
                    "next_poll": time.time() + 3600,
                    "processed_seq": original["seq"],
                    "assessed_input_revision": original["input_revision"],
                    "retry_after": 0,
                    "ready_after": 0,
                }
                if i == 100:
                    value["input_revision"] += 1
                c.store.put(conn, "discussions", value)
                c.ms.put(
                    conn,
                    "decisions",
                    {
                        "id": "request_fixture_" + str(i),
                        "kind": "discussion_information",
                        "discussion_id": did,
                        "work_id": None,
                        "requirements_revision": 0,
                        "basis": c.store.basis(original),
                        "revision": 0,
                        "state": "pending",
                        "reason": "fixture",
                        "candidate": {},
                        "expires_at": time.time() + (-1 if i < 100 else 3600),
                    },
                )
        for row in c.rows("discussions"):
            c.store.queue_round(row)
        self.assertIn(
            "discussion_fixture_100", [r["id"] for r in c.rows("discussions")]
        )
        self.assertEqual(
            [r.ref.id for r in service.requests()], ["request_fixture_100"]
        )
        expired = service.requests_page(expired=True, limit=20)
        self.assertEqual(len(expired["items"]), 20)
        self.assertTrue(expired["next_cursor"])

    def test_direction_question_is_version_bound_and_answerable(self):
        service, did, c = self.managed()
        with c.ms.db.write() as conn:
            row = c.store.get("discussions", did, conn)
            row["state"] = "waiting_operator"
            c.store.put(conn, "discussions", row)
            request = c.store.information_request(conn, row)
        self.assertIsNone(request["expires_at"])
        visible = next(r for r in service.requests() if r.ref.id == request["id"])
        service.decide(visible, "answer", text="Keep offline behavior.")
        self.assertEqual(
            service.discussion(did)["operator_note"], "Keep offline behavior."
        )

    def test_public_job_reads_frozen_topic_and_rejects_unrelated_internal_work(self):
        service, did, c = self.managed()
        with c.ms.db.write() as conn:
            unrelated = service.runtime.store.enqueue(
                conn, service.id, "Internal operator planning notes.", "internal"
            )
        agent = service.runtime.maintenance.agent
        row = c.store.get("discussions", did)
        agent.job = {
            "id": "job_fixture",
            "kind": "discussion",
            "target": did,
            "input": {
                "subject": row["subject"],
                "seq": row["seq"],
                "work_id": None,
                "projection": {"facts": [{"id": "known", "text": "Frozen fact"}]},
            },
        }
        for kind, identity in (
            ("work_list", ""),
            ("work", unrelated["id"]),
            ("messages", "unknown"),
        ):
            with self.assertRaises(ValueError):
                agent.read(kind, identity)
        result = json.loads(agent.read("discussions", did))
        self.assertIn("Frozen fact", result["text"])
        self.assertNotIn("Private synthetic", result["text"])

    def test_newer_source_edit_advances_observation_and_old_page_does_not_undo_it(self):
        service, did, c = self.managed()
        subject = ObjectRef(**service.discussion(did)["subject"])
        object_ref = ObjectRef(
            "github", "primary", "example/project", "comment", "fixture"
        )

        def event(version, body):
            return SourceEvent(
                object_ref,
                subject,
                version,
                {"title": "", "body": body, "state": None, "actor_id": 11},
            )

        old = event("2026-11-11T00:00:00Z", "Old fact.")
        new = event("2026-11-11T00:00:01Z", "New fact.")
        c.store.ingest("communication", [old], subject.scope(), observed=True)
        c.store.ingest("project", [new], subject.scope())
        before = service.discussion(did)["input_revision"]
        c.store.ingest("project", [old], subject.scope())
        self.assertEqual(service.discussion(did)["input_revision"], before)
        self.assertEqual(
            c.store.history(c.store.get("discussions", did))[-1]["content"]["body"],
            "New fact.",
        )

    def test_complete_observation_cannot_delete_events_arriving_after_capture(self):
        service, did, c = self.managed()
        subject = ObjectRef(**service.discussion(did)["subject"])
        before = service.discussion(did)["seq"]
        event = SourceEvent(
            ObjectRef("github", "primary", "example/project", "comment", "late"),
            subject,
            "2026-11-11T00:00:01Z",
            {"title": "", "body": "Late fact", "state": None, "actor_id": 11},
        )
        c.store.ingest("project", [event], subject.scope())
        self.assertEqual(
            c.store.observed_events(subject.data(), [], complete=True, through=before),
            [],
        )


class Publication(unittest.TestCase):
    setUp = flow.CommunicationFlow.setUp
    start = flow.CommunicationFlow.start
    discussion = flow.CommunicationFlow.discussion

    def action(self):
        self.peer.issue()
        service = self.start()
        did = self.discussion(service)
        eventually(
            lambda: service.discussion(did)["state"] == "waiting_participant", 15
        )
        plan = service.plan_communication(
            did,
            {"kind": "post_comment", "intent": "clarify", "body": "Fixture question."},
        )
        request = next(
            r for r in service.requests() if r.kind == "communication_authority"
        )
        return service, did, plan, request

    def test_definite_rejection_allows_corrected_plan(self):
        service, did, plan, request = self.action()
        self.peer.reject = 422
        service.decide(request, "approve")
        eventually(lambda: service.communication(plan["id"])["state"] == "failed", 15)
        self.assertFalse(self.peer.writes)
        self.peer.reject = None
        corrected = service.plan_communication(
            did,
            {
                "kind": "post_comment",
                "intent": "clarify",
                "body": "Corrected question.",
            },
        )
        request = next(
            r
            for r in service.requests()
            if r.arguments.get("action_id") == corrected["id"]
        )
        service.decide(request, "approve")
        eventually(
            lambda: service.communication(corrected["id"])["state"] == "confirmed", 15
        )
        self.assertEqual(len(self.peer.writes), 1)

    def test_unknown_requires_explicit_current_conclusion_and_new_authority(self):
        from homebrew_harness.platforms.effects import EffectFailure

        service, did, plan, request = self.action()
        c = service.runtime.maintenance.communication
        with patch.object(
            c.adapter(service.discussion(did)["subject"]),
            "publish",
            side_effect=EffectFailure("unknown"),
        ):
            service.decide(request, "approve")
            eventually(
                lambda: service.communication(plan["id"])["state"] == "unknown", 15
            )
        self.assertEqual(service.inspect_communication(plan["id"])["state"], "unknown")
        reference = service.communication(plan["id"])["revision"]
        with self.assertRaises(SDKError):
            service.inspect_communication(
                plan["id"],
                conclusion="retry_not_executed",
                revision=reference,
                confirmed=False,
            )
        first = service.inspect_communication(
            plan["id"],
            conclusion="retry_not_executed",
            revision=reference,
            confirmed=True,
            request_id="inspection",
        )
        again = service.inspect_communication(
            plan["id"],
            conclusion="retry_not_executed",
            revision=reference,
            confirmed=True,
            request_id="inspection",
        )
        self.assertEqual(first, again)
        self.assertEqual(first["state"], "awaiting_approval")
        self.assertFalse(self.peer.writes)

    def test_slow_inspection_does_not_hold_control_lock_and_keeps_close_pending(self):
        from homebrew_harness.platforms.effects import EffectFailure

        service, did, plan, request = self.action()
        c = service.runtime.maintenance.communication
        with patch.object(
            c.adapter(service.discussion(did)["subject"]),
            "publish",
            side_effect=EffectFailure("unknown"),
        ):
            service.decide(request, "approve")
            eventually(
                lambda: service.communication(plan["id"])["state"] == "unknown", 15
            )
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def slow(*args, **kwargs):
            started.set()
            release.wait(5)
            return None

        with patch.object(
            c.adapter(service.discussion(did)["subject"]), "publish", side_effect=slow
        ):
            errors = []

            def inspect():
                try:
                    service.inspect_communication(plan["id"], request_id="slow")
                except Exception as error:
                    errors.append(error)

            thread = threading.Thread(target=inspect)
            thread.start()
            self.assertTrue(started.wait(3))
            before = time.monotonic()
            service.snapshot()
            self.assertLess(time.monotonic() - before, 1)
            service.stop()
            self.assertFalse(service.wait_closed(0.1))
            self.assertTrue(c.leases.held("communication-publish"))
            release.set()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertFalse(errors)
        service.close()
        self.assertTrue(service.wait_closed(10))
