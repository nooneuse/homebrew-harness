"""Direct CLIs select real protocol transports from the host model catalog."""

import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from homebrew_harness.cli import main
from homebrew_harness.execution.model_connections import read_environment
from homebrew_harness.orchestration.workflow_runtime import (
    WorkflowRegistry,
    WorkflowSpec,
)


class ProtocolService:
    def __init__(self, protocol):
        self.protocol, self.requests = protocol, []
        self.closed = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append((self.path, body, dict(self.headers)))
                rows = (
                    body.get("messages")
                    or (body.get("input") or {}).get("messages", [])
                    if isinstance(body.get("input"), dict)
                    else body.get("messages") or body.get("input", [])
                )
                tool_mode = bool(
                    body.get("tools") or body.get("parameters", {}).get("tools")
                )
                done = any(
                    r.get("role") == "tool"
                    or r.get("type") == "function_call_output"
                    or any(
                        isinstance(b, dict) and b.get("type") == "tool_result"
                        for b in r.get("content", [])
                        if isinstance(r.get("content"), list)
                    )
                    for r in rows
                )
                text = json.dumps(rows)
                action = None
                if tool_mode and not done:
                    if "input.json" in text:
                        action = {
                            "path": "answer.json",
                            "content": '{"count":3,"sum":16}',
                        }
                    elif "module.py" in text:
                        action = {
                            "path": "module.py",
                            "content": "# preserved\ndef add(a, b):\n    return a + b\n",
                        }
                    else:
                        action = {"path": "result.txt", "content": "ready"}
                content = "Done" if tool_mode else '{"value":"ok"}'
                calls = (
                    [
                        {
                            "id": "fixture-call",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps(action),
                            },
                        }
                    ]
                    if action
                    else []
                )
                if outer.protocol == "openai_responses":
                    output = (
                        [
                            {
                                "type": "function_call",
                                "id": "fc_fixture",
                                "call_id": "fixture-call",
                                "name": "write_file",
                                "arguments": json.dumps(action),
                            }
                        ]
                        if action
                        else [
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": content}],
                            }
                        ]
                    )
                    value = {
                        "id": "response_fixture",
                        "status": "completed",
                        "output": output,
                        "usage": {"input_tokens": 10, "output_tokens": 5},
                    }
                elif outer.protocol == "anthropic_messages":
                    blocks = (
                        [
                            {
                                "type": "tool_use",
                                "id": "fixture-call",
                                "name": "write_file",
                                "input": action,
                            }
                        ]
                        if action
                        else [{"type": "text", "text": content}]
                    )
                    value = {
                        "id": "message_fixture",
                        "content": blocks,
                        "stop_reason": "tool_use" if action else "end_turn",
                        "usage": {"input_tokens": 10, "output_tokens": 5},
                    }
                else:
                    choice = {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None if action else content,
                            **({"tool_calls": calls} if action else {}),
                        },
                        "finish_reason": "tool_calls" if action else "stop",
                    }
                    value = (
                        {
                            "output": {"choices": [choice]},
                            "usage": {"input_tokens": 10, "output_tokens": 5},
                        }
                        if outer.protocol == "dashscope_generation"
                        else {
                            "id": "fixture",
                            "object": "chat.completion",
                            "created": 0,
                            "model": body["model"],
                            "choices": [choice],
                            "usage": {
                                "prompt_tokens": 10,
                                "completion_tokens": 5,
                                "total_tokens": 15,
                            },
                        }
                    )
                data = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = (
            "http:"
            + "//"
            + self.server.server_address[0]
            + ":"
            + str(self.server.server_address[1])
            + "/v1"
        )

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class DirectModelCatalogTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.root = self.base / "workspace"
        self.root.mkdir()

    def config(self, protocol, service):
        env = self.base / "model.env"
        env.write_text(
            "CATALOG_TEST_KEY=fixture-credential\nCATALOG_TEST_URL="
            + service.url
            + "\nCATALOG_TEST_MODEL=fixture-"
            + protocol
            + "\n"
        )
        env.chmod(0o600)
        path = self.base / "models.json"
        path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "connections": {
                        "fixture": {
                            "protocol": protocol,
                            "api_key_env": "CATALOG_TEST_KEY",
                            "base_url_env": "CATALOG_TEST_URL",
                        }
                    },
                    "profiles": {
                        "selected": {
                            "connection": "fixture",
                            "model_env": "CATALOG_TEST_MODEL",
                            "context_chars": 30000,
                            "max_output_tokens": 1024,
                            "options": {"temperature": 0.2},
                            "capabilities": {"tools": True, "json_schema": True},
                        }
                    },
                    "defaults": {"lead": "selected"},
                }
            )
        )
        return path, env

    def test_four_protocols_run_workflow_and_live_benchmark_from_cli(self):
        schema = {
            "type": "object",
            "properties": {"value": {"const": "ok"}},
            "required": ["value"],
            "additionalProperties": False,
        }

        async def script(ctx, _args):
            return await ctx.agent("Produce the requested object", schema=schema)

        for protocol in (
            "chat_completions",
            "openai_responses",
            "dashscope_generation",
            "anthropic_messages",
        ):
            with self.subTest(protocol=protocol):
                service = ProtocolService(protocol)
                self.addCleanup(service.close)
                config, env = self.config(protocol, service)
                argsfile = self.base / "args.json"
                argsfile.write_text("{}")
                registry = WorkflowRegistry(
                    [WorkflowSpec("probe", "probe", script, {"type": "object"})]
                )
                output, errors = io.StringIO(), io.StringIO()
                with (
                    patch.dict(os.environ, {"NO_PROXY": "127.0.0.1,localhost"}),
                    patch(
                        "homebrew_harness.orchestration.workflows.default_registry",
                        return_value=registry,
                    ),
                    redirect_stdout(output),
                    redirect_stderr(errors),
                ):
                    self.assertEqual(
                        main(
                            [
                                "--cwd",
                                str(self.root),
                                "workflow",
                                "--models-config",
                                str(config),
                                "--model",
                                "selected",
                                "--env-file",
                                str(env),
                                "run",
                                "probe",
                                "--args-file",
                                str(argsfile),
                            ]
                        ),
                        0,
                        errors.getvalue(),
                    )
                self.assertEqual(
                    json.loads(output.getvalue())["result"], {"value": "ok"}
                )
                output, errors = io.StringIO(), io.StringIO()
                with (
                    patch.dict(os.environ, {"NO_PROXY": "127.0.0.1,localhost"}),
                    redirect_stdout(output),
                    redirect_stderr(errors),
                ):
                    self.assertEqual(
                        main(
                            [
                                "--cwd",
                                str(self.root),
                                "admin",
                                "benchmark",
                                "--live",
                                "--models-config",
                                str(config),
                                "--model",
                                "selected",
                                "--env-file",
                                str(env),
                            ]
                        ),
                        0,
                        errors.getvalue(),
                    )
                report = json.loads(output.getvalue())
                self.assertTrue(report["passed"], report)
                self.assertEqual(report["model_profile"], "selected")
                self.assertTrue(
                    all(
                        body["model"] == "fixture-" + protocol
                        for _, body, _ in service.requests
                    )
                )
                first = service.requests[0][1]
                sampling = first.get("parameters", first)
                self.assertEqual(sampling["temperature"], 0.2)
                allowance = sampling.get("max_tokens", first.get("max_output_tokens"))
                self.assertLessEqual(allowance, 1024)
                self.assertNotIn("CATALOG_TEST_KEY", os.environ)
                service.close()

    def test_offline_and_bad_alias_do_not_request_a_model(self):
        service = ProtocolService("openai_responses")
        self.addCleanup(service.close)
        config, env = self.config("openai_responses", service)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(
                main(
                    [
                        "--cwd",
                        str(self.root),
                        "admin",
                        "benchmark",
                        "--models-config",
                        str(config),
                        "--model",
                        "selected",
                    ]
                ),
                1,
            )
            self.assertEqual(
                main(
                    [
                        "--cwd",
                        str(self.root),
                        "workflow",
                        "--models-config",
                        str(config),
                        "--env-file",
                        str(env),
                        "--model",
                        "unknown",
                        "run",
                        "review-changes",
                        "--changes-file",
                        str(env),
                    ]
                ),
                1,
            )
            self.assertEqual(
                main(["workflow", "--models-config", str(config), "list"]), 0
            )
        self.assertEqual(service.requests, [])

    def test_private_environment_and_export_precedence_are_preserved(self):
        env = self.base / "config.env"
        env.write_text("CATALOG_TEST_MODEL=file-value\n")
        env.chmod(0o600)
        with patch.dict(os.environ, {"CATALOG_TEST_MODEL": "export-value"}):
            self.assertEqual(
                read_environment(env, self.root, private=True)["CATALOG_TEST_MODEL"],
                "export-value",
            )
        env.chmod(0o644)
        with self.assertRaises(ValueError):
            read_environment(env, self.root, private=True)
        link = self.base / "link.env"
        link.symlink_to(env)
        with self.assertRaises(ValueError):
            read_environment(link, self.root, private=True)
