"""Job result transactions and bounded internal evidence over real service storage."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from homebrew_harness.services.job_contracts import HANDLERS, JobContract
from tests.services import test_maintenance as fixture


class JobContracts(unittest.TestCase):
    start = fixture.MaintenanceTests.start

    def setUp(self):
        fixture.MaintenanceTests.setUp(self)
        self.service = self.start(auto=False)
        self.service.pause("all")
        self.store = self.service.runtime.maintenance.store
        self.agent = self.service.runtime.maintenance.agent

    def activate(self, receipt):
        # The actual worker is paused; exercise a single claimed job deterministically.
        with self.store.db.write() as conn:
            job = self.store.get("jobs", receipt["job_id"], conn)
            job.update(
                state="running", attempts=1, generation=self.store.header["generation"]
            )
            self.store.put(conn, "jobs", job)
        self.agent.job = job
        return job

    def read(self, kind, identity="", **kwargs):
        return json.loads(self.agent.read(kind, identity, **kwargs))

    def test_all_job_schemas_reject_invalid_candidates_and_unknown_jobs(self):
        receipt = self.service.converse("Discuss the next step.")
        job = self.activate(receipt)
        valid = {
            "triage": self.peer.triage,
            "discussion": {
                "next_step": "wait",
                "reason": "Await facts.",
                "alignment": "Within scope.",
            },
            "review": {"text": "Reviewed fixed candidate.", "findings": []},
            "ci_triage": {
                "classification": "unknown",
                "reason": "Missing facts.",
                "plan": "Read logs.",
            },
            "audit": {"text": "Reviewed fixed code.", "findings": []},
            "conversation": {"text": "Discuss the plan."},
            "report": {"text": "Stored facts only."},
            "incident": {"text": "Recorded diagnosis."},
            "upgrade_assessment": {"text": "Recorded assessment."},
        }
        self.assertEqual(set(valid), set(HANDLERS))
        for kind, candidate in valid.items():
            with self.subTest(kind=kind):
                contract = JobContract(self.store, {**job, "kind": kind})
                self.assertEqual(contract.validate(candidate), candidate)
                with self.assertRaises(ValueError):
                    contract.validate({"unexpected": "invalid"})
        with self.assertRaises(ValueError):
            JobContract(self.store, {**job, "kind": "unknown"})
        with self.store.db.write() as conn, self.assertRaises(ValueError):
            self.store.job(conn, "unknown", "target", 0, {}, "invalid-kind")
        self.assertEqual(len(self.service.records("jobs")["items"]), 1)

    def test_conversation_history_and_direct_reads_share_context_and_time_bounds(self):
        earlier = [
            self.service.converse("History " + str(n), context="planning")
            for n in range(12)
        ]
        other = self.service.converse("Unrelated private discussion.", context="other")
        current = self.service.converse("Current discussion.", context="planning")
        future = self.service.converse("Later instruction.", context="planning")
        self.activate(current)
        one = self.read("history")
        two = self.read("history", offset=one["next_offset"])
        ids = [row["id"] for row in one["items"] + two["items"]]
        self.assertEqual(ids, [r["id"] for r in earlier] + [current["id"]])
        self.assertIsNone(two["next_offset"])
        self.assertIn("History 0", self.read("messages", earlier[0]["id"])["text"])
        for identity in (other["id"], future["id"]):
            with self.assertRaises(ValueError):
                self.read("messages", identity)

    def test_unfocused_conversation_has_a_minimal_directory_not_full_work(self):
        work = self.service.work.submit("Private input body.")
        self.activate(self.service.converse("Which Work should we discuss?"))
        directory = self.read("work_list")
        self.assertEqual(directory["items"][0]["id"], work.id)
        self.assertNotIn("Private input body", json.dumps(directory))
        for kind, identity in (
            ("work", work.id),
            ("reports", "other"),
            ("job", "other"),
        ):
            with self.assertRaises(ValueError):
                self.read(kind, identity)

    def test_bound_conversation_reads_associated_facts_but_not_other_work(self):
        work = self.service.work.submit("Preserve all accepted requirements.")
        other = self.service.work.submit("Unrelated input.")
        with self.store.db.write() as conn:
            record = self.store.get("work", work.id, conn)
            record.update(events=["event_bound"], requirements_id="requirements_bound")
            self.store.put(conn, "work", record)
            for kind, row in (
                (
                    "events",
                    {
                        "id": "event_bound",
                        "work_id": work.id,
                        "content": "Complete source facts.",
                    },
                ),
                (
                    "requirements",
                    {
                        "id": "requirements_bound",
                        "draft": {"out_of_scope": "Retain this exclusion."},
                    },
                ),
                (
                    "evidence",
                    {
                        "id": "evidence_bound",
                        "work_id": work.id,
                        "receipts": ["Actual receipt."],
                    },
                ),
                ("changesets", {"id": "changeset_bound", "work_id": work.id}),
                ("actions", {"id": "action_bound", "changeset_id": "changeset_bound"}),
                (
                    "decisions",
                    {
                        "id": "decision_bound",
                        "work_id": work.id,
                        "candidate": "Unapproved plan.",
                    },
                ),
                ("decisions", {"id": "decision_other", "work_id": other.id}),
            ):
                self.store.put(conn, kind, row)
        receipt = self.service.converse(
            "Discuss this Work.", work_id=work.id, revision=0
        )
        self.activate(receipt)
        for kind, identity in (
            ("work", work.id),
            ("events", "event_bound"),
            ("requirements", "requirements_bound"),
            ("evidence", "evidence_bound"),
            ("changesets", "changeset_bound"),
            ("actions", "action_bound"),
            ("decisions", "decision_bound"),
        ):
            with self.subTest(kind=kind):
                self.assertIn(identity, self.read(kind, identity)["text"])
        for kind, identity in (
            ("work_list", ""),
            ("work", other.id),
            ("decisions", "decision_other"),
        ):
            with self.assertRaises(ValueError):
                self.read(kind, identity)

    def test_plan_is_bound_early_and_revalidated_without_mutation_at_finish(self):
        work = self.service.work.submit("Maintain the public contract.")
        other = self.service.work.submit("Other work.")
        receipt = self.service.converse(
            "Propose a bounded plan.", work_id=work.id, revision=0
        )
        job = self.activate(receipt)
        for identity in (other.id, None, ""):
            candidate = {"text": "Plan.", "work_id": identity, "plan": self.peer.triage}
            with self.assertRaises(ValueError):
                self.agent.propose(json.dumps(candidate))
        candidate = {"text": "Plan.", "work_id": work.id, "plan": self.peer.triage}
        self.assertFalse(
            json.loads(self.agent.propose(json.dumps(candidate)))["persisted"]
        )
        work.revise("Preserve a new requirement too.")
        before = self.service.store.work(work.id)
        with self.assertRaises(ValueError):
            self.store.finish(job, self.agent.proposal)
        self.assertEqual(self.store.job_get(job["id"])["state"], "running")
        self.assertIsNone(self.service.conversation(receipt["id"]).get("result"))
        self.assertEqual(self.service.store.work(work.id), before)
        self.assertEqual(self.service.records("decisions")["items"], [])

    def test_plan_creates_a_request_without_granting_execution(self):
        work = self.service.work.submit("Maintain the public contract.")
        receipt = self.service.converse("Propose a plan.", work_id=work.id, revision=0)
        job = self.activate(receipt)
        candidate = {"text": "Plan.", "work_id": work.id, "plan": self.peer.triage}
        self.store.finish(job, candidate)
        self.assertEqual(self.service.conversation(receipt["id"])["state"], "answered")
        self.assertEqual(self.store.job_get(job["id"])["state"], "completed")
        self.assertEqual(len(self.service.records("decisions")["items"]), 1)
        self.assertIsNone(self.service.store.work(work.id)["authority"])
        self.assertIsNone(self.service.store.work(work.id)["run_id"])

    def test_result_apply_and_job_completion_roll_back_together(self):
        work = self.service.work.submit("Keep the transactional result.")
        receipt = self.service.converse("Propose a plan.", work_id=work.id, revision=0)
        job = self.activate(receipt)
        original = self.store.db._put

        def fail_completion(conn, section, key, value):
            if section == "service_jobs_model" and value["state"] == "completed":
                raise OSError("Simulated final job write failure.")
            return original(conn, section, key, value)

        candidate = {"text": "Plan.", "work_id": work.id, "plan": self.peer.triage}
        before = self.service.store.get(self.service.id)["activity"]
        with (
            patch.object(self.store.db, "_put", fail_completion),
            self.assertRaises(OSError),
        ):
            self.store.finish(job, candidate)
        self.assertEqual(self.service.conversation(receipt["id"])["state"], "queued")
        self.assertEqual(self.service.records("decisions")["items"], [])
        self.assertEqual(self.service.store.get(self.service.id)["activity"], before)
        self.assertEqual(self.store.job_get(job["id"])["state"], "running")
        self.store.finish(job, candidate)
        self.assertEqual(self.store.job_get(job["id"])["state"], "completed")

    def test_finish_uses_persisted_job_not_modified_caller_metadata(self):
        receipt = self.service.converse("An ordinary discussion.")
        job = self.activate(receipt)
        forged = {**job, "kind": "report", "target": "other_report"}
        self.store.finish(forged, {"text": "Discussion result."})
        self.assertEqual(self.service.conversation(receipt["id"])["state"], "answered")
        self.assertEqual(self.service.records("reports")["items"], [])

    def test_report_reads_window_roots_and_full_facts_only(self):
        work = self.service.work.submit("Window requirement.")
        with self.store.db.write() as conn:
            self.store.put(
                conn,
                "evidence",
                {
                    "id": "window_evidence",
                    "work_id": work.id,
                    "text": "Detailed actual receipt.",
                },
            )
            self.store.changed(conn, work.id)
            receipt = self.store.report(conn, force=True)
        other = self.service.work.submit("Future unrelated requirement.")
        unrelated = self.service.converse("Unrelated private conversation.")
        with self.store.db.write() as conn:
            self.store.put(conn, "reports", {"id": "unrelated_report", "facts": []})
            self.store.put(
                conn, "decisions", {"id": "future_decision", "work_id": other.id}
            )
        job = self.activate(receipt)
        offset, chunks = 0, []
        while offset is not None:
            page = self.read("reports", receipt["id"], offset=offset, limit=100)
            chunks.append(page["text"])
            offset = page["next_offset"]
        self.assertEqual(
            json.loads("".join(chunks)), self.store.get("reports", receipt["id"])
        )
        self.assertIn(
            "Detailed actual receipt", self.read("evidence", "window_evidence")["text"]
        )
        for kind, identity in (
            ("work_list", ""),
            ("work", other.id),
            ("messages", unrelated["id"]),
            ("reports", "unrelated_report"),
            ("decisions", "future_decision"),
            ("health", ""),
        ):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.read(kind, identity)
        with self.assertRaises(ValueError):
            self.agent.read_code("module.py")
        result = self.agent.run(job)
        offered = {
            tool["function"]["name"] for tool in self.peer.model_requests[0]["tools"]
        }
        self.assertIn("service_read", offered)
        self.assertNotIn("service_read_code", offered)
        self.assertNotIn("glob", offered)
        self.store.finish(job, result)
        self.assertEqual(self.service.report(receipt["id"])["state"], "ready")

    def test_report_uses_historical_verification_reference_after_work_changes(self):
        work = self.service.work.submit("Historical facts.")
        with self.store.db.write() as conn:
            self.store.put(
                conn, "evidence", {"id": "historical_receipt", "work_id": work.id}
            )
            row = self.store.get("work", work.id, conn)
            row["verification"] = {"id": "historical_receipt"}
            self.store.put(conn, "work", row)
            self.store.changed(conn, work.id)
            receipt = self.store.report(conn, force=True)
            row.pop("verification")
            self.store.put(conn, "work", row)
        self.activate(receipt)
        self.assertIn(
            "historical_receipt", self.read("evidence", "historical_receipt")["text"]
        )

    def test_operation_assessment_does_not_apply_to_a_revised_target(self):
        for kind, section in (
            ("incident", "incidents"),
            ("upgrade_assessment", "upgrade_plans"),
        ):
            with self.subTest(kind=kind):
                with self.store.db.write() as conn:
                    self.store.put(conn, section, {"id": kind, "revision": 1})
                    job = self.store.job(conn, kind, kind, 0, {}, kind)
                self.activate({"job_id": job["id"]})
                finished = self.store.finish(job, {"text": "Old assessment."})
                self.assertEqual(finished["state"], "stale")
                self.assertNotIn("assessment", self.store.get(section, kind))

    def test_stopping_requeues_candidate_before_validation_or_application(self):
        receipt = self.service.converse("An unfinished discussion.")
        job = self.activate(receipt)
        self.service.store.update(self.store.header, state="stopping")
        finished = self.store.finish(job, {"unexpected": "Invalid candidate."})
        self.assertEqual(finished["state"], "queued")
        self.assertEqual(finished["error"], "service_stopping")
        self.assertIsNone(self.service.conversation(receipt["id"]).get("result"))

    def test_internal_jobs_follow_accepted_discussion_and_publication_evidence(self):
        from homebrew_harness.services.communication.store import CommunicationStore

        communication = CommunicationStore(
            self.service.runtime, self.store, {"projects": []}
        )
        work = self.service.work.submit("Accepted community request.")
        with self.store.db.write() as conn:
            row = self.store.get("work", work.id, conn)
            row["requirements_id"] = "accepted_requirement"
            self.store.put(conn, "work", row)
            self.store.put(
                conn,
                "requirements",
                {
                    "id": "accepted_requirement",
                    "discussion_id": "bound_discussion",
                    "sources": ["source_event"],
                    "draft": {"out_of_scope": "Keep the exclusion."},
                },
            )
            self.store.put(
                conn,
                "events",
                {"id": "source_event", "discussion_id": "bound_discussion"},
            )
            for kind, identity in (
                ("discussions", "bound_discussion"),
                ("rounds", "bound_round"),
                ("communication_actions", "bound_publication"),
                ("review_snapshots", "bound_snapshot"),
            ):
                communication.put(
                    conn, kind, {"id": identity, "discussion_id": "bound_discussion"}
                )
            communication.put(conn, "discussions", {"id": "other_discussion"})
            communication.put(
                conn,
                "communication_actions",
                {"id": "other_publication", "discussion_id": "other_discussion"},
            )
            self.store.changed(conn, work.id)
            report = self.store.report(conn, force=True)
        conversation = self.service.converse(
            "Discuss the accepted request.", work_id=work.id, revision=0
        )
        with (
            patch.object(self.store, "communication", communication),
            patch.object(
                self.service.runtime.maintenance,
                "communication",
                SimpleNamespace(store=communication),
            ),
        ):
            for receipt in (conversation, report):
                self.activate(receipt)
                for kind, identity in (
                    ("requirements", "accepted_requirement"),
                    ("events", "source_event"),
                    ("discussions", "bound_discussion"),
                    ("rounds", "bound_round"),
                    ("communication_actions", "bound_publication"),
                    ("review_snapshots", "bound_snapshot"),
                ):
                    with self.subTest(job=receipt["job_id"], kind=kind):
                        self.assertIn(identity, self.read(kind, identity)["text"])
                for kind, identity in (
                    ("discussions", "other_discussion"),
                    ("communication_actions", "other_publication"),
                ):
                    with self.assertRaises(ValueError):
                        self.read(kind, identity)
