"""Run stage declarations constrain both advertised and executable endpoints."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

from homebrew_harness import HarnessSession, Capabilities
from homebrew_harness.runs.spec import RunSpec, RUN_CONTROL_TOOLS, RUN_STAGE_TOOLS
from homebrew_harness.runs.store import RunStore
from homebrew_harness.runs.controller import RunController, run_scope
from homebrew_harness.tools.dispatch import execute_tool
from homebrew_harness.execution.model_protocol import ToolInvocation
from tests.support.clients import fake_client


class EffectiveRunCapabilitiesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        for output in (redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            output.__enter__()
            self.addCleanup(output.__exit__, None, None, None)

    def pool(self, spec):
        store = RunStore(self.root)
        self.addCleanup(store.close)
        value = store.create(spec)
        value = store.claim(value["id"])
        control = RunController(store, value["id"], value["owner"], value["generation"])
        client, _ = fake_client([])
        session = HarnessSession(
            client,
            "fixture",
            self.root,
            run_control=control,
            capabilities=Capabilities(spec.enabled),
            shutdown_timeout=0.05,
        )
        self.addCleanup(lambda: session.wait_closed(3))
        self.addCleanup(session.close)
        return session, control, session.tool_pool()

    def test_no_stage_default_and_narrow_run_have_only_control_endpoints(self):
        spec = RunSpec("Inspect files", min_free_bytes=0, tools=("read_file",))
        session, control, pool = self.pool(spec)
        self.assertTrue(RUN_CONTROL_TOOLS <= pool.names)
        self.assertTrue(RUN_STAGE_TOOLS.isdisjoint(pool.names))
        self.assertTrue(RUN_STAGE_TOOLS.isdisjoint(pool.handlers))
        self.assertEqual(pool.names, RUN_CONTROL_TOOLS | {"read_file"})
        for name in RUN_STAGE_TOOLS:
            with run_scope(control), pool.scope():
                result = json.loads(
                    execute_tool(
                        ToolInvocation("fixture", name, "{}"),
                        self.root,
                        hooks=session.hooks,
                        handlers=pool.handlers,
                    )
                )
            self.assertTrue(result.get("error"))
        self.assertEqual(control.store.runtime.items("tasks"), {})

    def test_explicit_stage_endpoint_without_stages_is_invalid_configuration(self):
        for name in RUN_STAGE_TOOLS:
            with self.subTest(name=name), self.assertRaises(ValueError):
                RunSpec("Inspect", tools=("read_file", name))

    def test_nonintegration_stages_only_expose_handoff(self):
        goal = {
            "condition": "Report exists",
            "predicates": [
                {"id": "report", "type": "file_exists", "path": "REPORT.md"}
            ],
            "judge": False,
        }
        spec = RunSpec(
            "Create report",
            min_free_bytes=0,
            goal=goal,
            stages=({"key": "report", "subject": "Report", "acceptance": goal},),
        )
        _, _, pool = self.pool(spec)
        self.assertIn("get_handoff", pool.names)
        self.assertNotIn("integrate_stage", pool.names)
        self.assertNotIn("publish_stage", pool.names)
        self.assertIn("complete_task", pool.names)

    def test_narrow_stage_selection_does_not_gain_extra_endpoints(self):
        goal = {
            "condition": "Report exists",
            "predicates": [
                {"id": "report", "type": "file_exists", "path": "REPORT.md"}
            ],
            "judge": False,
        }
        spec = RunSpec(
            "Read report",
            min_free_bytes=0,
            goal=goal,
            stages=({"key": "report", "subject": "Report", "acceptance": goal},),
            tools=("read_file",),
        )
        _, _, pool = self.pool(spec)
        self.assertEqual(pool.names, RUN_CONTROL_TOOLS | {"read_file"})
