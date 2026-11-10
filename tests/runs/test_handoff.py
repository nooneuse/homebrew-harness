import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlunsplit

from homebrew_harness.execution.model_registry import ModelRegistry, ModelSpec
from homebrew_harness.execution.model_protocol import ModelCapabilities
from homebrew_harness.runs import RunSpec, RunStore
from homebrew_harness.runs.handoff import switch_model, context_message
from homebrew_harness.storage.checkpoint_retirement import workspace_blockers
from tests.support.clients import fake_client


class HandoffTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.store = RunStore(self.root)
        self.addCleanup(self.store.close)

    def registry(self, **kwargs):
        def factory(**_):
            client, _ = fake_client([])
            client.close = lambda: None
            return client

        endpoint = urlunsplit(("https", "model.invalid", "/", "", ""))
        specs = [
            ModelSpec("primary", "fixture", "fixture-credential", endpoint),
            ModelSpec("backup", "fixture", "fixture-credential", endpoint, **kwargs),
        ]
        value = ModelRegistry(specs, lead_default="primary", client_factory=factory)
        self.addCleanup(value.close)
        return value

    def closed(self, registry):
        state = self.store.create(
            RunSpec(
                "Original task constraints",
                model_profile="primary",
                fallback_models=("backup",),
                context_chars=60000,
            )
        )
        key = "lead:" + state["session_id"]
        self.store.runtime.start_session(state["session_id"], "fixture")
        saved = {
            "session_id": state["session_id"],
            "phase": "closed",
            "intent": {
                "original": state["spec"]["prompt"],
                "amendments": ["Preserve API"],
            },
            "input_control": {"revision": 1},
            "messages": [
                {
                    "role": "assistant",
                    "content": "reference",
                    "reasoning_content": "PRIVATE_PROTOCOL_SENTINEL",
                }
            ],
            "usage": {},
            "goal_id": None,
        }
        self.store.runtime.checkpoint(key, saved)
        state.update(
            status="paused",
            checkpoint=key,
            submitted=True,
            bindings={"models": registry.manifest(), "model": {"profile": "primary"}},
            selected_model="primary",
        )
        self.store.runtime.put("runs", state["id"], state)
        return state

    def test_handoff_preserves_run_and_uses_fresh_context(self):
        registry = self.registry()
        before = self.closed(registry)
        result = switch_model(
            self.store,
            before["id"],
            "backup",
            before["revision"],
            registry,
            reason="Use the declared alternate model",
        )
        after = self.store.get(before["id"])
        self.assertNotEqual(after["session_id"], before["session_id"])
        self.assertEqual(
            after["session_refs"], [before["session_id"], after["session_id"]]
        )
        self.assertEqual(after["evidence_session_id"], before["session_id"])
        self.assertEqual(after["deadline_at"], before["deadline_at"])
        record = self.store.runtime.get("run_handoffs", result["handoff_id"])
        self.assertEqual(record["intent"]["amendments"], ["Preserve API"])
        self.assertNotIn("PRIVATE_PROTOCOL_SENTINEL", json.dumps(record))
        self.assertNotIn(
            "PRIVATE_PROTOCOL_SENTINEL", json.dumps(context_message(record))
        )
        self.assertIn(
            "managed_run_unfinished",
            workspace_blockers(
                self.store.runtime.read(), {"records": {}}, before["session_id"]
            ),
        )

    def test_rejects_missing_tools_or_insufficient_context_before_transition(self):
        for options in (
            {"capabilities": ModelCapabilities(tools=False)},
            {"context_chars": 10000},
        ):
            registry = self.registry(**options)
            if options.get("context_chars"):
                # A deliberately large retained requirement must not be dropped to fit.
                state = self.closed(registry)
                key = state["checkpoint"]
                saved = self.store.runtime.get("checkpoints", key)
                saved["intent"]["original"] = "Required constraint. " * 2000
                self.store.runtime.checkpoint(key, saved)
            else:
                state = self.closed(registry)
            with self.assertRaises(ValueError):
                switch_model(
                    self.store,
                    state["id"],
                    "backup",
                    state["revision"],
                    registry,
                    reason="switch",
                )
            self.assertEqual(
                self.store.get(state["id"])["session_id"], state["session_id"]
            )
            state["status"] = "cancelled"
            self.store.runtime.put("runs", state["id"], state)

    def test_revision_policy_and_unfinished_execution_are_not_bypassed(self):
        registry = self.registry()
        state = self.closed(registry)
        for alias, revision in [
            ("unknown", state["revision"]),
            ("backup", state["revision"] + 1),
        ]:
            with self.assertRaises(ValueError):
                switch_model(
                    self.store, state["id"], alias, revision, registry, reason="switch"
                )
        state["cleanup_complete"] = False
        self.store.runtime.put("runs", state["id"], state)
        with self.assertRaises(ValueError):
            switch_model(
                self.store,
                state["id"],
                "backup",
                state["revision"],
                registry,
                reason="switch",
            )

    def test_pending_attention_survives_model_handoff_without_granting_permission(self):
        import time
        from homebrew_harness.runs.requests import HumanRequests, digest

        registry = self.registry()
        state = self.closed(registry)
        saved = self.store.runtime.get("checkpoints", state["checkpoint"])
        request = {
            "id": "req_" + "a" * 32,
            "run_id": state["id"],
            "checkpoint": state["checkpoint"],
            "kind": "attention",
            "tool": None,
            "arguments": None,
            "question": "Which behavior?",
            "reason": "Requirement decision",
            "impact": "API",
            "independent_work": "",
            "state": "pending",
            "revision": 0,
            "decision": None,
            "created_at": time.time(),
            "expires_at": time.time() + 120,
            "context": {
                "spec": digest(state["spec"]),
                "bindings": None,
                "control": state["control_identity"],
                "intent": digest(saved["intent"]),
                "epoch": 0,
                "workspace_identity": state["workspace_identity"],
            },
        }
        self.store.runtime.put("run_requests", request["id"], request)
        switch_model(
            self.store,
            state["id"],
            "backup",
            state["revision"],
            registry,
            reason="switch model",
        )
        answer = HumanRequests(self.store).decide(
            state["id"], request["id"], 0, "answer", "Preserve the API"
        )
        self.assertEqual(answer["state"], "answered")
        self.assertIsNone(answer["tool"])
        self.assertEqual(
            len(self.store.runtime.mailbox("inspect", "user_" + state["id"])), 1
        )

    def test_new_session_resumes_unfinished_lead_assignment_and_original_intent(self):
        from homebrew_harness import HarnessSession, Capabilities
        from homebrew_harness.runs import RunController
        from homebrew_harness.runs.requests import HumanRequests
        from tests.support.clients import reply, call
        import io
        from contextlib import redirect_stdout, redirect_stderr

        endpoint = urlunsplit(("https", "model.invalid", "/", "", ""))
        responses = []

        def catalog():
            def factory(**_):
                client, _ = fake_client([])
                client.chat.completions.create.side_effect = lambda **_: responses.pop(
                    0
                )
                client.close = lambda: None
                return client

            return ModelRegistry(
                [
                    ModelSpec("primary", "fixture", "fixture-credential", endpoint),
                    ModelSpec("backup", "fixture", "fixture-credential", endpoint),
                ],
                lead_default="primary",
                client_factory=factory,
            )

        acceptance = {
            "condition": "Output exists",
            "judge": False,
            "predicates": [
                {
                    "id": "output",
                    "type": "file_contains",
                    "path": "output.txt",
                    "text": "done",
                }
            ],
        }
        spec = RunSpec(
            "Original constraints must survive a model change",
            goal=acceptance,
            stages=({"key": "work", "subject": "Work", "acceptance": acceptance},),
            model_profile="primary",
            fallback_models=("backup",),
            context_chars=60000,
        )
        state = self.store.create(spec)
        task = next(iter(self.store.runtime.items("tasks").values()))
        responses.extend(
            [
                reply(
                    calls=[
                        call(
                            "claim",
                            name="claim_task",
                            arguments=json.dumps({"task_id": task["id"]}),
                        )
                    ]
                ),
                reply(
                    calls=[
                        call(
                            "question",
                            name="request_attention",
                            arguments=json.dumps(
                                {
                                    "question": "Confirm constraint",
                                    "reason": "Need a decision",
                                    "impact": "Output",
                                }
                            ),
                        )
                    ]
                ),
            ]
        )

        def session():
            state = self.store.claim(task["stage"]["run_id"])
            control = RunController(
                self.store, state["id"], state["owner"], state["generation"]
            )
            model_registry = catalog()
            raw, _ = fake_client([])
            harness = HarnessSession(
                raw,
                "fixture",
                self.root,
                model_registry=model_registry,
                lead_model_profile=state["selected_model"],
                capabilities=Capabilities(control.spec.enabled),
                run_control=control,
                context_chars=control.spec.context_chars,
                worker_output_tokens=control.spec.output_tokens,
                shutdown_timeout=1,
            )
            return control, harness

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            control, first = session()
            self.assertEqual(control.execute().status, "waiting")
            first.close()
            self.assertTrue(first.wait_closed(5))
            control.close_heartbeat()
            self.store.ended(control.header, cleanup=True)
            before = self.store.get(control.id)
            registry = catalog()
            try:
                switch_model(
                    self.store,
                    control.id,
                    "backup",
                    before["revision"],
                    registry,
                    reason="Explicit model switch",
                )
            finally:
                registry.close()
            request = HumanRequests(self.store).records(control.id)[0]
            HumanRequests(self.store).decide(
                control.id,
                request["id"],
                request["revision"],
                "answer",
                "Keep the original constraint",
            )
            responses.extend(
                [
                    reply(
                        calls=[
                            call(
                                "write",
                                name="write_file",
                                arguments=json.dumps(
                                    {"path": "output.txt", "content": "done"}
                                ),
                            )
                        ]
                    ),
                    reply(
                        calls=[
                            call(
                                "complete",
                                name="complete_task",
                                arguments=json.dumps({"task_id": task["id"]}),
                            )
                        ]
                    ),
                    reply("Completed"),
                ]
            )
            control, second = session()
            try:
                result = control.execute()
                self.assertEqual(result.status, "completed", result)
                self.assertEqual(second.intent.original, spec.prompt)
                self.assertIn(
                    "Keep the original constraint", second.intent.amendments[-1]
                )
                self.assertEqual(second.lead_binding.profile, "backup")
                self.assertEqual(
                    self.store.runtime.get("tasks", task["id"])["status"], "completed"
                )
            finally:
                second.close()
                second.wait_closed(5)
                control.close_heartbeat()
                self.store.ended(control.header, cleanup=True)
