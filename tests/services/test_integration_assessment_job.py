"""Frozen periodic integration input and generic service job routing."""

import json
import unittest
from unittest.mock import patch

from homebrew_harness.services.delivery import periodic
from homebrew_harness.services.job_contracts import JobContract
from tests.services import p2_fixture, test_maintenance as fixture


class IntegrationAssessmentValidation(unittest.TestCase):
    def test_strict_decision_schema(self):
        job = {"kind": "integration_assessment", "input": {"overdue": False}}
        contract = JobContract(None, job)
        reason = "Wait for pending feedback on this head."
        defer = {"action": "defer", "reason": reason}
        for valid in (
            {"action": "integrate", "reason": reason},
            {"action": "blocked", "reason": reason},
            defer,
            {**defer, "defer_seconds": 2.5},
        ):
            self.assertEqual(contract.validate(valid), valid)
        for invalid in (
            None,
            [defer],
            {"action": "defer"},
            {"reason": reason},
            {**defer, "action": "wait"},
            {**defer, "action": []},
            {**defer, "reason": True},
            {**defer, "reason": "  "},
            {**defer, "extra": False},
            {**defer, "action": "integrate", "defer_seconds": 1},
            {**defer, "action": "blocked", "defer_seconds": None},
            *({**defer, "defer_seconds": seconds} for seconds in (
                True, False, "1", None, 0, -1, float("nan"), float("inf"), 10**400,
            )),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                contract.validate(invalid)
        job["input"]["overdue"] = True
        for invalid in (defer, {**defer, "defer_seconds": 1}):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "overdue"):
                contract.validate(invalid)
        for action in ("integrate", "blocked"):
            valid = {"action": action, "reason": reason}
            self.assertEqual(contract.validate(valid), valid)


class IntegrationAssessmentJob(unittest.TestCase):
    start = fixture.MaintenanceTests.start

    def setUp(self):
        fixture.MaintenanceTests.setUp(self)
        service = self.start(auto=False, job_attempts=1)
        service.pause("all")
        self.store = service.runtime.maintenance.store
        self.agent = service.runtime.maintenance.agent

    def queue(self):
        payload = {
            "work_id": "work_bound_to_frozen_input",
            "source_head": "a" * 40,
            "target_commit": "b" * 40,
            "conflicts": ["src/value.py"],
            "ci_ready": True,
            "review_ready": False,
            "feedback_pending": True,
            "overdue": True,
        }
        with self.store.db.write() as conn:
            self.store.put(conn, "changesets", {
                "id": "changeset_current", "revision": 4,
                "candidate": {"commit": "c" * 40, "internal": "PRIVATE"},
            })
            queued = self.store.job(conn, "integration_assessment", "changeset_current", 3,
                                    payload, "periodic-evidence-key")
        # Target admission is exercised with actual Git and Work state in test_periodic_flow.
        with patch.object(periodic, "job_current", return_value=True):
            claimed = self.store.claim_job()
        self.assertEqual(claimed["id"], queued["id"])
        return claimed

    def test_existing_agent_uses_only_frozen_changeset_evidence(self):
        job = self.queue()
        contract = JobContract(self.store, job)
        self.assertEqual(contract.handler.section, "changesets")
        self.assertFalse(contract.handler.code)
        contract.primary()["conflicts"].clear()
        self.assertEqual(contract.input({"revision": 4}), job["input"])

        decision = {"action": "blocked", "reason": "Feedback on src/value.py still needs review."}
        completion = p2_fixture.completion(
            tool="service_propose", arguments={"proposal": json.dumps(decision)}
        )
        with patch.object(p2_fixture, "completion", return_value=completion):
            self.assertEqual(self.agent.run(job), decision)
        request = self.peer.model_requests[0]
        envelope = next(json.loads(m["content"]) for m in request["messages"]
                        if m["role"] == "user" and m["content"].startswith('{"job":'))
        self.assertEqual(envelope["input"], job["input"])
        offered = {tool["function"]["name"] for tool in request["tools"]}
        self.assertIn("service_propose", offered)
        self.assertTrue(offered.isdisjoint({"glob", "search_text", "service_read_code", "service_work_sources"}))
        page = json.loads(self.agent.read("changesets", job["target"]))
        self.assertEqual(json.loads(page["text"]), job["input"])
        with self.assertRaises(ValueError):
            self.agent.read("changesets", "changeset_other")

        with patch.object(periodic, "finish", return_value="completed") as apply:
            finished = self.store.finish(job, decision)
        self.assertEqual((finished["state"], finished["result"]), ("completed", decision))
        apply.assert_called_once()
        store, _, applied_job, applied_result = apply.call_args.args
        self.assertIs(store, self.store)
        self.assertEqual(applied_job["id"], job["id"])
        self.assertEqual(applied_result, decision)

    def test_terminal_job_error_routes_to_periodic_failure(self):
        job = self.queue()
        before = self.store.get("changesets", job["target"])
        with patch.object(periodic, "fail") as fail:
            finished = self.store.finish(job, error=ValueError("Assessment failed."))
        self.assertEqual(finished["state"], "failed")
        fail.assert_called_once()
        store, _, failed_job = fail.call_args.args
        self.assertIs(store, self.store)
        self.assertEqual(failed_job["id"], job["id"])
        self.assertEqual(failed_job["error"], "ValueError")
        self.assertEqual(self.store.get("changesets", job["target"]), before)
