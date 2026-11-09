"""MCP follows the parent selection and host role constraints in child loops."""

import unittest
import json
from unittest.mock import patch
from homebrew_harness.runs import RunSpec
from homebrew_harness.runtime.capabilities import Capabilities
from homebrew_harness.tools.tool_pool import ToolPool
from tests.runs import test_mcp as fixture
from tests.support.clients import reply, call


class RunMCPCombinations(unittest.TestCase):
    setUp = fixture.RunMCPTests.setUp
    controller = fixture.RunMCPTests.controller
    close = fixture.RunMCPTests.close

    def test_subagent_inherits_only_explicit_allow_readonly_tools(self):
        spec = RunSpec(
            "Delegate a documentation lookup",
            enabled=("subagents",),
            mcp={"docs": ["search", "record_note"]},
            mcp_delegation="read_only",
        )
        control, session, requests = self.controller(
            [
                reply(
                    calls=[
                        call(
                            "delegate",
                            name="task",
                            arguments='{"prompt":"Look up Agent Loop"}',
                        )
                    ]
                ),
                reply(
                    calls=[
                        call(
                            "read",
                            name="mcp__docs__search",
                            arguments='{"query":"loop"}',
                        )
                    ]
                ),
                reply("Agent Loop uses paired tool results"),
                reply("Lookup complete"),
            ],
            spec=spec,
        )
        outcome = control.execute()
        self.assertEqual(outcome.status, "completed", outcome.data())
        self.assertTrue((self.base / "service/calls.jsonl").exists())
        # Model snapshots record the actual child context separately.
        child = next(
            v
            for v in requests
            if any(m.get("content") == "Look up Agent Loop" for m in v)
        )
        self.assertFalse(any(m.get("name") == "managed_run" for m in child))
        for role in ("subagent", "teammate"):
            pool = ToolPool.build(
                [], {}, Capabilities(("mcp",)), mcp=session.mcp, mcp_role=role
            )
            self.assertEqual(pool.names, {"mcp__docs__search"})
        self.close(control, session)

    def test_default_delegation_does_not_expose_external_tools_to_children(self):
        control, session, _ = self.controller([reply("done")])
        self.assertEqual(control.execute().status, "completed")
        for role in ("subagent", "teammate"):
            pool = ToolPool.build(
                [], {}, Capabilities(("mcp",)), mcp=session.mcp, mcp_role=role
            )
            self.assertEqual(pool.names, set())
        self.close(control, session)

    def test_readonly_child_limit_does_not_claim_partial_writes(self):
        from homebrew_harness.runtime.turns import run_subagent

        spec = RunSpec(
            "Delegate a bounded read",
            enabled=("subagents",),
            mcp={"docs": ["search"]},
            mcp_delegation="read_only",
        )
        control, session, _ = self.controller(
            [
                reply(
                    calls=[
                        call(
                            "read",
                            name="mcp__docs__search",
                            arguments='{"query":"loop"}',
                        )
                    ]
                ),
            ],
            spec=spec,
        )
        control.prepare()
        with session.tool_pool().scope():
            result = json.loads(
                run_subagent(
                    "Read documentation",
                    self.root,
                    client=session.gateway,
                    model=session.model,
                    hooks=session.hooks,
                    max_turns=1,
                    mcp_runtime=session.mcp,
                    tool_allowlist=frozenset({"mcp__docs__search"}),
                    bash_policy=session.bash_policy,
                    shell_runner=session.shell,
                )
            )
        self.assertEqual(result["status"], "not_executed")
        self.assertEqual(result["effects"], [])
        self.assertEqual(session.recovery_operations(), [])
        self.close(control, session)

    def test_ready_host_pool_reuses_schema_until_explicit_reconnect(self):
        control, session, _ = self.controller([reply("done")])
        control.prepare()
        with patch.object(session.mcp, "connect", wraps=session.mcp.connect) as connect:
            session.tool_pool()
            session.tool_pool()
        connect.assert_not_called()
        self.close(control, session)

    def test_effective_pool_limit_includes_fixed_and_external_tools(self):
        selection = {
            "one": ["t" + str(i) for i in range(64)],
            "two": ["t" + str(i) for i in range(64)],
        }
        with self.assertRaisesRegex(ValueError, "128"):
            RunSpec("Oversized effective selection", mcp=selection)

    def test_malformed_selections_and_delegation_fail_as_configuration_errors(self):
        for fields in (
            {"mcp": {"docs": ["*"]}},
            {"mcp": {"*": ["read"]}},
            {"mcp_delegation": []},
            {"enabled": [{"name": "mcp"}], "mcp": {"docs": ["search"]}},
            {"enabled": ["mcp", "mcp"], "mcp": {"docs": ["search"]}},
        ):
            with self.assertRaises(ValueError):
                RunSpec("Invalid declaration", **fields)
