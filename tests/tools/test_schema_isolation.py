"""Final tool contracts keep targeted enums out of other argument fields."""

import copy
import json
import unittest
from types import SimpleNamespace

from jsonschema import Draft202012Validator

from homebrew_harness.runtime.capabilities import Capabilities
from homebrew_harness.tools.catalog import TOOLS, NONEMPTY_STRING, define_tool
from homebrew_harness.tools.tool_pool import ToolPool


class ToolSchemaIsolationTests(unittest.TestCase):
    def pool(self, aliases=("planner", "coder"), **options):
        return ToolPool.build(
            TOOLS,
            {t["function"]["name"]: lambda **_: None for t in TOOLS},
            Capabilities.full(),
            models=SimpleNamespace(catalog=lambda: [
                {"name": alias, "description": "Fixture role"} for alias in aliases
            ]),
            **options,
        )

    def parameters(self, pool, name):
        tool = next(t for t in pool.tools if t["function"]["name"] == name)
        # Assert the serialized contract the provider sees, not only templates.
        return json.loads(json.dumps(tool))["function"]["parameters"]

    def test_task_retains_full_multiline_prompt_with_model_catalog(self):
        schema = self.parameters(self.pool(), "task")
        prompt = "Implement src/greet.py.\nKeep the text interface unchanged.\nReturn changed files and evidence."
        Draft202012Validator(schema).validate(
            {"prompt": prompt, "model_profile": "coder"}
        )
        Draft202012Validator(schema).validate({"prompt": prompt})
        self.assertNotIn("enum", schema["properties"]["prompt"])
        self.assertEqual(schema["properties"]["model_profile"]["enum"],
                         ["planner", "coder"])
        self.assertFalse(Draft202012Validator(schema).is_valid(
            {"prompt": prompt, "model_profile": "unknown"}
        ))

    def test_teammate_assignment_fields_are_not_model_names(self):
        schema = self.parameters(self.pool(), "spawn_teammate")
        arguments = {"name": "builder", "role": "implementation",
                     "prompt": "Implement the assigned module.\nReport evidence.",
                     "task_id": "task-123", "model_profile": "coder"}
        Draft202012Validator(schema).validate(arguments)
        for field in ("name", "role", "prompt", "task_id"):
            self.assertNotIn("enum", schema["properties"][field])

    def test_bash_command_is_not_an_artifact_profile(self):
        profile = SimpleNamespace(name="report", data=lambda: {"name": "report"})
        schema = self.parameters(self.pool(
            bash_policy=SimpleNamespace(mode="container", outputs=[profile]),
            artifacts=object(),
        ), "bash")
        Draft202012Validator(schema).validate(
            {"command": "printf 'ok' > report.txt", "artifact_profile": "report"}
        )
        self.assertNotIn("enum", schema["properties"]["command"])
        self.assertFalse(Draft202012Validator(schema).is_valid(
            {"command": "printf 'ok'", "artifact_profile": "unknown"}
        ))

    def test_catalog_changes_do_not_mutate_templates_or_other_snapshots(self):
        before = copy.deepcopy(TOOLS)
        first = self.pool()
        second = self.pool(("researcher",))
        first_schema = self.parameters(first, "task")
        second_schema = self.parameters(second, "task")
        self.assertEqual(first_schema["properties"]["model_profile"]["enum"],
                         ["planner", "coder"])
        self.assertEqual(second_schema["properties"]["model_profile"]["enum"],
                         ["researcher"])
        for schema in (first_schema, second_schema):
            self.assertNotIn("enum", schema["properties"]["prompt"])
        self.assertEqual(TOOLS, before)
        self.assertNotIn("enum", NONEMPTY_STRING)

    def test_protocol_projections_preserve_the_scoped_tool_schema(self):
        from homebrew_harness.execution.model_protocol import ModelRequest, ModelCapabilities
        from homebrew_harness.execution.providers import (
            anthropic, chat_completions, dashscope, responses,
        )
        task = next(t for t in self.pool().tools if t["function"]["name"] == "task")
        before = copy.deepcopy(task)
        request = ModelRequest("fixture-model", (), tools=(task,))
        for codec in (chat_completions, responses, anthropic, dashscope):
            with self.subTest(protocol=codec.__name__):
                body = codec.prepare(request, [], {}, ModelCapabilities())
                tools = body.get("tools", body.get("parameters", {}).get("tools"))
                wire = json.loads(json.dumps(tools[0]))
                schema = (wire["function"]["parameters"] if "function" in wire
                          else wire.get("input_schema", wire.get("parameters")))
                Draft202012Validator(schema).validate({
                    "prompt": "Implement greet.py.\nReturn the verified result.",
                    "model_profile": "coder",
                })
                self.assertEqual(schema, before["function"]["parameters"])
                self.assertEqual(task, before)

    def test_mcp_name_enum_is_isolated_even_with_shared_field_templates(self):
        definition = define_tool(
            "connect_mcp", "Fixture connection", {"name": NONEMPTY_STRING,
                                                      "note": NONEMPTY_STRING},
            ["name"],
        )
        mcp = SimpleNamespace(specs={"docs": object()}, snapshot=lambda: ([], {}))
        pool = ToolPool.build([definition], {"connect_mcp": lambda **_: None},
                              Capabilities({"mcp"}), mcp=mcp)
        schema = self.parameters(pool, "connect_mcp")
        self.assertEqual(schema["properties"]["name"]["enum"], ["docs"])
        self.assertNotIn("enum", schema["properties"]["note"])
        self.assertNotIn("enum", definition["function"]["parameters"]["properties"]["name"])


if __name__ == "__main__":
    unittest.main()
