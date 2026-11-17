import json
import unittest

from homebrew_harness.execution.model_protocol import ProviderError, ModelOutputError
from homebrew_harness.execution.model_gateway import ModelDeadlineExceeded
from homebrew_harness.runs import RunSpec
from tests.runs import test_controller as fixture
from tests.support.clients import reply, call


def filtered():
    return ProviderError("invalid_request", code="data_inspection_failed", message="Output data may contain inappropriate content.")


class ModelRecoveryTests(unittest.TestCase):
    setUp = fixture.RunTests.setUp
    controller = fixture.RunTests.controller
    write = fixture.RunTests.write

    def responses(self, values, *, spec=None):
        control, session, client, requests = self.controller([], spec or RunSpec("Continue the original task", episode_model_calls=10))
        values = iter(values)
        def send(**kwargs):
            requests.append(kwargs["messages"])
            value = next(values)
            if isinstance(value, Exception):
                raise value
            return value
        client.chat.completions.create.side_effect = send
        return control, session, client, requests

    def test_output_filter_is_an_observation_and_model_continues_without_replaying_write(self):
        control, session, client, requests = self.responses([self.write("a", "a.txt"), filtered(), self.write("b", "b.txt"), reply("done")])
        outcome = control.execute()
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(client.chat.completions.create.call_count, 4)
        self.assertEqual((self.root / "a.txt").read_text(), "ok")
        self.assertEqual((self.root / "b.txt").read_text(), "ok")
        observations = [m for m in requests[2] if m.get("name") == "model_error_observation"]
        self.assertEqual(len(observations), 1)
        data = json.loads(observations[0]["content"])
        self.assertEqual(data["error"]["error_info"]["code"], "data_inspection_failed")
        self.assertEqual(data["business_actions_from_failed_model_request"], 0)
        self.assertEqual(session.intent.original, control.spec.prompt)
        self.assertTrue(self.store.get(control.id)["model_feedback"]["latest"]["resolved"])

    def test_model_can_block_and_later_batch_write_is_not_executed(self):
        stop = call("block", name="report_run_blocker", arguments=json.dumps({"reason":"Need the external dependency restored", "required_action":"Restore it before continuing"}))
        bad = call("bad", name="write_file", arguments=json.dumps({"path":"bad.txt","content":"must not run"}))
        control, session, client, _ = self.responses([filtered(), reply(calls=[stop, bad])])
        outcome = control.execute()
        self.assertEqual(outcome.status, "blocked")
        self.assertEqual(outcome.reason_code, "model_blocked")
        self.assertIn("external dependency", outcome.text)
        self.assertFalse((self.root / "bad.txt").exists())
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertTrue(self.store.get(control.id).get("model_blocker"))

    def test_feedback_attempts_are_bounded(self):
        control, _, client, _ = self.responses([filtered(), filtered(), filtered()], spec=RunSpec("Do work", episode_model_calls=10, retries=2))
        outcome = control.execute()
        self.assertEqual(outcome.status, "blocked")
        self.assertEqual(outcome.reason_code, "model_error_recovery_exhausted")
        self.assertEqual(client.chat.completions.create.call_count, 3)
        self.assertEqual(self.store.get(control.id)["model_feedback"]["consecutive"], 3)

    def test_malformed_model_response_is_returned_as_feedback(self):
        control, _, client, requests = self.responses([ModelOutputError("Malformed provider completion."), reply("done")])
        self.assertEqual(control.execute().status, "completed")
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertTrue(any(m.get("name") == "model_error_observation" for m in requests[1]))

    def test_authentication_failure_is_not_a_model_repair_attempt(self):
        control, _, client, _ = self.responses([ProviderError("auth", status_code=401), reply("should not run")])
        self.assertEqual(control.execute().status, "blocked")
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertFalse(self.store.get(control.id).get("model_feedback"))

    def test_input_filter_stays_outside_the_initial_output_repair_contract(self):
        control, _, client, _ = self.responses([ProviderError("invalid_request", code="data_inspection_failed", message="Input data may contain inappropriate content."), reply("should not run")])
        self.assertEqual(control.execute().status, "blocked")
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_feedback_count_survives_restarting_the_same_run(self):
        control, session, _, _ = self.responses([filtered(), ProviderError("transport")], spec=RunSpec("Do work", episode_model_calls=10, retries=1))
        self.assertEqual(control.execute().status, "retrying")
        self.assertEqual(self.store.get(control.id)["model_feedback"]["consecutive"], 1)
        session.close()
        self.assertTrue(session.wait_closed(5))
        control.close_heartbeat()
        self.store.ended(control.header, cleanup=True)
        self.store.update(control.header, {"next_retry_at": 0})
        resumed, next_session, client, requests = self.controller([], identity=control.id)
        client.chat.completions.create.side_effect = filtered()
        outcome = resumed.execute()
        self.assertEqual(outcome.reason_code, "model_error_recovery_exhausted")
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertEqual(self.store.get(control.id)["model_feedback"]["consecutive"], 2)

    def test_unverified_tool_effect_prevents_model_error_feedback(self):
        from unittest.mock import patch
        control, session, _, _ = self.responses([])
        control.prepare()
        with patch.object(session, "recovery_operations", return_value=[{"state":"unknown"}]):
            self.assertFalse(control.observe_model_failure(filtered(), session.messages))
        self.assertFalse(self.store.get(control.id).get("model_feedback"))

    def test_recovery_preparation_reports_its_specific_cause(self):
        from unittest.mock import patch
        from homebrew_harness.runtime.runtime_state import RecoveryRequired
        control, _, client, _ = self.responses([])
        with patch.object(control, "prepare", side_effect=RecoveryRequired("Specific binding check failed.")):
            outcome = control.execute()
        self.assertEqual(outcome.reason_code, "recovery_required")
        self.assertIn("Specific binding check failed", outcome.details["error_info"]["message"])
        self.assertEqual(client.chat.completions.create.call_count, 0)

    def test_runtime_recovery_tool_preserves_the_serialized_run_contract(self):
        spec = RunSpec("Keep original requirements")
        saved = json.loads(json.dumps(spec.data()))
        self.assertNotIn("report_run_blocker", saved["tools"])
        self.assertIn("report_run_blocker", spec.effective_tools())
        self.assertEqual(json.loads(json.dumps(RunSpec.from_dict(saved).data())), saved)

    def test_expired_deadline_has_no_feedback_request(self):
        control, _, client, _ = self.responses([filtered()])
        self.store.update(control.header, {"deadline_at": 0})
        self.assertEqual(control.execute().reason_code, "deadline_reached")
        self.assertEqual(client.chat.completions.create.call_count, 0)
