"""Model-visible Work tools must execute through the actual scoped tool pool."""

import json
import unittest
from pathlib import Path
from tests.services import test_work_sources as fixture
from tests.support.clients import fake_client, reply, call
from homebrew_harness.runtime.assembly import _prepare_loop
from homebrew_harness.runtime.loop_components import LoopComponents
from homebrew_harness.runtime.capabilities import Capabilities
from homebrew_harness.services.model_work_tools import ModelWorkTools, NAMES
from homebrew_harness.tools.catalog import TOOL_SCHEMAS, SERVICE_TOOL_DEFINITIONS


class WorkToolDispatch(unittest.TestCase):
    setUp = fixture.WorkSourcesTests.setUp
    start = fixture.WorkSourcesTests.start
    run_git = fixture.WorkSourcesTests.run_git
    ready = fixture.WorkSourcesTests.ready

    def test_service_definition_schemas_reach_the_dispatch_index(self):
        for definition in SERVICE_TOOL_DEFINITIONS:
            name = definition["function"]["name"]
            self.assertIn(name, TOOL_SCHEMAS)
            self.assertEqual(TOOL_SCHEMAS[name], definition["function"]["parameters"])

    def test_model_calls_each_work_tool_through_the_prepared_loop(self):
        service, work, candidate = self.ready()
        target = work.observe_target(
            execution={"mode": "one_shot"}, request_id="dispatch-target"
        )
        work.continue_from(
            {
                "mode": "fork",
                "source_ref": candidate,
                "reason": "Prepare owned integration facts.",
                "integration": {"method": "merge", "target_ref": target["source_ref"]},
                "start": False,
            },
            execution={"mode": "one_shot"},
            request_id="dispatch-preparation",
        )
        adapter = ModelWorkTools(service, work.id)

        class Boundary:
            def handlers(self):
                return adapter.tools()

            def describe_tools(self, definitions):
                return definitions

        invocations = [
            ("service_work_sources", {}),
            (
                "service_read_source",
                {"source_ref": candidate, "path": "src/value.txt", "limit": 100},
            ),
            (
                "service_compare_sources",
                {"before": candidate, "after": "draft:" + work.id},
            ),
            ("service_observe_target", {}),
            ("service_manage_execution", {"operation": "list"}),
            ("service_integration_control", {"operation": "inspect"}),
        ]
        client, queued = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "work-" + str(n), name=name, arguments=json.dumps(arguments)
                        )
                        for n, (name, arguments) in enumerate(invocations)
                    ]
                ),
                reply("Observed all owned tools."),
            ]
        )
        directory = self.control / ".harness" / "dispatch-model"
        directory.mkdir()
        with _prepare_loop(
            client,
            "fixture",
            directory,
            LoopComponents(
                capabilities=Capabilities(),
                service_control=Boundary(),
                tool_allowlist=NAMES,
            ),
        ) as loop:
            self.assertTrue(NAMES <= loop.pool.names)
            messages = [
                {
                    "role": "user",
                    "content": "Inspect the owned Work using its advertised tools.",
                }
            ]
            loop.run(messages, max_turns=2)
        results = [
            json.loads(m["content"]) for m in messages if m.get("role") == "tool"
        ]
        self.assertEqual(len(results), 6)
        self.assertTrue(all("error" not in result for result in results), results)
        self.assertEqual(results[0]["work_id"], work.id)
        self.assertEqual(results[1]["text"], "1\n")
        self.assertEqual(results[2]["total_files"], 0)
        self.assertTrue(results[3]["accepted"])
        self.assertIn("executions", results[4])
        self.assertTrue(results[5]["accepted"])
        work.wait_execution(results[5]["execution_id"], 10)
        final = json.loads(
            work.execution_output(results[5]["execution_id"], stream="result")[
                "content"
            ]
        )
        self.assertEqual(final["state"], "settled")

    def test_observation_effects_do_not_grant_control_as_read_only(self):
        from homebrew_harness.tools.tool_effects import is_read_only

        self.assertTrue(is_read_only("service_read_source"))
        self.assertTrue(
            is_read_only("service_manage_execution", arguments={"operation": "read"})
        )
        self.assertFalse(
            is_read_only("service_manage_execution", arguments={"operation": "stop"})
        )
        self.assertTrue(
            is_read_only(
                "service_integration_control", arguments={"operation": "inspect"}
            )
        )
        self.assertFalse(
            is_read_only(
                "service_integration_control", arguments={"operation": "continue"}
            )
        )
