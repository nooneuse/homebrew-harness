"""Different model endpoints run real isolated child and teammate loops."""

import io
import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from collections import deque
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlunsplit

from homebrew_harness import Capabilities, HarnessSession, ModelRegistry, ModelSpec
from homebrew_harness.execution.model_gateway import (
    Deadline,
    GatewayLimits,
    ModelDeadlineExceeded,
)
from homebrew_harness.runtime.runtime_state import RecoveryRequired
from homebrew_harness.runtime.turns import run_subagent
from tests.support.clients import call, fake_client, reply


class LocalModels:
    def __init__(self):
        self.responses = deque()
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(data)
                if not outer.responses:
                    self.send_error(500)
                    return
                response = outer.responses.popleft().model_dump(
                    mode="json", exclude_none=True
                )
                raw = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = urlunsplit(
            ("http", "127.0.0.1:" + str(self.server.server_port), "/v1", "", "")
        )

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class ModelSelectionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        for capture in (redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            capture.__enter__()
            self.addCleanup(capture.__exit__, None, None, None)

    def local_registry(self, **defaults):
        lead, worker = LocalModels(), LocalModels()
        self.addCleanup(lead.close)
        self.addCleanup(worker.close)
        registry = ModelRegistry(
            [
                ModelSpec(
                    "planner",
                    "strong-fixture",
                    "planner-synthetic-credential",
                    lead.endpoint,
                ),
                ModelSpec(
                    "coder",
                    "ordinary-fixture",
                    "coder-synthetic-credential",
                    worker.endpoint,
                    "Focused coding",
                    context_chars=20000,
                    max_output_tokens=2048,
                ),
            ],
            lead_default="planner",
            **defaults,
        )
        return registry, lead, worker

    def session(self, registry, *, capabilities=None, **kwargs):
        session = HarnessSession(
            None,
            "",
            self.root,
            model_registry=registry,
            capabilities=capabilities or Capabilities({"subagents"}),
            shutdown_timeout=0.05,
            **kwargs,
        )
        self.addCleanup(lambda: self.assertTrue(session.wait_closed(5)))
        self.addCleanup(session.close)
        return session

    def wait_member(self, member):
        deadline = time.monotonic() + 4
        while member.status not in {"idle", "failed"} and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        self.assertEqual(member.status, "idle")

    def test_real_http_lead_delegates_to_another_endpoint_and_model(self):
        registry, lead, worker = self.local_registry()
        lead.responses.extend(
            [
                reply(
                    calls=[
                        call(
                            "delegate",
                            name="task",
                            arguments=json.dumps(
                                {
                                    "prompt": "Write the declared output",
                                    "model_profile": "coder",
                                }
                            ),
                        )
                    ]
                ),
                reply("Lead reviewed the result"),
            ]
        )
        worker.responses.extend(
            [
                reply(
                    calls=[
                        call(
                            "write",
                            name="write_file",
                            arguments='{"path":"result.txt","content":"child-output"}',
                        )
                    ]
                ),
                reply("Output saved"),
            ]
        )
        session = self.session(registry)
        self.assertEqual(
            session.run_user("Implement locally; preserve all constraints"),
            "Lead reviewed the result",
        )
        self.assertEqual((self.root / "result.txt").read_text(), "child-output")
        self.assertEqual([r["model"] for r in lead.requests], ["strong-fixture"] * 2)
        self.assertEqual(
            [r["model"] for r in worker.requests], ["ordinary-fixture"] * 2
        )
        self.assertEqual(worker.requests[0]["max_tokens"], 2048)
        self.assertFalse(
            any(
                m.get("tool_calls") and m["tool_calls"][0]["id"] == "delegate"
                for m in worker.requests[0]["messages"]
            )
        )
        self.assertTrue(
            any(
                m.get("name") == "inherited_task_constraints"
                for m in worker.requests[0]["messages"]
            )
        )
        schemas = {t["function"]["name"]: t for t in lead.requests[0]["tools"]}
        self.assertEqual(
            schemas["task"]["function"]["parameters"]["properties"]["model_profile"][
                "enum"
            ],
            ["planner", "coder"],
        )
        self.assertEqual(session.gateway.summary()["by_role"]["subagent"]["calls"], 2)
        saved = session.runtime_store.items("checkpoints")
        child = next(v for k, v in saved.items() if k.startswith("sub:"))
        self.assertEqual(child["model_binding"]["profile"], "coder")

    def test_real_http_teammate_uses_exact_role_default_then_lead_receives_result(self):
        registry, lead, worker = self.local_registry(teammate_roles={"coder": "coder"})
        session = self.session(registry, capabilities=Capabilities({"teams"}))
        session.run_user("/usage")
        task = session.team.store.create("Write team output")
        args = {
            "name": "builder",
            "role": "coder",
            "prompt": "Implement task",
            "task_id": task.id,
        }
        lead.responses.extend(
            [
                reply(
                    calls=[
                        call("spawn", name="spawn_teammate", arguments=json.dumps(args))
                    ]
                ),
                reply("Work delegated"),
                reply("Received worker result"),
            ]
        )
        worker.responses.extend(
            [
                reply(
                    calls=[
                        call(
                            "write",
                            name="write_file",
                            arguments='{"path":"team.txt","content":"team-output"}',
                        ),
                        call(
                            "complete",
                            name="complete_task",
                            arguments=json.dumps({"task_id": task.id}),
                        ),
                    ]
                ),
                reply("Task completed"),
            ]
        )
        from homebrew_harness.security.permissions import PermissionDecision

        with patch(
            "homebrew_harness.security.permissions.ask_user",
            return_value=PermissionDecision("allow", "fixture host approval"),
        ):
            session.run_user("Implement using the worker")
        member = session.team.members["builder"]
        self.wait_member(member)
        self.assertEqual(member.model_binding.profile, "coder")
        self.assertEqual(member.compactor.model, "ordinary-fixture")
        self.assertEqual((self.root / "team.txt").read_text(), "team-output")
        self.assertEqual(session.team.store.load(task.id).status, "completed")
        session.drain(timeout=2)
        self.assertEqual(worker.requests[0]["model"], "ordinary-fixture")
        self.assertTrue(all(r["model"] == "strong-fixture" for r in lead.requests))
        self.assertEqual(
            session.team.checkpoint()["members"]["builder"]["model_binding"]["profile"],
            "coder",
        )

    def test_subagent_omission_inherits_without_creating_other_clients(self):
        registry, lead, worker = self.local_registry()
        lead.responses.extend(
            [
                reply(
                    calls=[
                        call(
                            "delegate",
                            name="task",
                            arguments='{"prompt":"Answer briefly"}',
                        )
                    ]
                ),
                reply("child answer"),
                reply("parent answer"),
            ]
        )
        session = self.session(registry)
        self.assertEqual(session.run_user("Use a child"), "parent answer")
        self.assertEqual(worker.requests, [])
        self.assertEqual(list(registry._clients), ["planner"])

    def test_unknown_profile_and_extra_connection_fields_do_not_run_children(self):
        registry, lead, worker = self.local_registry()
        lead.responses.extend(
            [
                reply(
                    calls=[
                        call(
                            "unknown",
                            name="task",
                            arguments='{"prompt":"Do work","model_profile":"invented"}',
                        ),
                        call(
                            "override",
                            name="task",
                            arguments='{"prompt":"Do work","model_profile":"coder","base_url":"invented"}',
                        ),
                    ]
                ),
                reply("Corrected selection"),
            ]
        )
        session = self.session(registry)
        session.run_user("Try an invalid selection")
        self.assertEqual(worker.requests, [])
        self.assertFalse(
            any(
                k.startswith("sub:") for k in session.runtime_store.items("checkpoints")
            )
        )

    def test_selected_subagent_compaction_uses_selected_endpoint(self):
        registry, lead, worker = self.local_registry()
        lead.responses.extend(
            [
                reply(
                    calls=[
                        call(
                            "delegate",
                            name="task",
                            arguments='{"prompt":"Compress once then answer","model_profile":"coder"}',
                        )
                    ]
                ),
                reply("Lead answer"),
            ]
        )
        worker.responses.extend(
            [
                reply(calls=[call("compact", name="compact", arguments="{}")]),
                reply("Summary of completed compression"),
                reply("Worker answer"),
            ]
        )
        session = self.session(registry)
        session.run_user("Delegate compression")
        self.assertEqual(len(worker.requests), 3)
        self.assertTrue(all(r["model"] == "ordinary-fixture" for r in worker.requests))
        self.assertEqual(
            session.gateway.summary()["by_purpose"]["compaction"]["calls"], 1
        )

    def test_resume_refuses_changed_model_configuration_and_accepts_key_rotation(self):
        registry, lead, worker = self.local_registry()
        lead.responses.append(reply("Done"))
        session = self.session(registry)
        session.run_user("Save current state")
        sid = session.session_id
        session.close()
        self.assertTrue(session.wait_closed(4))
        specs = list(registry.specs.values())
        changed = ModelRegistry(
            [
                replace(s, model="changed-fixture") if s.name == "coder" else s
                for s in specs
            ],
            lead_default="planner",
        )
        other = self.session(changed, resume_session=sid)
        with self.assertRaises(RecoveryRequired):
            other.run_user("/usage")
        other.close()
        self.assertTrue(other.wait_closed(4))
        rotated = ModelRegistry(
            [replace(s, api_key="rotated-synthetic-credential") for s in specs],
            lead_default="planner",
        )
        resumed = self.session(rotated, resume_session=sid)
        resumed.run_user("/usage")
        self.assertEqual(resumed.session_id, sid)

    def test_spawn_binding_change_after_approval_and_changed_member_resume_are_refused(
        self,
    ):
        registry, lead, worker = self.local_registry()
        session = self.session(registry, capabilities=Capabilities({"teams"}))
        session.run_user("/usage")
        task = session.team.store.create("Scoped task")
        args = dict(
            name="builder",
            role="coder",
            prompt="Work",
            task_id=task.id,
            model_profile="coder",
        )
        session.team.authorize_spawn(args)
        original = session.team.model_selector
        session.team.model_selector = lambda *_args: registry.select("planner")
        with self.assertRaises(ValueError):
            session.team.spawn(**args)
        self.assertEqual(session.team.store.load(task.id).status, "pending")
        self.assertNotIn("builder", session.team.members)
        session.team.model_selector = original
        session.team.recovered_members["builder"] = {
            "model_binding": registry.select("planner").identity()
        }
        with self.assertRaises(RecoveryRequired):
            session.team.spawn(**args)
        self.assertEqual(session.team.store.load(task.id).status, "pending")

    def test_selected_model_deadline_does_not_execute_late_tools(self):
        parent, _ = fake_client([])
        gate = threading.Event()
        self.addCleanup(gate.set)
        child = SimpleNamespace(
            close=Mock(),
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=Mock(
                        side_effect=lambda **_kwargs: (
                            gate.wait(2),
                            reply(
                                calls=[
                                    call(
                                        "late",
                                        name="write_file",
                                        arguments='{"path":"late.txt","content":"no"}',
                                    )
                                ]
                            ),
                        )[1]
                    )
                )
            ),
        )
        endpoint = urlunsplit(("http", "127.0.0.1:9", "/v1", "", ""))
        registry = ModelRegistry(
            [ModelSpec("coder", "ordinary-fixture", "synthetic-credential", endpoint)],
            client_factory=Mock(return_value=child),
        )
        session = HarnessSession(
            parent,
            "parent",
            self.root,
            model_registry=registry,
            capabilities=Capabilities({"subagents"}),
            shutdown_timeout=0.02,
        )
        try:
            with session.gateway.scope(deadline=Deadline.after(0.08)):
                with self.assertRaises(ModelDeadlineExceeded):
                    run_subagent(
                        "Write",
                        self.root,
                        client=session.client,
                        model=session.model,
                        model_registry=registry,
                        model_profile="coder",
                    )
            session.close()
            child.close.assert_not_called()
            self.assertFalse(session.wait_closed(0.02))
            gate.set()
            self.assertTrue(session.wait_closed(4))
            child.close.assert_called_once()
            self.assertFalse((self.root / "late.txt").exists())
            self.assertEqual(child.chat.completions.create.call_count, 1)
        finally:
            gate.set()
            session.close()
            self.assertTrue(session.wait_closed(4))

    def test_multiple_selected_clients_share_gateway_concurrency(self):
        parent, _ = fake_client([])
        lock = threading.Lock()
        active = peak = 0

        def execute(**_kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            threading.Event().wait(0.04)
            with lock:
                active -= 1
            return reply("Child result")

        clients = [
            SimpleNamespace(
                close=Mock(),
                chat=SimpleNamespace(
                    completions=SimpleNamespace(create=Mock(side_effect=execute))
                ),
            )
            for _ in range(2)
        ]
        endpoint = urlunsplit(("http", "127.0.0.1:9", "/v1", "", ""))
        registry = ModelRegistry(
            [
                ModelSpec(name, name, "synthetic-credential", endpoint)
                for name in ("a", "b")
            ],
            client_factory=Mock(side_effect=clients),
        )
        session = HarnessSession(
            parent,
            "parent",
            self.root,
            model_registry=registry,
            gateway_limits=GatewayLimits(concurrency=1),
            shutdown_timeout=0.03,
        )
        try:

            def run(alias):
                with session.gateway.scope(deadline=Deadline.after(3)):
                    return run_subagent(
                        "Read only",
                        self.root,
                        client=session.client,
                        model=session.model,
                        model_registry=registry,
                        model_profile=alias,
                    )

            with ThreadPoolExecutor(max_workers=2) as workers:
                outputs = list(workers.map(run, ("a", "b")))
            self.assertTrue(
                all(json.loads(v)["status"] == "completed" for v in outputs)
            )
            self.assertEqual(peak, 1)
            self.assertEqual(session.gateway.summary()["calls"], 2)
        finally:
            session.close()
            self.assertTrue(session.wait_closed(4))

    def test_changing_model_does_not_expand_child_tool_access(self):
        registry, lead, worker = self.local_registry()
        worker.responses.extend(
            [
                reply(
                    calls=[
                        call(
                            "write",
                            name="write_file",
                            arguments='{"path":"forbidden.txt","content":"no"}',
                        )
                    ]
                ),
                reply("Write unavailable"),
            ]
        )
        session = self.session(registry)
        result = run_subagent(
            "Read only",
            self.root,
            client=session.client,
            model=session.model,
            model_registry=registry,
            model_profile="coder",
            tool_allowlist=frozenset({"read_file"}),
        )
        self.assertEqual(json.loads(result)["status"], "completed")
        self.assertFalse((self.root / "forbidden.txt").exists())
        self.assertEqual(
            [t["function"]["name"] for t in worker.requests[0]["tools"]], ["read_file"]
        )

    def test_provider_failure_never_falls_back_or_replays_child(self):
        registry, lead, worker = self.local_registry()
        lead.responses.extend(
            [
                reply(
                    calls=[
                        call(
                            "delegate",
                            name="task",
                            arguments='{"prompt":"Do work","model_profile":"coder"}',
                        )
                    ]
                ),
                reply("Report failure"),
            ]
        )
        # Empty worker response queue produces one genuine SDK server error.
        session = self.session(registry)
        self.assertEqual(session.run_user("Delegate once"), "Report failure")
        self.assertEqual(len(worker.requests), 1)
        self.assertEqual(len(lead.requests), 2)

    def test_registry_client_close_failure_preserves_session_lease_and_retries(self):
        parent, _ = fake_client([])
        child, _ = fake_client([reply("Child answer")])
        child.close = Mock(side_effect=[OSError("fixture"), None])
        endpoint = urlunsplit(("http", "127.0.0.1:9", "/v1", "", ""))
        registry = ModelRegistry(
            [ModelSpec("coder", "ordinary-fixture", "synthetic-credential", endpoint)],
            client_factory=Mock(return_value=child),
        )
        session = HarnessSession(
            parent, "parent", self.root, model_registry=registry, shutdown_timeout=0.03
        )
        try:
            session.run_user("/usage")
            run_subagent(
                "Answer",
                self.root,
                client=session.client,
                model=session.model,
                model_registry=registry,
                model_profile="coder",
            )
            session.close()
            self.assertTrue(session._shutdown.workers.wait(2))
            self.assertTrue(session.workspace.leases.held("session"))
            self.assertTrue(session.shutdown_status()["retryable"])
            session.close()
            self.assertTrue(session.wait_closed(4))
            self.assertEqual(child.close.call_count, 2)
        finally:
            session.close()
            self.assertTrue(session.wait_closed(4))

    def test_cli_external_registry_selects_lead_without_legacy_connection_variables(
        self,
    ):
        import homebrew_harness.cli

        registry, lead, worker = self.local_registry()
        config_temp = tempfile.TemporaryDirectory()
        self.addCleanup(config_temp.cleanup)
        external = Path(config_temp.name)
        config = {
            "version": 2,
            "connections": {name: {"protocol": "chat_completions", "api_key_env": name.upper() + "_KEY", "base_url_env": name.upper() + "_ENDPOINT"} for name in registry.specs},
            "profiles": {
                name: {
                    "connection": name,
                    "model_env": name.upper() + "_MODEL",
                }
                for name in registry.specs
            },
            "defaults": {"lead": "planner"},
        }
        path = external / "models.json"
        path.write_text(json.dumps(config))
        env = {
            key: value
            for name, s in registry.specs.items()
            for key, value in [
                (name.upper() + "_MODEL", s.model),
                (name.upper() + "_KEY", s.api_key),
                (name.upper() + "_ENDPOINT", s.base_url),
            ]
        }
        worker.responses.append(reply("CLI selected alternate Lead"))
        with patch.dict(os.environ, env, clear=True):
            status = homebrew_harness.cli.main(
                [
                    "--cwd",
                    str(self.root),
                    "--models-config",
                    str(path),
                    "--lead-model",
                    "coder",
                    "--env-file",
                    str(external / "absent.env"),
                    "Answer briefly",
                ]
            )
        self.assertEqual(status, 0)
        self.assertEqual(lead.requests, [])
        self.assertEqual(worker.requests[0]["model"], "ordinary-fixture")
        registry.close()

    def test_inherited_subagent_preserves_lead_output_setting(self):
        registry, lead, worker = self.local_registry()
        replacement = ModelRegistry(
            [
                replace(s, max_output_tokens=1024) if s.name == "planner" else s
                for s in registry.specs.values()
            ],
            lead_default="planner",
        )
        lead.responses.extend(
            [
                reply(
                    calls=[
                        call(
                            "delegate",
                            name="task",
                            arguments='{"prompt":"Answer briefly"}',
                        )
                    ]
                ),
                reply("Child answer"),
                reply("Lead answer"),
            ]
        )
        session = self.session(replacement)
        session.run_user("Delegate once")
        self.assertEqual([r["max_tokens"] for r in lead.requests], [1024] * 3)
        self.assertEqual(worker.requests, [])

    def test_explicit_teammate_model_overrides_role_default(self):
        registry, lead, worker = self.local_registry(teammate_roles={"coder": "coder"})
        session = self.session(registry, capabilities=Capabilities({"teams"}))
        session.run_user("/usage")
        lead.responses.append(reply("Member result"))
        args = dict(
            name="reviewer", role="coder", prompt="Review plan", model_profile="planner"
        )
        session.team.authorize_spawn(args)
        session.team.spawn(**args)
        self.wait_member(session.team.members["reviewer"])
        self.assertEqual(
            session.team.members["reviewer"].model_binding.profile, "planner"
        )
        self.assertEqual(worker.requests, [])

    def test_teammate_resume_restores_context_with_the_same_model_binding(self):
        registry, lead, worker = self.local_registry()
        session = self.session(registry, capabilities=Capabilities({"teams"}))
        session.run_user("/usage")
        args = dict(
            name="builder", role="coder", prompt="Focused task", model_profile="coder"
        )
        worker.responses.append(reply("Unique saved member fact"))
        session.team.authorize_spawn(args)
        session.team.spawn(**args)
        self.wait_member(session.team.members["builder"])
        sid = session.session_id
        session.close()
        self.assertTrue(session.wait_closed(4))
        worker.responses.append(reply("Resumed member result"))
        same = ModelRegistry(list(registry.specs.values()), lead_default="planner")
        resumed = self.session(
            same, capabilities=Capabilities({"teams"}), resume_session=sid
        )
        resumed.run_user("/usage")
        resumed.team.authorize_spawn(args)
        resumed.team.spawn(**args)
        self.wait_member(resumed.team.members["builder"])
        self.assertTrue(
            any(
                m.get("content") == "Unique saved member fact"
                for m in worker.requests[-1]["messages"]
            )
        )
        self.assertEqual(resumed.team.members["builder"].model_binding.profile, "coder")
