"""Approval judgment uses the existing real HTTP transports for every protocol."""

import json
import unittest

from homebrew_harness import ModelRegistry, ModelSpec, HarnessSession, Capabilities
from homebrew_harness.execution.model_gateway import ensure_gateway
from homebrew_harness.security.approval_review import ApprovalReviewer
from homebrew_harness.storage.durable_runtime import DurableStore
from tests.integration import test_native_model_protocols as helpers
from tests.support.clients import reply, call


class ApprovalProtocolTests(unittest.TestCase):
    setUp = helpers.NativeProtocolTests.setUp
    service = helpers.NativeProtocolTests.service

    def test_real_subagent_review_uses_its_selected_requester_model(self):
        approved = json.dumps(
            {
                "decision": "approve",
                "reason": "Exact user command",
                "risk": "low",
                "authorization": "explicit",
            }
        )
        parent = self.service(
            [
                reply(
                    calls=[
                        call(
                            "task",
                            name="task",
                            arguments='{"prompt":"Print ok","model_profile":"coder"}',
                        )
                    ]
                ).model_dump(exclude_none=True),
                reply("Done").model_dump(exclude_none=True),
            ]
        )
        worker = self.service(
            [
                reply(calls=[call("shell", command="printf ok")]).model_dump(
                    exclude_none=True
                ),
                reply(approved).model_dump(exclude_none=True),
                reply("Child done").model_dump(exclude_none=True),
            ]
        )
        registry = ModelRegistry(
            [
                ModelSpec(
                    "planner",
                    "planner-fixture",
                    "synthetic-credential",
                    parent.url,
                    transport="http",
                ),
                ModelSpec(
                    "coder",
                    "coder-fixture",
                    "synthetic-credential",
                    worker.url,
                    transport="http",
                ),
            ],
            lead_default="planner",
        )
        session = HarnessSession(
            None,
            "",
            self.root,
            model_registry=registry,
            capabilities=Capabilities({"subagents"}),
            approval_review={"mode": "auto_review", "model_source": "requester"},
            shutdown_timeout=0.2,
        )
        try:
            self.assertEqual(session.run_user("Print ok using a subagent"), "Done")
            self.assertEqual(len(parent.requests), 2)
            self.assertEqual(len(worker.requests), 3)
            self.assertEqual(worker.requests[1]["model"], "coder-fixture")
            self.assertIn(
                "independent approval reviewer", str(worker.requests[1]["messages"])
            )
            audit = next(iter(session.runtime_store.items("approval_reviews").values()))
            self.assertEqual(audit["reviewer_binding"]["profile"], "coder")
            self.assertEqual(audit["state"], "approved")
        finally:
            session.close()
            self.assertTrue(session.wait_closed(5))

    def test_native_protocols_and_usage_are_shared_with_reviewer(self):
        from homebrew_harness.execution.model_protocol import ModelCapabilities

        text = json.dumps(
            {
                "decision": "approve",
                "reason": "Exact authorized action",
                "risk": "low",
                "authorization": "explicit",
            }
        )
        for protocol, answer in (
            ("chat_completions", reply(text).model_dump(exclude_none=True)),
            ("openai_responses", helpers.responses(text)),
            ("anthropic_messages", helpers.anthropic(text)),
            ("dashscope_generation", helpers.generation(text)),
        ):
            with self.subTest(protocol=protocol):
                server = self.service([answer])
                registry = ModelRegistry(
                    [
                        ModelSpec(
                            "safety",
                            "fixture",
                            "synthetic-credential",
                            server.url,
                            protocol=protocol,
                            transport="http",
                            capabilities=ModelCapabilities(tools=False),
                        )
                    ],
                    lead_default="safety",
                )
                binding = registry.select("safety")
                gateway = ensure_gateway(binding.client, workdir=self.root)
                reviewer = ApprovalReviewer(
                    {"mode": "auto_review"},
                    DurableStore(self.root),
                    registry,
                    gateway,
                    lambda: binding,
                )
                try:
                    request = {
                        "id": "request-" + protocol,
                        "revision": 0,
                        "kind": "tool",
                        "tool": "bash",
                        "arguments": {"command": "printf ok"},
                    }
                    context = {
                        "cwd": str(self.root),
                        "intent": {"original": "Print ok", "amendments": []},
                    }
                    self.assertEqual(
                        reviewer.review(
                            request,
                            context,
                            lambda: context,
                            lambda decision, identity: decision,
                        ),
                        "approve",
                    )
                    self.assertEqual(gateway.summary()["calls"], 1)
                    raw = server.requests[0]
                    payload = raw.get("parameters", raw)
                    self.assertFalse(payload.get("tools"))
                    if protocol == "anthropic_messages":
                        self.assertIn(
                            "independent approval reviewer", str(raw["system"])
                        )
                    else:
                        self.assertIn("independent approval reviewer", json.dumps(raw))
                finally:
                    reviewer.close()
                    gateway.close()
                    self.assertTrue(gateway.wait_idle(3))
                    registry.close()

    def test_evidence_turns_preserve_native_reasoning_replay(self):
        (self.root / "script.py").write_text('print("ok")')
        text = json.dumps(
            {
                "decision": "approve",
                "reason": "Reviewed authorized script",
                "risk": "low",
                "authorization": "explicit",
            }
        )
        for protocol, first, last in (
            (
                "openai_responses",
                helpers.responses(tool={"path": "script.py"}),
                helpers.responses(text),
            ),
            (
                "anthropic_messages",
                helpers.anthropic(tool={"path": "script.py"}),
                helpers.anthropic(text),
            ),
            (
                "dashscope_generation",
                helpers.generation(tool={"path": "script.py"}),
                helpers.generation(text),
            ),
        ):
            with self.subTest(protocol=protocol):
                if protocol == "openai_responses":
                    first["output"][1]["name"] = "read_review_file"
                elif protocol == "anthropic_messages":
                    first["content"][1]["name"] = "read_review_file"
                else:
                    first["output"]["choices"][0]["message"]["tool_calls"][0][
                        "function"
                    ]["name"] = "read_review_file"
                server = self.service([first, last])
                registry = ModelRegistry(
                    [
                        ModelSpec(
                            "safety",
                            "fixture",
                            "synthetic-credential",
                            server.url,
                            protocol=protocol,
                            transport="http",
                        )
                    ],
                    lead_default="safety",
                )
                binding = registry.select("safety")
                gateway = ensure_gateway(binding.client, workdir=self.root)
                reviewer = ApprovalReviewer(
                    {"mode": "auto_review"},
                    DurableStore(self.root),
                    registry,
                    gateway,
                    lambda: binding,
                )
                try:
                    request = {
                        "id": "read-" + protocol,
                        "revision": 0,
                        "kind": "tool",
                        "tool": "bash",
                        "arguments": {"command": "python script.py"},
                    }
                    context = {
                        "cwd": str(self.root),
                        "intent": {"original": "Run script.py", "amendments": []},
                    }
                    self.assertEqual(
                        reviewer.review(
                            request,
                            context,
                            lambda: context,
                            lambda decision, identity: decision,
                        ),
                        "approve",
                    )
                    self.assertEqual(len(server.requests), 2)
                    replay = json.dumps(server.requests[1])
                    self.assertIn("print", replay)
                    if protocol == "openai_responses":
                        self.assertIn("opaque-provider-state", replay)
                    if protocol == "anthropic_messages":
                        self.assertIn("unaltered-signature", replay)
                    self.assertNotIn("_protocol_state", replay)
                finally:
                    reviewer.close()
                    gateway.close()
                    self.assertTrue(gateway.wait_idle(3))
                    registry.close()
