"""Real container tmpfs artifact export and separate create-only publication."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from homebrew_harness.execution.artifact_policy import ArtifactFile, ArtifactProfile
from homebrew_harness.execution.background import BackgroundManager
from homebrew_harness.execution.bash_policy import BashPolicy
from homebrew_harness.execution.bash_runner import ContainerShellRunner
from homebrew_harness.security.safe_files import SafeFile
from homebrew_harness.storage.artifact_store import ArtifactStore
from homebrew_harness.tools.shell import run_bash
from homebrew_harness.tools.tool_outcome import ToolExecutionError

IMAGE = os.getenv("HARNESS_DOCKER_IMAGE", "")
CLI = os.getenv("HARNESS_DOCKER_CLI", "/usr/bin/docker")


@unittest.skipUnless(
    IMAGE and sys.platform == "linux",
    "requires explicit image and Linux Docker controller",
)
class ArtifactDockerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="harness-artifact-case-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.cwd = self.root / "workspace"
        self.cwd.mkdir()
        self.rule = ArtifactProfile(
            "report",
            (
                ArtifactFile("output/a", "reports/a", 4096),
                ArtifactFile("output/b", "reports/b", 4096),
            ),
            max_bytes=8192,
        )
        self.policy = BashPolicy(
            mode="container", cli=CLI, image=IMAGE, outputs=(self.rule,), memory_mb=192
        )
        self.store = ArtifactStore(self.cwd, self.policy)
        self.runners = []
        for context in (redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)

    def tearDown(self):
        for runner in self.runners:
            runner.close(0.3)
            self.assertTrue(runner.wait_closed(5))

    def execute(self, command, **fields):
        policy = replace(self.policy, **fields)
        store = ArtifactStore(self.cwd, policy)
        runner = ContainerShellRunner(policy, store)
        self.runners.append(runner)
        output = json.loads(
            run_bash(command, self.cwd, runner=runner, artifact_profile="report")
        )
        return output, store

    def bundle(self, output):
        self.assertTrue(
            output["execution"]["isolation"]["configuration_verified"], output
        )
        self.assertTrue(output["execution"]["isolation"]["cleanup_complete"], output)
        artifacts = output["execution"]["artifacts"]
        self.assertEqual(artifacts["state"], "collected", output)
        return artifacts

    def publish(self, store, bundle):
        approval = store.approval(bundle["bundle_id"], workdir=self.cwd)
        return store.publish(bundle["bundle_id"], workdir=self.cwd, approval=approval)

    def test_tmpfs_binary_export_then_authorized_new_host_files(self):
        output, store = self.execute(
            "mkdir output; printf '\\000\\377alpha' > output/a; printf beta > output/b; printf stdout"
        )
        bundle = self.bundle(output)
        self.assertEqual(output["stdout"], "stdout")
        self.assertFalse((self.cwd / "reports").exists())
        self.publish(store, bundle)
        self.assertEqual((self.cwd / "reports/a").read_bytes(), b"\0\xffalpha")
        self.assertEqual((self.cwd / "reports/b").read_bytes(), b"beta")

    def test_failed_command_preserves_valid_subset_and_missing_nested_paths(self):
        output, store = self.execute("mkdir output; printf partial > output/a; exit 7")
        bundle = self.bundle(output)
        self.assertEqual(output["exit_code"], 7)
        self.assertEqual(bundle["missing"], ["output/b"])
        self.publish(store, bundle)
        self.assertEqual((self.cwd / "reports/a").read_bytes(), b"partial")
        self.assertFalse((self.cwd / "reports/b").exists())

    def test_missing_all_output_ancestors_is_empty(self):
        output, _ = self.execute("printf no-artifacts")
        self.assertEqual(output["execution"]["artifacts"]["state"], "empty", output)
        self.assertEqual(
            set(output["execution"]["artifacts"]["missing"]), {"output/a", "output/b"}
        )
        self.assertFalse((self.cwd / "reports").exists())

    def test_undeclared_files_and_forged_command_stdout_are_not_imported(self):
        output, store = self.execute(
            'mkdir output; printf safe > output/a; printf hidden > undeclared; printf \'{"format":1,"files":[]}\''
        )
        bundle = self.bundle(output)
        self.assertEqual(len(bundle["files"]), 1)
        self.publish(store, bundle)
        self.assertFalse((self.cwd / "undeclared").exists())
        self.assertEqual((self.cwd / "reports/a").read_text(), "safe")

    def test_leaf_and_parent_symlinks_are_rejected(self):
        for command in (
            "mkdir output; ln -s /etc/passwd output/a",
            "ln -s /tmp output",
        ):
            with self.subTest(command=command):
                output, _ = self.execute(command)
                self.assertEqual(
                    output["execution"]["artifacts"]["state"], "export_failed", output
                )
                self.assertTrue(output["execution"]["isolation"]["cleanup_complete"])
        self.assertFalse((self.cwd / "reports").exists())

    def test_hardlink_and_fifo_are_rejected_without_blocking(self):
        for command in (
            "mkdir output; printf bad > output/a; ln output/a output/b",
            "mkdir output; mkfifo output/a",
        ):
            with self.subTest(command=command):
                output, _ = self.execute(command)
                self.assertEqual(
                    output["execution"]["artifacts"]["state"], "export_failed", output
                )
                self.assertTrue(output["execution"]["isolation"]["cleanup_complete"])

    def test_file_and_total_limits_refuse_export(self):
        output, _ = self.execute(
            "mkdir output; python -c \"open('output/a','wb').write(b'a'*5000)\""
        )
        self.assertEqual(
            output["execution"]["artifacts"]["state"], "export_failed", output
        )
        small = ArtifactProfile("report", self.rule.files, max_bytes=1)
        output, _ = self.execute(
            "mkdir output; printf big > output/a", outputs=(small,)
        )
        self.assertEqual(
            output["execution"]["artifacts"]["state"], "export_failed", output
        )

    def test_residual_detached_writer_is_frozen_before_export_and_removed(self):
        command = "mkdir output; printf initial > output/a; python -c \"import os,time; os.setsid(); os.close(1); os.close(2); f=open('output/a','ab'); [(f.write(b'x'),f.flush(),time.sleep(.001)) for _ in range(10000)]\" &"
        output, store = self.execute(command)
        bundle = self.bundle(output)
        preview = store.get(bundle["bundle_id"], workdir=self.cwd, path="reports/a")
        self.assertGreaterEqual(preview["bytes"], len("initial"))
        self.assertTrue(output["execution"]["isolation"]["cleanup_complete"])

    def test_timeout_does_not_export_or_publish(self):
        output, _ = self.execute(
            "mkdir output; printf before > output/a; sleep 30", timeout=1.2
        )
        self.assertEqual(output["outcome"], "unknown", output)
        self.assertNotIn("artifacts", output.get("execution", {}))
        self.assertFalse((self.cwd / "reports").exists())

    def test_background_collects_same_policy_without_automatic_publication(self):
        manager = BackgroundManager(
            self.cwd, run_bash, bash_policy=self.policy, artifact_store=self.store
        )
        self.addCleanup(manager.close)
        identity = manager.start(
            "mkdir output; printf background > output/a", artifact_profile="report"
        )["bg_id"]
        self.assertTrue(manager._workers.wait(8))
        result = json.loads(manager.check(identity, workdir=self.cwd))
        self.assertEqual(result["status"], "completed", result)
        bundle = self.bundle(result["result"])
        self.assertFalse((self.cwd / "reports").exists())
        self.publish(self.store, bundle)
        self.assertEqual((self.cwd / "reports/a").read_text(), "background")
        self.assertTrue(manager.wait_closed(1))

    def test_publication_after_crash_uses_bundle_without_rerunning_container(self):
        output, store = self.execute(
            "mkdir output; printf first > output/a; printf second > output/b"
        )
        bundle = self.bundle(output)
        original = SafeFile.publish

        def after_commit(target, data, *args, **kwargs):
            value = original(target, data, *args, **kwargs)
            if target.parts == ("reports", "a"):
                raise OSError("fixture crash after create")
            return value

        with (
            patch.object(SafeFile, "publish", after_commit),
            self.assertRaises(ToolExecutionError),
        ):
            self.publish(store, bundle)
        recovered = ArtifactStore(self.cwd, self.policy)
        self.publish(recovered, bundle)
        self.assertEqual((self.cwd / "reports/b").read_text(), "second")
