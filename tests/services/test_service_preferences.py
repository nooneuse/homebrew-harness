"""Configured preferences stay scoped and survive real Service/Run boundaries."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from homebrew_harness import ServiceSpec
from homebrew_harness.services.job_contracts import HANDLERS, JobContract
from homebrew_harness.services.maintenance_spec import INSTRUCTION_TARGETS
from homebrew_harness.services.model_control import ServiceModels
from homebrew_harness.runs.spec import RUN_CONTROL_TOOLS
from tests.runtime.test_interaction import eventually
from tests.services import test_maintenance as fixture


class PreferenceContracts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def spec(self, instructions):
        return ServiceSpec(str(self.root), profile="repository_maintenance", maintenance={"instructions": instructions})

    def test_guidance_accepts_only_known_bounded_text_and_no_credentials(self):
        invalid = [[], {"pr_template": "unsupported field"}, {"common": ""}, {"common": 42}, {"common": "x" * 8001}, {"common": "sk-" + "a" * 32}, {name: "x" * 2000 for name in INSTRUCTION_TARGETS}]
        for value in invalid:
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError):
                self.spec(value)

    def test_each_job_sees_only_common_and_its_own_preferences(self):
        self.assertEqual(INSTRUCTION_TARGETS, set(HANDLERS) | {"common", "development"})
        configured = {name: "guidance for " + name for name in INSTRUCTION_TARGETS}
        policy = self.spec(configured).maintenance
        store = SimpleNamespace(policy=policy)
        for kind in HANDLERS:
            with self.subTest(kind=kind):
                contract = JobContract(store, {"kind": kind})
                view = contract.policy()
                self.assertEqual(view["instructions"], {"common": configured["common"], kind: configured[kind]})
                view["instructions"][kind] = "changed returned view"
                self.assertEqual(policy["instructions"][kind], configured[kind])

    def test_unconfigured_guidance_adds_no_prompt_field(self):
        store = SimpleNamespace(policy=self.spec({}).maintenance)
        for kind in HANDLERS:
            self.assertNotIn("instructions", JobContract(store, {"kind": kind}).policy())

    def test_role_models_and_explicit_fallbacks_remain_distinct(self):
        spec = SimpleNamespace(
            run_defaults={"model_profile": "coder"},
            communication={"model_profile": "community", "review_model": "reviewer"},
            delivery={"review_model": "reviewer"},
            operations={"audit_model": "audit"},
        )
        agent = SimpleNamespace(runtime=SimpleNamespace(spec=spec), models=SimpleNamespace(alias="planner"), binding=object(), gateway=object())
        models = ServiceModels(agent)
        models.contexts.update({name: (name, name + " gateway") for name in ("community", "reviewer", "audit")})
        for kind in ("triage", "conversation", "report", "ci_triage"):
            self.assertEqual(models.select({"kind": kind, "input": {}}), models.contexts["planner"])
        self.assertEqual(models.select({"kind": "discussion", "input": {}})[0], "community")
        self.assertEqual(models.select({"kind": "discussion", "input": {"review_id": "snapshot"}})[0], "reviewer")
        self.assertEqual(models.select({"kind": "review", "input": {}})[0], "reviewer")
        self.assertEqual(models.select({"kind": "audit", "input": {}})[0], "audit")
        spec.communication["review_model"] = None
        self.assertEqual(models.select({"kind": "discussion", "input": {"review_id": "snapshot"}})[0], "community")
        spec.communication["model_profile"] = None
        self.assertEqual(models.select({"kind": "discussion", "input": {}}), models.contexts["planner"])
        self.assertEqual(spec.run_defaults["model_profile"], "coder")


class PreferenceRuntime(unittest.TestCase):
    start = fixture.MaintenanceTests.start

    def setUp(self):
        fixture.MaintenanceTests.setUp(self)

    def test_native_run_gets_development_guidance_and_keeps_original_scope(self):
        service = self.start(instructions={"common": "Keep compatibility.", "development": "Use project typing conventions.", "review": "PR-only heading instruction."})
        work = service.work.submit("Preserve the original local requirement.")
        eventually(lambda: work.snapshot().state == "review", 20)
        compiled = service.store.work(work.id)["run_spec"]
        self.assertIn("Preserve the original local requirement.", compiled["prompt"])
        self.assertIn("Keep compatibility.", compiled["prompt"])
        self.assertIn("Use project typing conventions.", compiled["prompt"])
        self.assertNotIn("PR-only heading instruction.", compiled["prompt"])
        self.assertEqual(compiled["write_paths"], ["src"])
        self.assertEqual(set(compiled["tools"]) - RUN_CONTROL_TOOLS, {"read_file", "write_file"})

    def test_conversation_payload_is_scoped_and_saved_preferences_survive_restart(self):
        instructions = {"common": "Keep compatibility.", "conversation": "Reply briefly.", "review": "PR heading only."}
        service = self.start(instructions=instructions)
        message = service.converse("Discuss priorities.")
        eventually(lambda: service.conversation(message["id"]).get("result"), 15)
        payloads = [json.loads(m["content"]) for request in self.peer.model_requests for m in request["messages"] if m.get("role") == "user" and m.get("content", "").startswith('{"job":')]
        current = next(p for p in payloads if p["job"]["kind"] == "conversation")
        self.assertEqual(current["policy"]["instructions"], {"common": instructions["common"], "conversation": instructions["conversation"]})
        service.stop()
        self.assertTrue(service.wait_closed(10))
        restarted = self.host.services.start(identity=service.id)
        self.assertEqual(restarted.runtime.maintenance.store.policy["instructions"], instructions)


if __name__ == "__main__":
    unittest.main()
