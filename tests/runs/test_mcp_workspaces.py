"""Delegated workspace tools prepare the member's own scope and input copy."""

import json
import unittest
from dataclasses import replace

from homebrew_harness import HarnessSession, Capabilities
from homebrew_harness.runs import RunController
from homebrew_harness.tools.mcp.runtime import MCPRuntime, ServerSpec, ToolPolicy
from homebrew_harness.tools.tool_pool import ToolPool
from tests.runs import test_managed_team as team_fixture
from tests.tools.mcp import test_container as container_fixture
from tests.support.clients import fake_client
from unittest.mock import patch


class RunMCPWorkspaces(unittest.TestCase):
    setUp = team_fixture.ManagedTeamTests.setUp

    def test_role_readonly_bindings_use_exact_task_workspaces(self):
        policy = container_fixture.MCPExecutionPolicy(
            mode="container",
            cli="/missing/docker",
            image="sha256:" + "a" * 64,
            inputs="workspace",
        )
        server = ServerSpec(
            "docs",
            "stdio",
            command="/image/server",
            execution=policy,
            tools={
                "read_text": ToolPolicy("allow", True),
                "get_version": ToolPolicy("confirm", True),
            },
        )
        spec = replace(
            team_fixture.team_spec(),
            mcp={"docs": ["read_text", "get_version"]},
            mcp_delegation="read_only",
        )
        state = self.store.claim(self.store.create(spec)["id"])
        control = RunController(
            self.store, state["id"], state["owner"], state["generation"]
        )
        self.addCleanup(control.close_heartbeat)
        backend = patch(
            "homebrew_harness.execution.mcp_runner.MCPContainerBackend",
            container_fixture.ProcessBackend,
        )
        backend.start()
        self.addCleanup(backend.stop)
        runtime = MCPRuntime(
            {"docs": server},
            workdir=self.root,
            selection=spec.mcp,
            delegation=spec.mcp_delegation,
        )
        client, _ = fake_client([])
        session = HarnessSession(
            client,
            "fixture",
            self.root,
            capabilities=Capabilities(spec.enabled),
            run_control=control,
            mcp_runtime=runtime,
        )
        self.addCleanup(lambda: self.assertTrue(session.wait_closed(8)))
        self.addCleanup(session.close)
        control.prepare()
        bindings = []
        for role, name in (("teammate", "alpha"), ("subagent", "beta")):
            task = control.team_control.task_for(name)
            cwd = session.workspace.store.workspace(task)
            (cwd / "input.txt").write_text(name + " only")
            pool = ToolPool.build(
                [],
                {},
                Capabilities(("mcp",)),
                mcp=runtime,
                mcp_role=role,
                mcp_workdir=cwd,
                mcp_task_id=task.id,
            )
            self.assertEqual(pool.names, {"mcp__docs__read_text"})
            binding = pool.bindings["mcp__docs__read_text"]
            result = json.loads(
                runtime.call(
                    binding, {"path": "input.txt"}, workdir=cwd, task_id=task.id
                )
            )
            self.assertEqual(result["structuredContent"]["text"], name + " only")
            bindings.append(binding)
        self.assertIsNot(bindings[0].connection, bindings[1].connection)
        with self.assertRaises(container_fixture.MCPError):
            runtime.call(
                bindings[0], {"path": "input.txt"}, workdir=cwd, task_id=task.id
            )
