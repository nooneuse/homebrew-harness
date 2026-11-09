"""Real HTTP and independent worker/supervisor processes, without paid models."""

import json
import os
import tempfile
import threading
import unittest
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlunsplit

from homebrew_harness.runtime.interaction_config import (
    InteractionAssembler,
    InteractionConfig,
)
from homebrew_harness.runtime.interaction_host import InteractionHost
from homebrew_harness.runtime.task_routing import TaskRoute
from homebrew_harness.runs.detached import detached_status
from homebrew_harness.runs.detached import launch_detached, recover_detached
from homebrew_harness.runs.spec import RunSpec
from tests.runtime.test_interaction import eventually
from tests.support.clients import call, reply


class Service:
    def __init__(self, replies):
        self.replies = deque(replies)
        self.requests = []
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                request = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                with outer.lock:
                    outer.requests.append(request)
                    response = outer.replies.popleft()
                value = response(request) if callable(response) else response
                raw = value.model_dump_json().encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = urlunsplit(
            ("http", "127.0.0.1:" + str(self.server.server_port), "/v1", "", "")
        )

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)


class AutoProcessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "workspace"
        self.root.mkdir()
        self.hosts = []
        self.addCleanup(self.close_hosts)

    def close_hosts(self):
        for host in reversed(self.hosts):
            active = host.state.active()
            if active and (active.get("target") or {}).get("kind") == "run":
                run = host.runs.get(active["target"]["id"])
                if run["status"] not in {"completed", "failed", "cancelled"}:
                    host.runs.control(run["id"], "cancel")
            host.close()
            host.workers.wait(8)

    def config(self, service, **kwargs):
        env = self.base / "fixture.env"
        env.write_text(
            "OPENAI_API_KEY=interaction-fixture-credential\nOPENAI_BASE_URL="
            + service.url
            + "\nOPENAI_MODEL=test-model\n"
        )
        env.chmod(0o600)
        return InteractionConfig(
            env_file=env,
            model_planning=False,
            shutdown_seconds=2,
            total_seconds=30,
            request_seconds=5,
            **kwargs,
        )

    def host(self, service, **kwargs):
        config = kwargs.pop("config", None) or self.config(service)
        with patch.dict(os.environ, {}, clear=True):
            assembler = InteractionAssembler(self.root, config)
        host = InteractionHost(assembler, trusted=True, **kwargs)
        self.hosts.append(host)
        return host

    def service(self, values):
        service = Service(values)
        self.addCleanup(service.close)
        return service

    def test_attached_managed_attention_answer_resumes_real_worker(self):
        service = self.service(
            [
                reply(
                    calls=[
                        call(
                            "question",
                            name="request_attention",
                            arguments=json.dumps(
                                {
                                    "question": "What should empty input return?",
                                    "reason": "The requirement is ambiguous",
                                    "impact": "Determines behavior",
                                }
                            ),
                        )
                    ],
                    finish_reason="tool_calls",
                ),
                reply("Answer received; task complete."),
            ]
        )
        notices = []
        host = self.host(service, announce=notices.append)
        receipt = host.submit(
            "/managed Explain the empty input choice and ask if needed"
        )
        request = eventually(lambda: host.requests() and host.requests()[0], 12)
        self.assertEqual(request["kind"], "attention")
        eventually(
            lambda: any(
                isinstance(n, dict)
                and n.get("requests")
                and n["requests"][0]["id"] == request["id"]
                for n in notices
            ),
            5,
        )
        eventually(lambda: host.focus == host._focus_key(request), 5)
        host.submit("Return an empty string")
        eventually(
            lambda: host.state.get(receipt["task_id"])["state"] == "finished", 12
        )
        self.assertEqual(len(service.requests), 2)
        self.assertIn("Return an empty string", str(service.requests[-1]))

    def test_lost_run_acceptance_return_creates_one_execution(self):
        service = self.service([reply("Managed task complete.")])
        host = self.host(service)
        original = host.runs.create

        def create(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("Fixture acknowledgement lost after commit")

        with patch.object(host.runs, "create", create):
            receipt = host.submit("/managed Explain the repository")
            eventually(
                lambda: host.state.get(receipt["task_id"])["state"] == "finished", 12
            )
        self.assertEqual(len(host.runs.runtime.items("runs")), 1)
        self.assertEqual(len(service.requests), 1)

    def test_observer_failure_does_not_block_exact_answer_or_completion(self):
        service = self.service(
            [
                reply(
                    calls=[
                        call(
                            "question",
                            name="request_attention",
                            arguments=json.dumps(
                                {
                                    "question": "Choose the output",
                                    "reason": "A choice is needed",
                                    "impact": "Determines the output",
                                }
                            ),
                        )
                    ],
                    finish_reason="tool_calls",
                ),
                reply("Task completed."),
            ]
        )

        def broken(_value):
            raise RuntimeError("Fixture UI failure")

        host = self.host(service, announce=broken)
        receipt = host.submit("/managed Explain the chosen output")
        request = eventually(lambda: host.requests() and host.requests()[0], 12)
        eventually(lambda: host.observation_errors, 5)
        self.assertIsNone(host.focus)
        host.respond(request["id"], request["revision"], "answer", answer="Plain text")
        eventually(
            lambda: host.state.get(receipt["task_id"])["state"] == "finished", 12
        )
        self.assertEqual(len(service.requests), 2)

    def test_managed_bash_focus_approves_exact_call_then_consumes_once(self):
        request_ids = []
        service = self.service(
            [
                reply(
                    calls=[call("shell", "printf approved")], finish_reason="tool_calls"
                ),
                lambda _request: reply(
                    calls=[
                        call(
                            "approved",
                            name="execute_approved",
                            arguments=json.dumps({"request_id": request_ids[0]}),
                        )
                    ],
                    finish_reason="tool_calls",
                ),
                reply("Command completed."),
            ]
        )
        host = self.host(service, config=self.config(service, managed_bash=True))
        receipt = host.submit("/managed Run the requested fixed demonstration command")
        request = eventually(lambda: host.requests() and host.requests()[0], 12)
        self.assertEqual(request["kind"], "tool")
        request_ids.append(request["id"])
        host.status()
        host.submit("允许这次操作")
        eventually(
            lambda: host.state.get(receipt["task_id"])["state"] == "finished", 12
        )
        records = host.runs.runtime.items("run_requests")
        self.assertEqual(records[request["id"]]["state"], "consumed")
        self.assertEqual(len(service.requests), 3)

    def test_detached_start_close_and_reattach_preserve_one_run(self):
        entered, release = threading.Event(), threading.Event()

        def wait(_request):
            entered.set()
            release.wait(8)
            return reply("Completed after the interface closed.")

        service = self.service([wait])
        config = self.config(service, detached=True)

        class Router:
            def propose(self, *_args, **_kwargs):
                return TaskRoute(execution_mode="managed", lifetime="detached")

        host = self.host(service, config=config, router=Router())
        receipt = host.submit("Continue after closing this interface")
        task = eventually(
            lambda: (
                host.state.get(receipt["task_id"])
                if host.state.get(receipt["task_id"]).get("target")
                else None
            ),
            12,
        )
        identity = task["target"]["id"]
        self.assertTrue(entered.wait(12))
        eventually(lambda: detached_status(host.runs, identity)["live"], 5)
        self.assertEqual(host.close()["state"], "closed")
        reopened = self.host(service, config=config, router=Router())
        self.assertEqual(reopened.state.active()["target"]["id"], identity)
        release.set()
        eventually(lambda: reopened.runs.get(identity)["status"] == "completed", 12)
        eventually(lambda: reopened.runs.get(identity).get("finalized_at"), 8)
        reopened.status()
        self.assertEqual(len(reopened.runs.runtime.items("runs")), 1)
        self.assertEqual(len(service.requests), 1)

    def test_session_handoff_preserves_original_context_without_replay(self):
        entered, release = threading.Event(), threading.Event()

        def wait(_request):
            entered.set()
            release.wait(8)
            return reply("Known source result.")

        service = self.service([wait, reply("Managed continuation finished.")])
        host = self.host(service)
        receipt = host.submit(
            "Explain this module and preserve every original constraint"
        )
        self.assertTrue(entered.wait(12))
        host.upgrade()
        release.set()
        eventually(
            lambda: host.state.get(receipt["task_id"])["state"] == "finished", 15
        )
        task = host.state.get(receipt["task_id"])
        record = host.runs.runtime.get("run_handoffs", task["handoff_id"])
        self.assertEqual(record["kind"], "session_to_run")
        self.assertEqual(
            record["intent"]["original"],
            "Explain this module and preserve every original constraint",
        )
        self.assertIn("Known source result", str(service.requests[-1]))
        self.assertEqual(len(service.requests), 2)
        self.assertLess(
            host.runs.get(task["target"]["id"])["spec"]["total_seconds"], 30
        )
        source = host.runs.runtime.get("checkpoints", record["source_checkpoint"])
        self.assertEqual(source["phase"], "closed")
        self.assertNotIn("managed_run", source)
        from homebrew_harness.runtime.session import HarnessSession

        obsolete = HarnessSession(
            host.assembler.binding.client,
            host.assembler.binding.model,
            self.root,
            resume_session=record["from_session"],
        )
        try:
            with self.assertRaisesRegex(ValueError, "handed off"):
                obsolete.run_user("Do not resume obsolete work")
        finally:
            obsolete.close()

    def test_handoff_carries_consumed_tests_without_reissuing_grant(self):
        import sys

        entered, release = threading.Event(), threading.Event()

        def wait(_request):
            entered.set()
            release.wait(8)
            return reply("Source work is recorded.")

        decisions = []

        def after_approval(_request):
            request = decisions[0]
            if request["kind"] == "tool":
                return reply(
                    calls=[
                        call(
                            "approved",
                            name="execute_approved",
                            arguments=json.dumps({"request_id": request["id"]}),
                        )
                    ],
                    finish_reason="tool_calls",
                )
            return reply(
                calls=[
                    call(
                        "repeat",
                        name="run_tests",
                        arguments=json.dumps({"name": "checks"}),
                    )
                ],
                finish_reason="tool_calls",
            )

        service = self.service(
            [
                wait,
                reply(
                    calls=[
                        call(
                            "test",
                            name="run_tests",
                            arguments=json.dumps({"name": "checks"}),
                        )
                    ],
                    finish_reason="tool_calls",
                ),
                after_approval,
                reply("All requested work is complete."),
            ]
        )
        config_path = self.base / "tests-fixture.json"
        config_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "recipes": [
                        {
                            "name": "checks",
                            "argv": [sys.executable, "-c", "print('verified')"],
                            "input_globs": ["seed.txt"],
                            "locked_globs": [],
                            "roles": ["lead"],
                            "max_runs": 1,
                            "timeout": 5,
                            "approval_seconds": 30,
                        }
                    ]
                }
            )
        )
        (self.root / "seed.txt").write_text("input")
        host = self.host(
            service,
            config=self.config(
                service, test_config=config_path, allow_tests=("checks",)
            ),
        )
        receipt = host.submit("Implement the task and run the registered checks")
        self.assertTrue(entered.wait(12))
        self.assertEqual(host.session.tests.run("checks")["status"], "passed")
        self.assertEqual(host.session.tests.grants[-1].remaining, 0)
        host.upgrade()
        release.set()
        request = eventually(lambda: host.requests() and host.requests()[0], 15)
        run = host.runs.get(host.state.get(receipt["task_id"])["target"]["id"])
        self.assertEqual(run["test_runs"], 1)
        self.assertEqual(sum(run["test_attempts"].values()), 1)
        self.assertEqual(run["test_grants"]["checks"]["max_runs"], 0)
        decisions.append(request)
        if request["kind"] == "test_grant":
            host.respond(
                request["id"], request["revision"], "approve", runs=1, seconds=20
            )
        else:
            host.respond(request["id"], request["revision"], "approve")
        eventually(
            lambda: host.state.get(receipt["task_id"])["state"] == "finished", 15
        )
        self.assertEqual(host.runs.get(run["id"])["test_runs"], 2)

    def test_uncertain_detached_start_is_fenced_before_a_new_process(self):
        service = self.service([reply("Completed recovered launch.")])
        host = self.host(service)
        run = host.runs.create(RunSpec("Explain the module", total_seconds=30))
        token = "a" * 32
        record = {
            "run_id": run["id"],
            "token": token,
            "phase": "pending",
            "config": host.config.identity(),
        }
        host.runs.runtime.put("run_supervisors", run["id"], record)
        with patch("homebrew_harness.runs.detached.subprocess.Popen") as spawn:
            self.assertEqual(
                launch_detached(host.runs, run["id"], host.config)["state"],
                "startup_pending",
            )
            spawn.assert_not_called()
        recover_detached(host.runs, run["id"], token)
        self.assertEqual(
            launch_detached(host.runs, run["id"], host.config)["state"], "accepted"
        )
        eventually(lambda: host.runs.get(run["id"]).get("finalized_at"), 12)
        self.assertEqual(len(service.requests), 1)

    def test_mcp_catalog_prepares_only_selected_interactive_resources(self):
        import sys
        from tests.support.paths import PROJECT_ROOT

        service = self.service([])
        mcp = self.base / "mcp-fixture.json"
        mcp.write_text(
            json.dumps(
                {
                    "servers": {
                        "docs": {
                            "transport": "stdio",
                            "command": sys.executable,
                            "args": [str(PROJECT_ROOT / "examples/mcp_demo_server.py")],
                            "cwd": str(self.base),
                            "tools": {"search": {"action": "allow", "read_only": True}},
                        }
                    }
                }
            )
        )
        config = self.config(service, allowed=("mcp",), mcp_config=mcp)
        host = self.host(service, config=config)
        session = host.assembler.session(TaskRoute(capabilities=("mcp",)))
        self.addCleanup(session.close)
        self.assertTrue(session.capabilities.has("mcp"))
        session.mcp.connect("docs")
        self.assertTrue(session.mcp.snapshot()[0])
        with self.assertRaises(ValueError):
            host.assembler.run_spec(
                TaskRoute(execution_mode="managed", capabilities=("mcp",)),
                "Use documentation",
            )
