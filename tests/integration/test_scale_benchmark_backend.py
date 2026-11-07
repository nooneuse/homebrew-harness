import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import homebrew_harness.tools.shell
from homebrew_harness.execution.execution_backend import (
    ContainerBackend,
    ContainerPolicy,
    ExecutionRequest,
    BackendUnavailable,
)
from homebrew_harness.execution.model_gateway import Deadline
from homebrew_harness.benchmark import run_benchmark, verify, TASKS
from homebrew_harness.execution.test_runner import TestRecipe, TestRunner
from tests.support.clients import fake_client, reply, call
from homebrew_harness.admin import main, external_model_config
from contextlib import redirect_stderr


class BenchmarkTests(unittest.TestCase):
    def test_offline_protocol_and_independent_verifiers_pass(self):
        result = run_benchmark()
        self.assertTrue(result["passed"])
        self.assertEqual(result["usage"]["calls"], 3)
        self.assertEqual(result["quality_claim"], "protocol_and_verifier_only")

    def test_final_claim_without_artifact_fails(self):
        client, _ = fake_client([reply("Everything is complete.")] * 3)
        result = run_benchmark(client, "fixture")
        self.assertFalse(result["passed"])
        self.assertTrue(all(not task["checks"]["output"] for task in result["tasks"]))

    def test_turn_limit_records_uncovered_tasks(self):
        result = run_benchmark(
            max_turns=1, client=fake_client([reply("unfinished")])[0]
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["usage"]["calls"], 1)
        self.assertEqual(len(result["uncovered"]), 2)

    def test_invalid_runtime_limits_and_removed_budgets_are_rejected(self):
        for kwargs in ({"max_turns": 101}, {"count": 4}, {"timeout_seconds": 0}):
            with self.assertRaises(ValueError):
                run_benchmark(**kwargs)
        for kwargs in ({"max_calls": 1}, {"max_tokens": 100}):
            with self.assertRaises(TypeError):
                run_benchmark(**kwargs)

    def test_invented_unoffered_bash_cannot_execute(self):
        client, _ = fake_client(
            [reply(calls=[call("bad", "touch bad.txt")]), reply("done")] * 3
        )
        with patch(
            "homebrew_harness.tools.shell.run_bash",
            side_effect=AssertionError("must not run"),
        ):
            result = run_benchmark(client, "fixture")
        self.assertFalse(result["passed"])

    def test_verifier_detects_constraint_violation_even_with_correct_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            task = TASKS[0]
            for name, value in task["fixtures"].items():
                (root / name).write_text(value)
            (root / "answer.json").write_text(json.dumps(task["expected"]))
            (root / "protected.txt").write_text("changed")
            self.assertFalse(verify(root, task)["ok"])

    def test_admin_report_cannot_overwrite_backend_marker(self):
        with tempfile.TemporaryDirectory() as temp, redirect_stderr(io.StringIO()):
            self.assertEqual(
                main(
                    [
                        "inventory",
                        "--cwd",
                        temp,
                        "--output",
                        ".harness/runtime/backend.json",
                    ]
                ),
                1,
            )
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_private_config_cannot_be_read_from_model_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            config = root / ".env"
            config.write_text("OPENAI_MODEL=fixture")
            with self.assertRaises(ValueError):
                external_model_config(config, root)


class BackendTests(unittest.TestCase):
    def policy(self, argv):
        return ContainerPolicy(
            "/missing-container-cli",
            "fixture@sha256:" + "a" * 64,
            ("/usr/local/bin/python", "-c", "print(1)"),
            argv,
        )

    def test_missing_container_never_executes_native_recipe(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            argv = (sys.executable, "-c", 'open("effect.txt","w").write("unsafe")')
            backend = ContainerBackend(self.policy(argv))
            runner = TestRunner(root, [TestRecipe("fixed", argv)], backend=backend)
            runner.approve("fixed")
            result = runner.run("fixed")
            runner.close()
            self.assertEqual(result["status"], "error")
            self.assertFalse((root / "effect.txt").exists())
            self.assertFalse(backend.probe()["available"])
            self.assertTrue(runner.wait_closed(0.1))

    def test_policy_identity_changes_authorization(self):
        argv = (sys.executable, "-c", "print(1)")
        first = ContainerBackend(self.policy(argv))
        second = ContainerBackend(
            ContainerPolicy(
                "/missing-container-cli",
                "fixture@sha256:" + "b" * 64,
                ("/usr/local/bin/python", "-c", "print(1)"),
                argv,
            )
        )
        self.assertNotEqual(first.fingerprint(), second.fingerprint())

    def test_container_request_must_match_exact_host_recipe(self):
        backend = ContainerBackend(self.policy((sys.executable, "-c", "print(1)")))
        with (
            tempfile.TemporaryDirectory() as temp,
            self.assertRaises(BackendUnavailable),
        ):
            backend.launch(
                ExecutionRequest(
                    ("ignored",),
                    Path(temp).resolve(),
                    {},
                    Deadline.after(1),
                    recipe_argv=("other",),
                )
            )

    def test_mutable_image_and_privileged_uid_are_refused(self):
        with self.assertRaises(ValueError):
            ContainerPolicy("/missing", "fixture:latest", ("/bin/true",), ("/native",))
        with self.assertRaises(ValueError):
            ContainerPolicy(
                "/missing",
                "fixture@sha256:" + "a" * 64,
                ("/bin/true",),
                ("/native",),
                uid=0,
            )
