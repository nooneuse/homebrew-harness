"""Installed entry points and tests must select the assigned source tree."""

import ast
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

from homebrew_harness import HarnessSession, Capabilities, LoopComponents, agent_loop
from homebrew_harness.execution.test_runner import TestRunner, TestRunnerError
from tests.support.clients import fake_client, reply
from tests.support.paths import PROJECT_ROOT


class PackageLayoutTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for context in (redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)

    def test_public_api_runs_from_the_package(self):
        client, _ = fake_client([reply("done")])
        with HarnessSession(
            client, "fixture", self.root, shutdown_timeout=0.05
        ) as session:
            self.assertEqual(session.run_user("Read the fixture"), "done")
            self.assertIsInstance(session.loop_components(), LoopComponents)
        self.assertTrue(session.wait_closed(3))

    def test_root_python_modules_are_retired(self):
        self.assertFalse(list(PROJECT_ROOT.glob("*.py")))
        for path in (PROJECT_ROOT / "src/homebrew_harness").rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn(
                        node.module, {"agent", "harness", "model_gateway", "tool_pool"}
                    )
                if isinstance(node, ast.Import):
                    self.assertFalse(
                        any(
                            item.name
                            in {"agent", "harness", "model_gateway", "tool_pool"}
                            for item in node.names
                        )
                    )

    def fixture_project(self):
        package = self.root / "src/homebrew_harness"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("MARKER = 'assigned-workspace'\n")
        tests = self.root / "tests/nested"
        tests.mkdir(parents=True)
        (self.root / "tests/__init__.py").write_text("")
        (tests / "__init__.py").write_text("")
        (tests / "test_source.py").write_text("""import unittest
from pathlib import Path
import homebrew_harness
class SourceTests(unittest.TestCase):
    def test_assigned_source(self):
        self.assertEqual(homebrew_harness.MARKER, 'assigned-workspace')
        self.assertEqual(Path(homebrew_harness.__file__).resolve(), Path.cwd()/'src/homebrew_harness/__init__.py')
    def test_second_case_is_discovered(self):
        self.assertTrue(True)
""")
        runner = TestRunner.default(self.root)
        self.addCleanup(runner.close)
        return runner

    def test_nested_discovery_and_source_override_installed_checkout(self):
        runner = self.fixture_project()
        runner.approve("unit-tests")
        result = runner.run("unit-tests")
        self.assertEqual(result["status"], "passed", result)
        self.assertIn("Ran 2 tests", result["stderr_preview"])
        self.assertTrue(result["source_stable"])
        self.assertTrue(runner.verify("unit-tests", self.root)["ok"])

    def test_changed_nested_test_definition_requires_new_authorization(self):
        runner = self.fixture_project()
        runner.approve("unit-tests")
        (self.root / "tests/nested/test_source.py").write_text(
            'raise RuntimeError("changed protected test")\n'
        )
        with self.assertRaises(TestRunnerError):
            runner.run("unit-tests")
        self.assertFalse(runner.active.is_set())

    def test_changed_src_invalidates_previous_receipt(self):
        runner = self.fixture_project()
        runner.approve("unit-tests")
        self.assertEqual(runner.run("unit-tests")["status"], "passed")
        (self.root / "src/homebrew_harness/__init__.py").write_text(
            "MARKER = 'changed'\n"
        )
        self.assertFalse(runner.verify("unit-tests", self.root)["ok"])

    def test_private_working_state_remains_relative_to_cwd(self):
        client, _ = fake_client([])
        session = HarnessSession(
            client,
            "fixture",
            self.root,
            capabilities=Capabilities({"tasks"}),
            shutdown_timeout=0.05,
        )
        self.addCleanup(lambda: session.wait_closed(3))
        self.addCleanup(session.close)
        session.run_user("/usage")
        self.assertEqual(session.runtime_store.workdir, self.root)
        self.assertEqual(session.workspace.store.workdir, self.root)
        self.assertTrue((self.root / ".harness").is_dir())
        self.assertFalse((self.root / "src").exists())
