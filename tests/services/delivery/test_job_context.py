"""Job inputs and retry preserve Work facts and actual candidate evidence."""

import json
import unittest
from unittest.mock import patch

from homebrew_harness.runtime.errors import AgentError, boundary_error
from homebrew_harness.services.agent import ServiceAgent
from tests.services.delivery import test_flow as fixture
from tests.runtime.test_interaction import eventually


class JobContextFlows(unittest.TestCase):
    setUp = fixture.FlowTests.setUp
    start = fixture.FlowTests.start
    run_git = fixture.FlowTests.run_git
    pending_target = fixture.FlowTests.pending_target

    def test_soft_call_warning_allows_a_later_valid_proposal(self):
        from tests.services import p2_fixture as model_fixture
        self.peer.ci_failure = True
        original_propose = ServiceAgent.propose
        original_completion = model_fixture.completion
        attempts = 0
        call_id = 0
        def complete(*args, **kwargs):
            nonlocal call_id
            value = original_completion(*args, **kwargs)
            if value["choices"][0]["message"].get("content") == "Recorded.":
                request = self.peer.model_requests[-1]
                envelopes = [json.loads(m["content"]) for m in request["messages"]
                    if m.get("role") == "user" and m.get("content", "").startswith('{"job":')]
                if envelopes and envelopes[0]["job"]["kind"] == "ci_triage":
                    value = original_completion(tool="service_propose", arguments={"proposal": json.dumps({
                        "classification": "code", "reason": "Recorded sufficient evidence.",
                        "plan": "Set src/value.txt to 2 and preserve the fixed tests."})})
            call_id += 1
            for call in value["choices"][0]["message"].get("tool_calls", []):
                call["id"] += str(call_id)
            return value
        def propose(agent, proposal, **kwargs):
            nonlocal attempts
            if agent.job["kind"] == "ci_triage":
                attempts += 1
                if attempts < 5:
                    raise ValueError("Synthetic evidence still being evaluated.")
            return original_propose(agent, proposal, **kwargs)
        with patch.object(ServiceAgent, "propose", propose), patch.object(model_fixture, "completion", complete):
            service = self.start(auto_ci_fix=False, maintenance={
                "job_turns": None, "job_call_warning": 2, "job_seconds": 30,
            })
            work = service.work.submit("Fix the scoped value.")
            row = eventually(lambda: next((r for r in service.records("changesets")["items"]
                if r.get("ci_diagnosis")), None), 40)
        self.assertEqual(attempts, 5)
        self.assertEqual(row["ci_diagnosis"]["classification"], "code")
        observations = [m["content"] for request in self.peer.model_requests for m in request["messages"]
            if m.get("name") == "service_call_observation"]
        self.assertTrue(any("2 model calls" in text for text in observations), observations)
        self.assertTrue(any("4 model calls" in text for text in observations), observations)
        self.assertTrue(all("not a stop condition" in text for text in observations))
        self.assertEqual(service.store.work(work.id)["requirements_revision"], 0)

    def test_first_ci_request_includes_latest_context_without_fixed_payload_duplication(self):
        self.peer.ci_failure = True
        original = ServiceAgent._execute
        marker = "Current operator directive: integrate the already verified framework update."
        inventory = {"host-only-inventory": "v" * 200000}
        def execute(agent, job, **kw):
            if job["kind"] == "ci_triage":
                with agent.store.db.write() as conn:
                    row = agent.store.get("changesets", job["target"], conn)
                    work = agent.store.get("work", row["work_id"], conn)
                    work["execution_context"] = [marker]
                    work["amendments"] = ["Accepted scope remains unchanged. " + "A" * 35000]
                    work["execution_context"].append("Published source evidence: " + "B" * 35000)
                    agent.store.put(conn, "work", work)
                    row["primary_expected"] = inventory
                    row["quality"][0]["inputs"] = inventory
                    row["validation_history"] = [{"quality": [{"run_id": "previous-failure",
                        "status": "failed", "inputs": inventory}]}]
                    agent.store.put(conn, "changesets", row)
            return original(agent, job, **kw)
        with patch.object(ServiceAgent, "_execute", execute):
            service = self.start(auto_ci_fix=False)
            work = service.work.submit("Fix the scoped value.")
            row = eventually(lambda: next((r for r in service.records("changesets")["items"]
                if r.get("ci_diagnosis")), None), 40)
        requests = [request for request in self.peer.model_requests if any(
            m.get("role") == "user" and m.get("content", "").startswith("{")
            and json.loads(m["content"]).get("job", {}).get("kind") == "ci_triage"
            for m in request["messages"])]
        self.assertTrue(requests)
        first = requests[0]
        body = next(json.loads(m["content"]) for m in first["messages"]
                    if m.get("role") == "user" and m.get("content", "").startswith("{")
                    and json.loads(m["content"]).get("job", {}).get("kind") == "ci_triage")
        self.assertEqual(body["input"]["work_context"]["execution_context"][0], marker)
        self.assertIn("A" * 35000, json.dumps(body))
        self.assertIn("B" * 35000, json.dumps(body))
        self.assertEqual(body["input"]["validation_history"][0]["quality"][0]["run_id"], "previous-failure")
        self.assertNotIn("host-only-inventory", json.dumps(first))
        intents = [m for m in first["messages"] if m.get("name") == "task_intent"]
        self.assertTrue(intents)
        self.assertTrue(all(len(m["content"]) < 6000 for m in intents),
                        [(len(m["content"]), m["content"][:800]) for m in intents])
        self.assertTrue(all("A" * 35000 not in m["content"] and "B" * 35000 not in m["content"] for m in intents))
        self.assertEqual(json.dumps(first).count(marker), 1)
        self.assertLess(len(json.dumps(first)), 100000)
        self.assertEqual(service.store.work(work.id)["requirements_revision"], 0)

    def test_job_failure_is_visible_and_retry_does_not_repeat_candidate_tests(self):
        self.peer.ci_failure = True
        original = ServiceAgent._execute
        failing = True
        def execute(agent, job, **kw):
            if failing and job["kind"] == "ci_triage":
                raise boundary_error(AgentError, "model_turn_limit", "Synthetic model-call limit reached.",
                                     limit_value=24, observed_value=24, unit="model_calls")
            return original(agent, job, **kw)
        with patch.object(ServiceAgent, "_execute", execute):
            service = self.start(auto_ci_fix=False, maintenance={"job_attempts": 1})
            work = service.work.submit("Fix the scoped value.")
            row = eventually(lambda: next((r for r in service.runtime.maintenance.delivery.rows("changesets")
                if r["state"] == "blocked"), None), 40)
            job_id = row["reasoning_job_id"]
            issue = next(i for i in service.health()["issues"] if i["code"] == "delivery_blocked")
            self.assertEqual(issue["reason_code"], "model_turn_limit")
            self.assertEqual(issue["reasoning_job_id"], job_id)
            self.assertIn("model-call limit", issue["message"])
            self.assertEqual(work.progress()["delivery"]["error_info"]["code"], "model_turn_limit")
            previous_quality = row["quality"]
            previous_run = service.store.work(work.id)["run_id"]
            failing = False
            service.retry_job(job_id, request_id="retry-reasoning-only")
            current = eventually(lambda: next((r for r in service.runtime.maintenance.delivery.rows("changesets")
                if r["id"] == row["id"] and r["state"] == "repair_waiting"), None), 30)
        self.assertEqual(current["quality"], previous_quality)
        self.assertEqual(service.store.work(work.id)["run_id"], previous_run)
        self.assertIsNone(current.get("error_info"))
        job = service.runtime.maintenance.store.job_get(job_id)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(job["retry_history"][0]["error_info"]["code"], "model_turn_limit")
        self.assertEqual(len(self.peer.pulls), 1)

    def test_ci_repair_run_inherits_current_operator_context(self):
        self.peer.ci_failure = True
        service = self.start(auto_ci_fix=False)
        work = service.work.submit("Fix the scoped value.")
        request = eventually(lambda: next((r for r in service.requests() if r.kind == "ci_repair"), None), 40)
        work.context("Use the currently published framework fix and preserve its recorded tests.")
        service.decide(request, "approve", decision_id="repair-with-current-context")
        eventually(lambda: self.pending_target(service), 40)
        current = service.store.work(work.id)
        self.assertIn("currently published framework fix", current["run_spec"]["prompt"])
        self.assertEqual(current["requirements_revision"], 0)
        self.assertEqual(current["ci_fix_attempts"], 1)

    def test_remaining_phase_calls_are_visible_before_the_final_proposal(self):
        import examples.delivery_fixture as model_fixture
        self.peer.ci_failure = True
        original = ServiceAgent.propose
        original_completion = model_fixture.completion
        attempts = 0
        calls = 0
        def unique_completion(*args, **kw):
            nonlocal calls
            value = original_completion(*args, **kw)
            # This peer normally acknowledges tool results. For this scenario,
            # it must retry the deliberately rejected proposal in the same phase.
            if value["choices"][0]["message"].get("content") == "Recorded.":
                current = self.peer.model_requests[-1]
                envelopes = [json.loads(m["content"]) for m in current["messages"]
                    if m.get("role") == "user" and m.get("content", "").startswith('{"job":')]
                if envelopes and envelopes[0]["job"]["kind"] == "ci_triage":
                    value = original_completion(tool="service_propose", arguments={"proposal": json.dumps({
                        "classification": "code", "reason": "Confirmed current-candidate regression.",
                        "plan": "Set src/value.txt to 2 and preserve the fixed tests."})})
            calls += 1
            for call in value["choices"][0]["message"].get("tool_calls", []):
                call["id"] += str(calls)
            return value
        def propose(agent, proposal, **kw):
            nonlocal attempts
            if agent.job["kind"] == "ci_triage":
                attempts += 1
                if attempts < 3:
                    raise ValueError("Synthetic unfinished evidence read.")
            return original(agent, proposal, **kw)
        with patch.object(ServiceAgent, "propose", propose), patch.object(model_fixture, "completion", unique_completion):
            service = self.start(auto_ci_fix=False, maintenance={"job_turns": 3, "job_seconds": 30})
            work = service.work.submit("Fix the scoped value.")
            eventually(lambda: next((r for r in service.records("changesets")["items"] if r.get("ci_diagnosis")), None), 40)
        requests = [r for r in self.peer.model_requests if any(
            m.get("role") == "user" and m.get("content", "").startswith("{")
            and json.loads(m["content"]).get("job", {}).get("kind") == "ci_triage" for m in r["messages"])]
        remaining = [next(m["content"] for m in r["messages"] if m.get("name") == "service_remaining_calls") for r in requests]
        self.assertEqual(len(remaining), 3, remaining)
        self.assertIn("3 model calls", remaining[0])
        self.assertIn("1 model calls", remaining[-1])
        self.assertEqual(service.store.work(work.id)["requirements_revision"], 0)
