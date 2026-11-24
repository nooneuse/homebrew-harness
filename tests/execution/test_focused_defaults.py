"""Host default_targets/min_tests: opt-in launcher, evidence and compatibility.

New regression coverage only; protected baseline tests are untouched.
"""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from homebrew_harness.execution.test_runner import (
    TestRecipe,
    TestRunner,
    TestRunnerError,
    digest,
    load_test_runner,
)
from homebrew_harness.execution.test_selection import (
    EXTENDED_LAUNCHER,
    LAUNCHER,
    selection_environment,
)

PASS_MODULE = (
    "import unittest\n"
    "class Sample(unittest.TestCase):\n"
    "    def test_ok(self):\n"
    "        self.assertTrue(True)\n"
)
FAIL_MODULE = (
    "import unittest\n"
    "class Broken(unittest.TestCase):\n"
    "    def test_bad(self):\n"
    "        self.fail('fixture failure')\n"
)
EMPTY_MODULE = "value = 1\n"

# The registry loader supplies these historical builtin locked_globs when the
# host entry omits them; expected fixtures must compare equivalent settings.
BUILTIN_LOCKED_GLOBS = ("tests/**", "pyproject.toml", "conftest.py", "pytest.ini")


def builtin_recipe(launcher=LAUNCHER, *, name="suite", **settings):
    return TestRecipe(
        name,
        (sys.executable, "-I", "-c", launcher),
        selector="unittest",
        **settings,
    )


class WorkspaceFixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        for redirect in (redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            redirect.__enter__()
            self.addCleanup(redirect.__exit__, None, None, None)

    def make_tests(self, **modules):
        folder = self.root / "tests"
        folder.mkdir(exist_ok=True)
        (folder / "__init__.py").write_text("")
        for name, source in modules.items():
            (folder / (name + ".py")).write_text(source)

    def runner(self, recipe):
        runner = TestRunner(self.root, [recipe])
        self.addCleanup(runner.close)
        runner.approve(recipe.name)
        return runner


class DefaultTargetValidation(unittest.TestCase):
    def test_valid_targets_are_normalized_to_a_deduplicated_tuple(self):
        recipe = builtin_recipe(
            EXTENDED_LAUNCHER,
            default_targets=["tests.test_a", "tests.test_a", "tests.pkg.test_b"],
        )
        self.assertEqual(recipe.default_targets, ("tests.test_a", "tests.pkg.test_b"))

    def test_string_bytes_dict_and_other_containers_are_rejected_not_coerced(self):
        for value in ("tests.test_a", b"tests.test_a", {"targets": ["tests.test_a"]},
                      {"tests.test_a"}, 3, []):
            with self.subTest(value=value):
                with self.assertRaises(TestRunnerError):
                    builtin_recipe(EXTENDED_LAUNCHER, default_targets=value)

    def test_invalid_or_out_of_root_targets_are_rejected(self):
        for value in (["other.test_a"], ["tests..bad"], ["tests/test_a"],
                      ["tests.test_a", "outside.test_b"], [""], [None]):
            with self.subTest(value=value):
                with self.assertRaises(TestRunnerError):
                    builtin_recipe(EXTENDED_LAUNCHER, default_targets=value)

    def test_targets_must_respect_declared_test_roots(self):
        with self.assertRaises(TestRunnerError):
            builtin_recipe(
                EXTENDED_LAUNCHER,
                test_roots=("checks",),
                default_targets=["tests.test_a"],
            )
        recipe = builtin_recipe(
            EXTENDED_LAUNCHER,
            test_roots=("checks",),
            default_targets=["checks.test_a"],
        )
        self.assertEqual(recipe.default_targets, ("checks.test_a",))

    def test_options_require_the_unittest_selector(self):
        with self.assertRaises(TestRunnerError):
            TestRecipe("suite", (sys.executable, "-c", "print('ok')"),
                       default_targets=["tests.test_a"])
        with self.assertRaises(TestRunnerError):
            TestRecipe("suite", (sys.executable, "-c", "print('ok')"), min_tests=2)
        with self.assertRaises(TestRunnerError):
            TestRecipe("suite", (sys.executable, "-I", "-c", EXTENDED_LAUNCHER),
                       selector="pytest", default_targets=["tests/test_a.py"])

    def test_min_tests_must_be_a_positive_integer(self):
        for value in (0, -1, True, 1.5, "2"):
            with self.subTest(value=value):
                with self.assertRaises(TestRunnerError):
                    builtin_recipe(EXTENDED_LAUNCHER, min_tests=value)
        self.assertEqual(builtin_recipe(EXTENDED_LAUNCHER, min_tests=1).min_tests, 1)

    def test_opt_in_binds_the_extended_launcher_without_fallback(self):
        with self.assertRaises(TestRunnerError):
            builtin_recipe(LAUNCHER, default_targets=["tests.test_a"])
        with self.assertRaises(TestRunnerError):
            builtin_recipe(LAUNCHER, min_tests=2)
        with self.assertRaises(TestRunnerError):
            builtin_recipe(EXTENDED_LAUNCHER)  # no opt-in keeps the legacy argv
        legacy = builtin_recipe(LAUNCHER)
        self.assertEqual(legacy.argv[1:], ("-I", "-c", LAUNCHER))
        self.assertIsNone(legacy.default_targets)
        self.assertIsNone(legacy.min_tests)


class DefaultRunEvidence(WorkspaceFixture):
    def test_default_run_uses_only_configured_targets(self):
        self.make_tests(test_pass=PASS_MODULE, test_fail=FAIL_MODULE)
        runner = self.runner(builtin_recipe(
            EXTENDED_LAUNCHER, default_targets=["tests.test_pass"], min_tests=1))
        result = runner.run("suite")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["exit_code"], 0)
        self.assertIsNone(result["selection"])
        self.assertEqual(result["test_scope"], "full")
        self.assertEqual(result["default_targets"], ["tests.test_pass"])
        self.assertEqual(result["min_tests"], 1)
        self.assertTrue(runner.verify("suite", self.root)["ok"])

    def test_legacy_discovery_is_preserved_without_options(self):
        self.make_tests(test_pass=PASS_MODULE, test_fail=FAIL_MODULE)
        runner = self.runner(builtin_recipe(LAUNCHER))
        result = runner.run("suite")
        self.assertEqual(result["status"], "failed")  # discovery sees the failure
        self.assertEqual(result["test_scope"], "full")
        self.assertNotIn("default_targets", result)
        self.assertNotIn("min_tests", result)
        self.assertEqual(
            selection_environment("unittest", ("tests",), None),
            {"HARNESS_TEST_SELECTION": json.dumps(
                {"framework": "unittest", "roots": ["tests"], "selection": None})},
        )

    def test_min_tests_applies_to_the_full_run_only(self):
        self.make_tests(test_pass=PASS_MODULE)
        runner = self.runner(builtin_recipe(EXTENDED_LAUNCHER, min_tests=5))
        full = runner.run("suite")
        self.assertEqual(full["status"], "failed")
        self.assertEqual(full["exit_code"], 4)
        self.assertFalse(runner.verify("suite", self.root)["ok"])
        selected = runner.run(
            "suite", selection={"targets": ["tests.test_pass.Sample.test_ok"]})
        self.assertEqual(selected["status"], "passed")  # minimum not applied
        self.assertEqual(selected["test_scope"], "selected")
        self.assertNotIn("min_tests", selected)
        self.assertNotIn("default_targets", selected)

    def test_selection_replaces_defaults_and_cannot_clear_full_acceptance(self):
        self.make_tests(test_pass=PASS_MODULE, test_fail=FAIL_MODULE)
        runner = self.runner(builtin_recipe(
            EXTENDED_LAUNCHER,
            default_targets=["tests.test_fail"],
            min_tests=50,
        ))
        selected = runner.run(
            "suite", selection={"targets": ["tests.test_pass.Sample.test_ok"]})
        self.assertEqual(selected["status"], "passed")
        self.assertEqual(selected["selection"],
                         {"targets": ["tests.test_pass.Sample.test_ok"]})
        self.assertEqual(selected["test_scope"], "selected")
        self.assertNotIn("default_targets", selected)
        self.assertNotIn("min_tests", selected)
        verdict = runner.verify("suite", self.root)
        self.assertFalse(verdict["ok"])  # selected evidence never satisfies full
        full = runner.run("suite")
        self.assertEqual(full["status"], "failed")  # defaults run the failing module

    def test_empty_selection_is_rejected(self):
        self.make_tests(test_pass=PASS_MODULE)
        runner = self.runner(builtin_recipe(EXTENDED_LAUNCHER,
                                            default_targets=["tests.test_pass"]))
        for selection in ({"targets": []}, {}):
            with self.subTest(selection=selection):
                with self.assertRaises(ValueError):
                    runner.run("suite", selection=selection)

    def test_zero_collected_tests_never_pass(self):
        self.make_tests(test_empty=EMPTY_MODULE)
        runner = self.runner(builtin_recipe(
            EXTENDED_LAUNCHER, default_targets=["tests.test_empty"], min_tests=1))
        default = runner.run("suite")
        self.assertEqual(default["status"], "failed")
        self.assertEqual(default["exit_code"], 3)
        selected = runner.run("suite", selection={"targets": ["tests.test_empty"]})
        self.assertEqual(selected["status"], "failed")
        self.assertEqual(selected["exit_code"], 3)
        legacy = self.runner(builtin_recipe(LAUNCHER))
        discovered = legacy.run("suite")
        self.assertEqual(discovered["status"], "failed")
        self.assertEqual(discovered["exit_code"], 3)
        self.assertFalse(runner.verify("suite", self.root)["ok"])

    def test_catalog_lists_opt_in_settings_only_when_configured(self):
        self.make_tests(test_pass=PASS_MODULE)
        opted = self.runner(builtin_recipe(
            EXTENDED_LAUNCHER, default_targets=["tests.test_pass"], min_tests=1))
        entry = opted.catalog()[0]
        self.assertEqual(entry["default_targets"], ["tests.test_pass"])
        self.assertEqual(entry["min_tests"], 1)
        legacy = self.runner(builtin_recipe(LAUNCHER))
        entry = legacy.catalog()[0]
        self.assertNotIn("default_targets", entry)
        self.assertNotIn("min_tests", entry)


class FingerprintBinding(WorkspaceFixture):
    def test_legacy_fingerprint_serialization_is_unchanged(self):
        recipe = builtin_recipe(LAUNCHER)
        data = {
            "argv": recipe.argv,
            "snapshot_limits": recipe.snapshot_limits,
            "executable": recipe._identity,
            "env": recipe.env,
            "inputs": recipe.input_globs,
            "locked": recipe.locked_globs,
            "roles": recipe.roles,
            "selector": recipe.selector,
            "test_roots": recipe.test_roots,
            "timeout": recipe.timeout,
            "output": recipe.output_bytes,
            "cpu": recipe.cpu_seconds,
            "file": recipe.file_bytes,
            "fds": recipe.open_files,
            "memory": recipe.memory_mb,
            "worktrees": recipe.allow_worktrees,
            "hard_output": recipe.hard_output_bytes,
        }
        self.assertEqual(recipe.fingerprint(), digest(data))
        self.assertEqual(recipe.fingerprint(legacy_output=True),
                         digest({k: v for k, v in data.items() if k != "hard_output"}))

    def test_new_settings_change_recipe_and_runner_fingerprints(self):
        legacy = builtin_recipe(LAUNCHER, name="legacy")
        targets = builtin_recipe(EXTENDED_LAUNCHER, name="targets",
                                 default_targets=["tests.test_a"])
        minimum = builtin_recipe(EXTENDED_LAUNCHER, name="minimum", min_tests=2)
        both = builtin_recipe(EXTENDED_LAUNCHER, name="both",
                              default_targets=["tests.test_a"], min_tests=2)
        fingerprints = {r.fingerprint() for r in (legacy, targets, minimum, both)}
        self.assertEqual(len(fingerprints), 4)
        runner = TestRunner(self.root, [legacy, targets, minimum, both])
        self.addCleanup(runner.close)
        runner_fingerprints = {runner._fingerprint(r) for r in
                               (legacy, targets, minimum, both)}
        self.assertEqual(len(runner_fingerprints), 4)
        contracts = runner.evidence_contracts(both)
        self.assertIn(runner._fingerprint(both), contracts)
        self.assertNotIn(runner._fingerprint(legacy), contracts)
        self.assertNotIn(runner._fingerprint(targets), contracts)

    def test_selection_environment_binds_options_only_for_default_runs(self):
        roots = ("tests",)
        legacy = selection_environment("unittest", roots, None)
        self.assertEqual(json.loads(legacy["HARNESS_TEST_SELECTION"]),
                         {"framework": "unittest", "roots": ["tests"], "selection": None})
        default = selection_environment("unittest", roots, None,
                                        default_targets=("tests.test_a",), min_tests=3)
        payload = json.loads(default["HARNESS_TEST_SELECTION"])
        self.assertEqual(payload["default_targets"], ["tests.test_a"])
        self.assertEqual(payload["min_tests"], 3)
        selected = selection_environment("unittest", roots,
                                         {"targets": ["tests.test_b"]},
                                         default_targets=("tests.test_a",), min_tests=3)
        self.assertEqual(json.loads(selected["HARNESS_TEST_SELECTION"]),
                         {"framework": "unittest", "roots": ["tests"],
                          "selection": {"targets": ["tests.test_b"]}})


class RegistryAdmission(WorkspaceFixture):
    def setUp(self):
        super().setUp()
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        self.registry_dir = Path(outside.name).resolve()
        self.image = "sha256:" + "a" * 64

    def write_registry(self, recipe, native_launcher=None):
        entry = {"name": "suite", "builtin": "unittest", "authorization": "allow"}
        entry.update(recipe)
        raw = {"version": 1, "recipes": [entry]}
        if native_launcher is not None:
            raw["execution"] = {
                "backend": "container",
                "policy": {
                    "cli": "/nonexistent/docker-cli",
                    "image": self.image,
                    "argv": ["/usr/local/bin/python", "-I", "-c", native_launcher],
                    "native_argv": ["{python}", "-I", "-c", native_launcher],
                },
            }
        path = self.registry_dir / "tests.json"
        path.write_text(json.dumps(raw))
        return path

    def test_legacy_builtin_container_admission_keeps_exact_launcher_and_identity(self):
        path = self.write_registry({}, native_launcher=LAUNCHER)
        runner = load_test_runner(path, self.root)
        self.addCleanup(runner.close)
        recipe = runner.recipes["suite"]
        self.assertEqual(recipe.argv, (sys.executable, "-I", "-c", LAUNCHER))
        self.assertEqual(recipe.locked_globs, BUILTIN_LOCKED_GLOBS)
        self.assertEqual(
            recipe.fingerprint(),
            builtin_recipe(LAUNCHER, locked_globs=BUILTIN_LOCKED_GLOBS).fingerprint(),
        )
        self.assertEqual(runner.backend.name, "container")

    def test_opt_in_container_registry_binds_the_extended_launcher_exactly(self):
        path = self.write_registry(
            {"default_targets": ["tests.test_a"], "min_tests": 2},
            native_launcher=EXTENDED_LAUNCHER,
        )
        runner = load_test_runner(path, self.root)
        self.addCleanup(runner.close)
        recipe = runner.recipes["suite"]
        self.assertEqual(recipe.argv, (sys.executable, "-I", "-c", EXTENDED_LAUNCHER))
        self.assertEqual(recipe.default_targets, ("tests.test_a",))
        self.assertEqual(recipe.min_tests, 2)

    def test_opt_in_registry_with_legacy_native_argv_is_rejected(self):
        path = self.write_registry({"min_tests": 2}, native_launcher=LAUNCHER)
        with self.assertRaises(TestRunnerError):
            load_test_runner(path, self.root)

    def test_legacy_registry_with_extended_native_argv_is_rejected(self):
        path = self.write_registry({}, native_launcher=EXTENDED_LAUNCHER)
        with self.assertRaises(TestRunnerError):
            load_test_runner(path, self.root)

    def test_registry_rejects_string_or_dict_default_targets(self):
        for value in ("tests.test_a", {"targets": ["tests.test_a"]}):
            with self.subTest(value=value):
                path = self.write_registry({"default_targets": value},
                                           native_launcher=EXTENDED_LAUNCHER)
                with self.assertRaises(TestRunnerError):
                    load_test_runner(path, self.root)

    def test_legacy_native_builtin_registry_is_unchanged(self):
        path = self.write_registry({})
        runner = load_test_runner(path, self.root)
        self.addCleanup(runner.close)
        recipe = runner.recipes["suite"]
        self.assertEqual(recipe.argv, (sys.executable, "-I", "-c", LAUNCHER))
        self.assertEqual(recipe.locked_globs, BUILTIN_LOCKED_GLOBS)
        self.assertEqual(
            recipe.fingerprint(),
            builtin_recipe(LAUNCHER, locked_globs=BUILTIN_LOCKED_GLOBS).fingerprint(),
        )


if __name__ == "__main__":
    unittest.main()
