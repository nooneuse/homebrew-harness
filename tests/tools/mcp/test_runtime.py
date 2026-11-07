"""Real stdio/HTTP protocol tests plus tool-pool and authorization contracts."""

from __future__ import annotations
from tests.support.paths import PROJECT_ROOT
from homebrew_harness.runtime.capabilities import Capabilities

import copy
import io
import json
import os
import select
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.support.tools import execute_fixture_tool
import homebrew_harness.runtime.hooks
import homebrew_harness.runtime.loop
import homebrew_harness.runtime.turns
from homebrew_harness.tools.mcp.runtime import (
    MCPError,
    MCPRuntime,
    ServerSpec,
    ToolPolicy,
    load_specs,
    validate_mcp_schema,
)
from homebrew_harness.security.permissions import noninteractive_permissions
from homebrew_harness.context.skill_loader import SkillLoader
from homebrew_harness.orchestration.teams import TeamManager, TeammateRuntime
from tests.support.clients import call, fake_client, reply


SERVER = PROJECT_ROOT / "examples" / "mcp_demo_server.py"


class MCPConfigurationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / "mcp.json"

    def load(self, data):
        self.path.write_text(json.dumps(data))
        return load_specs(self.path)

    def test_missing_default_and_valid_host_config_are_lazy(self):
        self.assertEqual(load_specs(self.path, missing_ok=True), {})
        specs = self.load(
            {
                "servers": {
                    "docs": {
                        "transport": "stdio",
                        "command": sys.executable,
                        "args": [str(SERVER)],
                        "cwd": ".",
                        "env": {"MCP_TOKEN": {"env": "DEMO_TOKEN"}},
                        "tools": {"search": {"action": "allow", "read_only": True}},
                    }
                }
            }
        )
        with MCPRuntime(specs, workdir=self.root) as runtime:
            self.assertIsNone(runtime._thread)
            self.assertEqual(runtime.snapshot(), ([], {}))
            self.assertEqual(specs["docs"].cwd, self.root)
            self.assertEqual(specs["docs"].tools["search"], ToolPolicy("allow", True))

    def test_invalid_transport_config_and_policy_flags_are_rejected(self):
        invalid = [
            {"transport": []},
            {"transport": "stdio", "command": " "},
            {"transport": "stdio", "command": "python", "timeout_seconds": True},
            {"transport": "stdio", "command": "python", "args": "bad"},
            {
                "transport": "stdio",
                "command": "python",
                "tools": {"read": {"action": {}}},
            },
            {
                "transport": "stdio",
                "command": "python",
                "tools": {"read": {"read_only": "true"}},
            },
            {
                "transport": "stdio",
                "command": "python",
                "env": {"OPENAI_API_KEY": "fixture"},
            },
            {"transport": "streamable_http", "url": "file:///private/file"},
            {
                "transport": "streamable_http",
                "url": "http://user:password@example.invalid/mcp",
            },
            {"transport": "streamable_http", "url": "http://localhost:bad/mcp"},
            {
                "transport": "streamable_http",
                "url": "http://localhost/mcp",
                "command": "python",
            },
        ]
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(MCPError):
                self.load({"servers": {"docs": data}})

    def test_normalized_server_collisions_and_unknown_config_fields_are_rejected(self):
        server = {"transport": "stdio", "command": sys.executable}
        with self.assertRaises(MCPError):
            self.load({"servers": {"docs.one": server, "docs_one": server}})
        with self.assertRaises(MCPError):
            self.load({"servers": {"docs": {**server, "unregistered": True}}})

    def test_schema_annotations_are_redacted_before_model_discovery(self):
        runtime = MCPRuntime({}, workdir=Path.cwd())
        self.addCleanup(runtime.close)
        private = "fixture-schema-private-value"
        connection = SimpleNamespace(
            spec=ServerSpec("docs", "stdio"), secrets=[private]
        )
        raw = {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": private,
                    "default": private,
                    "examples": [private],
                }
            },
            "required": ["query"],
        }
        binding = runtime._make_bindings(
            connection, [{"name": "read", "description": private, "inputSchema": raw}]
        )["mcp__docs__read"]
        self.assertNotIn(private, json.dumps(binding.definition))
        self.assertEqual(
            binding.definition["function"]["parameters"]["properties"]["query"][
                "default"
            ],
            "[redacted]",
        )
        self.assertEqual(raw["properties"]["query"]["default"], private)
        binding.validate({"query": "ordinary input"})

    def test_schema_references_stay_local_and_complex_inputs_are_supported(self):
        schema, validator = validate_mcp_schema(
            {
                "type": "object",
                "$defs": {
                    "Item": {
                        "type": "object",
                        "properties": {"value": {"type": "number", "minimum": 1}},
                        "required": ["value"],
                    }
                },
                "properties": {
                    "item": {"anyOf": [{"$ref": "#/$defs/Item"}, {"type": "null"}]},
                    "$ref": {"type": "string"},
                },
                "additionalProperties": False,
            }
        )
        self.assertTrue(
            validator.is_valid({"item": {"value": 2.5}, "$ref": "ordinary property"})
        )
        self.assertTrue(validator.is_valid({"item": None}))
        self.assertFalse(validator.is_valid({"item": {"value": 0}}))
        self.assertFalse(validator.is_valid({"unknown": True}))
        for raw in (
            {"type": "array"},
            {"type": "object", "$ref": "https://example.invalid/schema"},
            {"type": "object", "properties": []},
        ):
            with self.assertRaises(MCPError):
                validate_mcp_schema(raw)

    def test_raw_tool_normalization_collisions_and_length_limits(self):
        runtime = MCPRuntime({}, workdir=self.root)
        connection = SimpleNamespace(spec=ServerSpec("docs", "stdio"), secrets=[])
        schema = {"type": "object", "properties": {}}
        with self.assertRaises(MCPError):
            runtime._make_bindings(
                connection,
                [
                    {"name": "one.two", "inputSchema": schema},
                    {"name": "one_two", "inputSchema": schema},
                ],
            )
        with self.assertRaises(MCPError):
            runtime._make_bindings(
                connection, [{"name": "x" * 70, "inputSchema": schema}]
            )
        with self.assertRaises(MCPError):
            runtime._make_bindings(
                connection,
                [
                    {
                        "name": "read",
                        "inputSchema": schema,
                        "outputSchema": {"$ref": "https://example.invalid/output"},
                    }
                ],
            )


class MCPTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name).resolve()
        cls.stdio_state, cls.http_state = cls.root / "stdio", cls.root / "http"
        cls.stdio_state.mkdir()
        cls.http_state.mkdir()
        policies = {
            name: ToolPolicy("allow", True)
            for name in ("search", "get_version", "echo_scope", "dotted.echo")
        }
        policies["record_note"] = ToolPolicy("confirm", False)
        cls.env_patch = patch.dict(
            os.environ,
            {
                key: "fixture-model-value"
                for key in (
                    "OPENAI_API_KEY",
                    "OPENAI_BASE_URL",
                    "OPENAI_MODEL",
                    "DASHSCOPE_API_KEY",
                )
            },
        )
        cls.env_patch.start()
        cls.addClassCleanup(cls.env_patch.stop)
        cls.runtime = MCPRuntime(
            {
                "docs": ServerSpec(
                    "docs",
                    "stdio",
                    command=sys.executable,
                    args=(str(SERVER), "--test-tools"),
                    cwd=cls.stdio_state,
                    env={"MCP_DEMO_STATE_DIR": str(cls.stdio_state)},
                    tools=policies,
                )
            },
            workdir=cls.root,
        )
        cls.addClassCleanup(cls.runtime.close)
        cls.runtime.connect("docs")
        cls.stderr_file = tempfile.TemporaryFile(mode="w+")
        cls.addClassCleanup(cls.stderr_file.close)
        env = {
            key: value
            for key, value in os.environ.items()
            if key
            not in (
                "OPENAI_API_KEY",
                "OPENAI_BASE_URL",
                "OPENAI_MODEL",
                "DASHSCOPE_API_KEY",
            )
        }
        env.update(
            MCP_DEMO_STATE_DIR=str(cls.http_state), MCP_DEMO_TOKEN="fixture-auth-value"
        )
        cls.server = subprocess.Popen(
            [sys.executable, str(SERVER), "--transport", "http", "--test-tools"],
            stdout=subprocess.PIPE,
            stderr=cls.stderr_file,
            text=True,
            env=env,
        )

        def stop_server():
            if cls.server.poll() is None:
                cls.server.terminate()
            cls.server.communicate(timeout=10)

        cls.addClassCleanup(stop_server)
        if not select.select([cls.server.stdout], [], [], 20)[0]:
            raise RuntimeError("HTTP fixture did not become ready")
        ready = json.loads(cls.server.stdout.readline())
        cls.http_url = ready["url"]
        cls.http_runtime = MCPRuntime(
            {
                "net": ServerSpec(
                    "net",
                    "streamable_http",
                    url=cls.http_url,
                    headers={"Authorization": "Bearer fixture-auth-value"},
                    tools=policies,
                )
            },
            workdir=cls.root,
        )
        cls.addClassCleanup(cls.http_runtime.close)
        cls.http_runtime.connect("net")

    def setUp(self):
        self.stderr = io.StringIO()
        capture = redirect_stderr(self.stderr)
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def records(self, state=None):
        path = (state or self.stdio_state) / "calls.jsonl"
        return (
            [json.loads(line) for line in path.read_text().splitlines()]
            if path.exists()
            else []
        )

    def invoke(self, tool, args=None, *, runtime=None, hooks=None, bindings=None):
        block = (
            reply(calls=[call("mcp-call", name=tool, arguments=json.dumps(args or {}))])
            .choices[0]
            .message.tool_calls[0]
        )
        return json.loads(
            execute_fixture_tool(
                block,
                self.root,
                hooks=hooks,
                mcp_runtime=runtime or self.runtime,
                mcp_bindings=bindings,
            )
        )

    def test_stdio_initializes_real_child_and_preserves_text_and_structured_output(
        self,
    ):
        result = self.invoke("mcp__docs__get_version")
        self.assertFalse(result["isError"])
        self.assertTrue(result["content"][0]["text"])
        self.assertNotEqual(result["structuredContent"]["pid"], os.getpid())
        self.assertFalse(result["structuredContent"]["private_model_env_present"])

    def test_http_handshake_session_headers_and_tools_call_are_real(self):
        result = self.invoke(
            "mcp__net__search", {"query": "Hooks"}, runtime=self.http_runtime
        )
        self.assertEqual(result["structuredContent"]["results"][0]["title"], "Hooks")
        records = self.records(self.http_state)
        methods = [item.get("method") for item in records if item["kind"] == "rpc"]
        self.assertIn("initialize", methods)
        self.assertIn("notifications/initialized", methods)
        self.assertIn("tools/list", methods)
        self.assertIn("tools/call", methods)
        self.assertTrue(
            any(
                item.get("method") == "tools/call" and item["has_session"]
                for item in records
            )
        )

    def test_normalized_name_routes_to_raw_external_tool(self):
        result = self.invoke("mcp__docs__dotted_echo", {"value": "raw-name routing"})
        self.assertEqual(result["structuredContent"]["value"], "raw-name routing")
        self.assertEqual(self.records()[-1]["name"], "dotted.echo")

    def test_external_argument_names_do_not_override_host_dispatch(self):
        arguments = {
            "name": "ordinary data",
            "workdir": "data, not host cwd",
            "payload": {"name": "nested", "quantity": 2},
        }
        result = self.invoke("mcp__docs__echo_scope", arguments)
        self.assertEqual(result["structuredContent"], arguments)

    def test_schema_error_never_reaches_external_service(self):
        before = len(self.records())
        result = self.invoke(
            "mcp__docs__echo_scope",
            {
                "name": "x",
                "workdir": "data",
                "payload": {"name": "nested", "quantity": 0},
            },
        )
        self.assertIn("error", result)
        self.assertEqual(len(self.records()), before)

    def test_unconfigured_readonly_hint_still_requires_host_approval(self):
        before = len(self.records())
        with (
            patch(
                "homebrew_harness.security.permissions.sys.stdin.isatty",
                return_value=True,
            ),
            patch("builtins.input", return_value="no") as ask,
        ):
            result = self.invoke("mcp__docs__fail")
        self.assertEqual(result["permission"], "deny")
        ask.assert_called_once()
        self.assertEqual(len(self.records()), before)

    def test_mutation_confirmation_and_worker_denial_apply_before_rpc(self):
        before = len(self.records())
        with (
            noninteractive_permissions(),
            patch(
                "homebrew_harness.security.permissions.sys.stdin.isatty",
                return_value=True,
            ),
            patch("builtins.input") as ask,
        ):
            result = self.invoke("mcp__docs__record_note", {"text": "must not execute"})
        self.assertEqual(result["permission"], "deny")
        ask.assert_not_called()
        self.assertEqual(len(self.records()), before)
        with (
            patch(
                "homebrew_harness.security.permissions.sys.stdin.isatty",
                return_value=True,
            ),
            patch("builtins.input", return_value="yes"),
        ):
            result = self.invoke("mcp__docs__record_note", {"text": "approved note"})
        self.assertTrue(result["structuredContent"]["stored"])
        notes = [
            json.loads(line)
            for line in (self.stdio_state / "notes.jsonl").read_text().splitlines()
        ]
        self.assertEqual(notes[-1]["text"], "approved note")

    def test_explicit_host_deny_never_asks_or_sends_rpc(self):
        bindings = self.runtime.snapshot()[1]
        bindings["mcp__docs__get_version"] = replace(
            bindings["mcp__docs__get_version"], policy=ToolPolicy("deny", True)
        )
        before = len(self.records())
        with patch("homebrew_harness.security.permissions.ask_user") as ask:
            result = self.invoke("mcp__docs__get_version", bindings=bindings)
        self.assertEqual(result["permission"], "deny")
        ask.assert_not_called()
        self.assertEqual(len(self.records()), before)

    def test_pre_hook_can_block_rpc_and_post_hook_sees_returned_result(self):
        hooks = homebrew_harness.runtime.hooks.create_hooks()
        posts = []
        hooks.register_hook(
            "PostToolUse", lambda event, output: posts.append(event.name)
        )
        self.invoke("mcp__docs__get_version", hooks=hooks)
        self.assertEqual(posts, ["mcp__docs__get_version"])
        hooks.register_hook("PreToolUse", lambda event: '{"error":"Host blocked"}')
        before = len(self.records())
        self.assertIn("error", self.invoke("mcp__docs__get_version", hooks=hooks))
        self.assertEqual(len(self.records()), before)

    def test_http_auth_env_reference_and_credential_redaction(self):
        config = self.root / "auth.json"
        config.write_text(
            json.dumps(
                {
                    "servers": {
                        "auth": {
                            "transport": "streamable_http",
                            "url": self.http_url,
                            "headers": {"Authorization": {"env": "MCP_TEST_AUTH"}},
                            "tools": {
                                "echo_scope": {"action": "allow", "read_only": True}
                            },
                        }
                    }
                }
            )
        )
        with (
            patch.dict(os.environ, {"MCP_TEST_AUTH": "Bearer fixture-auth-value"}),
            MCPRuntime(load_specs(config), workdir=self.root) as runtime,
        ):
            runtime.connect("auth")
            result = self.invoke(
                "mcp__auth__echo_scope",
                {"name": "fixture-auth-value", "workdir": "data"},
                runtime=runtime,
            )
        self.assertNotIn("fixture-auth-value", json.dumps(result))
        self.assertEqual(result["structuredContent"]["name"], "[redacted]")

    def test_http_auth_failure_contains_no_endpoint_or_token(self):
        with MCPRuntime(
            {"bad": ServerSpec("bad", "streamable_http", url=self.http_url)},
            workdir=self.root,
        ) as runtime:
            with self.assertRaises(MCPError) as error:
                runtime.connect("bad")
        self.assertNotIn(self.http_url, str(error.exception))
        self.assertNotIn("fixture-auth-value", str(error.exception))

    def test_refresh_uses_tools_list_again_and_snapshot_is_immutable_to_callers(self):
        before = sum(
            record.get("method") == "tools/list"
            for record in self.records(self.http_state)
        )
        self.http_runtime.connect("net")
        after = sum(
            record.get("method") == "tools/list"
            for record in self.records(self.http_state)
        )
        self.assertEqual(after, before + 1)
        tools, bindings = self.runtime.snapshot()
        tools[0]["function"]["description"] = "Changed outside"
        self.assertNotEqual(
            self.runtime.snapshot()[0][0]["function"]["description"], "Changed outside"
        )
        self.assertTrue(bindings)

    def test_real_is_error_result_is_preserved(self):
        binding = self.runtime.snapshot()[1]["mcp__docs__fail"]
        result = json.loads(
            self.runtime.call(binding, {})
        )  # Host-level transport test.
        self.assertTrue(result["isError"])
        self.assertIn("Demo tool failure", result["content"][0]["text"])

    def test_request_timeout_does_not_retry_and_explicit_reconnect_works(self):
        spec = ServerSpec(
            "slow",
            "streamable_http",
            url=self.http_url,
            headers={"Authorization": "Bearer fixture-auth-value"},
            timeout_seconds=0.1,
        )
        before = sum(
            record.get("name") == "delay" for record in self.records(self.http_state)
        )
        with MCPRuntime({"slow": spec}, workdir=self.root) as runtime:
            runtime.connect("slow")
            binding = runtime.snapshot()[1]["mcp__slow__delay"]
            with self.assertRaises(MCPError):
                runtime.call(binding, {"seconds": 1})
            runtime.connect("slow")
            result = json.loads(
                runtime.call(runtime.snapshot()[1]["mcp__slow__get_version"], {})
            )
            self.assertFalse(result["isError"])
        after = sum(
            record.get("name") == "delay" for record in self.records(self.http_state)
        )
        self.assertEqual(after, before + 1)

    def test_member_plan_gate_applies_even_to_host_allowed_external_write(self):
        manager = TeamManager(self.root, Mock())
        self.addCleanup(manager.close)
        member = TeammateRuntime(manager, "worker", "worker", "Assigned", True)
        manager.members[member.name] = member
        task = manager.store.create("Work")
        member.bind(member.store.claim(task.id))
        block = (
            reply(
                calls=[
                    call(
                        "note",
                        name="mcp__docs__record_note",
                        arguments='{"text":"must not execute"}',
                    )
                ]
            )
            .choices[0]
            .message.tool_calls[0]
        )
        bindings = self.runtime.snapshot()[1]
        bindings["mcp__docs__record_note"] = replace(
            bindings["mcp__docs__record_note"], policy=ToolPolicy("allow", False)
        )
        before = len(self.records())
        result = json.loads(
            execute_fixture_tool(
                block,
                self.root,
                team_member=member,
                mcp_runtime=self.runtime,
                mcp_bindings=bindings,
            )
        )
        self.assertIn("required", result["error"])
        self.assertEqual(len(self.records()), before)

    def test_model_loop_uses_next_round_snapshot_and_paired_mcp_results(self):
        with MCPRuntime(
            {
                "fresh": ServerSpec(
                    "fresh",
                    "streamable_http",
                    url=self.http_url,
                    headers={"Authorization": "Bearer fixture-auth-value"},
                    tools={"get_version": ToolPolicy("allow", True)},
                )
            },
            workdir=self.root,
        ) as runtime:
            client, snapshots = fake_client(
                [
                    reply(
                        calls=[
                            call(
                                "connect",
                                name="connect_mcp",
                                arguments='{"name":"fresh"}',
                            ),
                            call(
                                "too-early",
                                name="mcp__fresh__get_version",
                                arguments="{}",
                            ),
                        ]
                    ),
                    reply(
                        calls=[
                            call(
                                "version",
                                name="mcp__fresh__get_version",
                                arguments="{}",
                            )
                        ]
                    ),
                    reply("Version received"),
                ]
            )
            history = [{"role": "user", "content": "Connect, then read the version."}]
            homebrew_harness.runtime.loop.agent_loop(
                client,
                "test-model",
                history,
                self.root,
                mcp_runtime=runtime,
                capabilities=Capabilities.full(memory_mode="off"),
            )
            requests = client.chat.completions.create.call_args_list
            first = {tool["function"]["name"] for tool in requests[0].kwargs["tools"]}
            second = {tool["function"]["name"] for tool in requests[1].kwargs["tools"]}
            self.assertNotIn("mcp__fresh__get_version", first)
            self.assertIn("mcp__fresh__get_version", second)
            results = {
                message["tool_call_id"]: json.loads(message["content"])
                for message in history
                if message["role"] == "tool"
            }
            self.assertEqual(results["too-early"]["error"], "Unknown tool.")
            self.assertFalse(results["version"]["isError"])

    def test_child_receives_connected_tools_but_cannot_connect_new_servers(self):
        client, _ = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "connect", name="connect_mcp", arguments='{"name":"docs"}'
                        ),
                        call("version", name="mcp__docs__get_version", arguments="{}"),
                    ]
                ),
                reply("Child finding"),
            ]
        )
        result = homebrew_harness.runtime.turns.run_subagent(
            "Read connected service version",
            self.root,
            client=client,
            model="test-model",
            mcp_runtime=self.runtime,
        )
        self.assertEqual(json.loads(result)["result"], "Child finding")
        names = {
            tool["function"]["name"]
            for tool in client.chat.completions.create.call_args_list[0].kwargs["tools"]
        }
        self.assertNotIn("connect_mcp", names)
        self.assertIn("mcp__docs__get_version", names)

    def test_unknown_server_does_not_start_transport(self):
        with MCPRuntime({}, workdir=self.root) as runtime:
            with self.assertRaises(MCPError):
                runtime.connect("unregistered")
            self.assertIsNone(runtime._thread)

    def test_stdio_process_crash_is_reported_and_cleanup_stops_owned_process(self):
        state = self.root / "crash"
        state.mkdir(exist_ok=True)
        spec = ServerSpec(
            "crash",
            "stdio",
            command=sys.executable,
            args=(str(SERVER), "--test-tools"),
            cwd=state,
            env={"MCP_DEMO_STATE_DIR": str(state)},
            timeout_seconds=1,
        )
        with MCPRuntime({"crash": spec}, workdir=self.root) as runtime:
            runtime.connect("crash")
            bindings = runtime.snapshot()[1]
            result = json.loads(runtime.call(bindings["mcp__crash__get_version"], {}))
            pid = result["structuredContent"]["pid"]
            with self.assertRaises(MCPError):
                runtime.call(bindings["mcp__crash__crash"], {})
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertFalse(runtime._thread.is_alive())
