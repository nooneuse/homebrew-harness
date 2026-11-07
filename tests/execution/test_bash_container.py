"""Bash target, source isolation, inheritance and cleanup contracts; no remote model."""

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
from unittest.mock import Mock, patch
from types import SimpleNamespace

from homebrew_harness import HarnessSession, Capabilities, LoopComponents, agent_loop
from homebrew_harness.execution.bash_policy import (
    BashPolicy,
    BashPolicyError,
    HOST_POLICY,
    bash_policy_scope,
    bash_snapshot,
    current_bash_policy,
    load_bash_policy,
)
from homebrew_harness.execution.bash_runner import ContainerShellRunner
from homebrew_harness.execution.background import BackgroundManager
from homebrew_harness.execution.model_gateway import Deadline, ModelCancelled
from homebrew_harness.security.permissions import PermissionDecision, check_rules
from homebrew_harness.storage.durable_runtime import RecoveryRequired
from homebrew_harness.tools.shell import run_bash
from tests.support.clients import fake_client, reply, call


class FakeBackend:
    instances = []
    cleanup_gate = None

    def __init__(self, policy, command):
        self.policy = SimpleNamespace(
            argv=("bash", command), native_argv=("bash", command)
        )
        self.command = command
        self.inputs = {"included_files": 1}
        self.active = False
        self.request = None
        self.instances.append(self)

    def launch(self, request):
        self.request = request
        self.active = True
        return subprocess.Popen(
            [sys.executable, "-c", "print('sandbox result')"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

    def finish(self, process):
        self.active = False
        return {
            "backend": "container",
            "configuration_verified": True,
            "os_isolation_verified": True,
            "cleanup_complete": self.cleanup_gate is None or self.cleanup_gate.is_set(),
        }

    def status(self):
        return {
            "backend": "container",
            "cleanup_complete": not self.active
            and (self.cleanup_gate is None or self.cleanup_gate.is_set()),
        }

    def wait_closed(self, timeout=0):
        return not self.active and (
            self.cleanup_gate is None or self.cleanup_gate.wait(timeout)
        )


class BashContainerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.outer = self.root / "workspace"
        self.outer.mkdir()
        self.policy = BashPolicy(
            mode="container", cli="/missing-docker-cli", image="sha256:" + "a" * 64
        )
        FakeBackend.instances = []
        FakeBackend.cleanup_gate = None
        for context in (redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)

    def session(self, responses=(), **kwargs):
        client, requests = fake_client(responses)
        session = HarnessSession(
            client,
            "fixture",
            self.outer,
            bash_policy=self.policy,
            shutdown_timeout=0.03,
            **kwargs,
        )
        self.addCleanup(lambda: session.wait_closed(3))
        self.addCleanup(session.close)
        return session, client, requests

    def test_policy_rejects_mutable_image_privileged_uid_and_model_supplied_fields(
        self,
    ):
        invalid = [
            {"image": "image:latest"},
            {"uid": 0},
            {"pids": True},
            {"cpus": float("nan")},
            {"workspace_bytes": 0},
        ]
        for fields in invalid:
            with self.subTest(fields=fields), self.assertRaises(BashPolicyError):
                replace(self.policy, **fields)
        path = self.root / "policy.json"
        path.write_text(
            json.dumps(
                {
                    "mode": "container",
                    "cli": "/missing",
                    "image": "sha256:" + "a" * 64,
                    "mounts": ["/"],
                }
            )
        )
        with self.assertRaises(BashPolicyError):
            load_bash_policy(path, self.outer)

    def test_configuration_is_explicit_and_outside_workspace(self):
        path = self.root / "policy.json"
        path.write_text(json.dumps({"mode": "host"}))
        self.assertEqual(
            load_bash_policy(path, self.outer).identity(), HOST_POLICY.identity()
        )
        forbidden = self.outer / "policy.json"
        forbidden.write_text('{"mode":"host"}')
        with self.assertRaises(BashPolicyError):
            load_bash_policy(forbidden, self.outer)
        with self.assertRaises(BashPolicyError):
            load_bash_policy(
                self.root / "absent", self.outer, mode="container", missing_ok=True
            )

    def test_delegation_cannot_choose_host_or_different_image(self):
        with bash_policy_scope(self.policy):
            self.assertIs(current_bash_policy(), self.policy)
            for child in (
                HOST_POLICY,
                replace(self.policy, image="sha256:" + "b" * 64),
            ):
                with self.assertRaises(BashPolicyError):
                    with bash_policy_scope(child):
                        pass
            with bash_policy_scope(replace(self.policy, timeout=30)):
                self.assertEqual(current_bash_policy().timeout, 30)
        self.assertEqual(current_bash_policy().mode, "host")

    def test_snapshot_keeps_current_and_untracked_files_without_reading_internal_state(
        self,
    ):
        (self.outer / "tracked.txt").write_text("current uncommitted content")
        (self.outer / "untracked.txt").write_text("new ordinary file")
        for name in (".git", ".harness", ".memory", ".venv"):
            (self.outer / name).mkdir()
            (self.outer / name / "private.txt").write_text("must not enter")
        (self.outer / ".env").write_text("private configuration")
        (self.outer / ".netrc").write_text("private login")
        (self.outer / "key.pem").write_text("private key material")
        (self.outer / "link").symlink_to(self.root)
        os.mkfifo(self.outer / "fifo")
        snapshot, stats = bash_snapshot(self.outer, self.policy)
        self.assertEqual(set(snapshot), {"tracked.txt", "untracked.txt"})
        self.assertGreaterEqual(stats["omitted_entries"], 7)

    def test_sensitive_body_is_omitted_and_size_limits_fail_before_execution(self):
        (self.outer / "settings.txt").write_text("api_key=synthetic-private-material")
        (self.outer / "input.txt").write_text("ordinary input")
        snapshot, _ = bash_snapshot(self.outer, self.policy)
        self.assertEqual(set(snapshot), {"input.txt"})
        (self.outer / "large").write_bytes(b"a" * 2000)
        with self.assertRaises((BashPolicyError, ValueError)):
            bash_snapshot(self.outer, replace(self.policy, max_input_bytes=1024))

    def test_expired_snapshot_deadline_stops(self):
        (self.outer / "input").write_text("fixture")
        with self.assertRaises(Exception):
            bash_snapshot(self.outer, self.policy, Deadline.after(0))

    def test_missing_backend_never_runs_host_command(self):
        runner = ContainerShellRunner(self.policy)
        self.addCleanup(runner.close)
        output = json.loads(run_bash("touch host-effect", self.outer, runner=runner))
        self.assertIn("error", output)
        self.assertFalse(output["executed"])
        self.assertFalse((self.outer / "host-effect").exists())
        self.assertTrue(runner.wait_closed(1))

    def test_container_environment_and_result_are_separate_from_host(self):
        with patch(
            "homebrew_harness.execution.bash_runner.BashContainerBackend", FakeBackend
        ):
            runner = ContainerShellRunner(self.policy)
            self.addCleanup(runner.close)
            output = json.loads(run_bash("printf fixture", self.outer, runner=runner))
        self.assertEqual(output["exit_code"], 0)
        self.assertFalse(output["execution"]["writeback"])
        self.assertEqual(output["execution"]["workspace"], "isolated_copy")
        self.assertEqual(FakeBackend.instances[0].request.env, {"LANG": "C.UTF-8"})
        self.assertTrue(runner.wait_closed(1))

    def test_extra_model_backend_option_is_rejected_before_launch(self):
        with (
            patch(
                "homebrew_harness.execution.bash_runner.BashContainerBackend",
                FakeBackend,
            ),
            patch(
                "homebrew_harness.security.permissions.ask_user",
                return_value=PermissionDecision("allow", "fixture"),
            ),
        ):
            session, _, _ = self.session(
                [
                    reply(
                        calls=[
                            call(
                                "bad",
                                name="bash",
                                arguments='{"command":"printf fixture","mode":"host"}',
                            )
                        ]
                    ),
                    reply("done"),
                ]
            )
            self.assertEqual(session.run_user("Read fixture"), "done")
        self.assertFalse(FakeBackend.instances)

    def test_permission_still_requires_terminal_approval_and_describes_target(self):
        with bash_policy_scope(self.policy):
            decision = check_rules("bash", {"command": "printf fixture"}, self.outer)
        self.assertEqual(decision.action, "ask")
        self.assertIn("container workspace copy", decision.reason)
        self.assertEqual(
            check_rules("bash", {"command": "printf fixture"}, self.outer).action, "ask"
        )

    def test_real_entry_tool_pool_describes_copy_and_rejects_denied_command(self):
        with (
            patch(
                "homebrew_harness.execution.bash_runner.BashContainerBackend",
                FakeBackend,
            ),
            patch(
                "homebrew_harness.security.permissions.ask_user",
                return_value=PermissionDecision("deny", "fixture"),
            ),
        ):
            session, _, _ = self.session(
                [reply(calls=[call("bash", "printf fixture")]), reply("done")]
            )
            self.assertEqual(session.run_user("Read fixture"), "done")
            tool = next(
                item["function"]
                for item in session.tool_pool().tools
                if item["function"]["name"] == "bash"
            )
            self.assertIn("no host-file writeback", tool["description"])
        self.assertFalse(FakeBackend.instances)

    def test_subagent_uses_parent_policy_and_shared_execution_owner(self):
        responses = [
            reply(
                calls=[call("task", name="task", arguments='{"prompt":"Run fixture"}')]
            ),
            reply(calls=[call("bash", "printf fixture")]),
            reply("child done"),
            reply("done"),
        ]
        with (
            patch(
                "homebrew_harness.execution.bash_runner.BashContainerBackend",
                FakeBackend,
            ),
            patch(
                "homebrew_harness.security.permissions.ask_user",
                return_value=PermissionDecision("allow", "fixture"),
            ),
        ):
            session, _, _ = self.session(
                responses, capabilities=Capabilities({"subagents"})
            )
            self.assertEqual(session.run_user("Run a child fixture"), "done")
        self.assertEqual(len(FakeBackend.instances), 1)
        self.assertEqual(session.shell.bash_policy, self.policy)
        self.assertTrue(session.shell.wait_closed(1))

    def test_background_executes_with_the_same_policy(self):
        with patch(
            "homebrew_harness.execution.bash_runner.BashContainerBackend", FakeBackend
        ):
            manager = BackgroundManager(self.outer, run_bash, bash_policy=self.policy)
            self.addCleanup(manager.close)
            identity = manager.start("printf fixture")["bg_id"]
            self.assertTrue(manager._workers.wait(2))
            result = json.loads(manager.check(identity, workdir=self.outer))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["execution"]["mode"], "container")
        self.assertEqual(manager.runner.bash_policy, self.policy)

    def test_injected_native_runner_cannot_replace_container_policy(self):
        native = BackgroundManager(self.outer, run_bash)
        self.addCleanup(native.close)
        with self.assertRaises(BashPolicyError):
            self.session(background_manager=native)

    def test_configuration_fingerprint_is_preserved_on_resume(self):
        session, _, _ = self.session()
        session.run_user("/usage")
        identity = session.session_id
        session.close()
        self.assertTrue(session.wait_closed(2))
        client, _ = fake_client([])
        resumed = HarnessSession(
            client,
            "fixture",
            self.outer,
            resume_session=identity,
            bash_policy=HOST_POLICY,
            shutdown_timeout=0.03,
        )
        self.addCleanup(lambda: resumed.wait_closed(2))
        self.addCleanup(resumed.close)
        with self.assertRaises(RecoveryRequired):
            resumed.run_user("/usage")
        client.chat.completions.create.assert_not_called()

    def test_cleanup_failure_keeps_active_recovery_and_session_lease(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        FakeBackend.cleanup_gate = gate
        with (
            patch(
                "homebrew_harness.execution.bash_runner.BashContainerBackend",
                FakeBackend,
            ),
            patch(
                "homebrew_harness.security.permissions.ask_user",
                return_value=PermissionDecision("allow", "fixture"),
            ),
        ):
            session, _, _ = self.session(
                [reply(calls=[call("bash", "printf fixture")])]
            )
            with self.assertRaises(RecoveryRequired):
                session.run_user("Run fixture")
            row = session.recovery_operations()[0]
            self.assertTrue(row["active"])
            with self.assertRaises(RecoveryRequired):
                session.run_user(
                    "/recovery inspect "
                    + row["operation_id"]
                    + " "
                    + str(row["revision"])
                )
            started = time.monotonic()
            session.close()
            self.assertLess(time.monotonic() - started, 0.3)
            self.assertFalse(session.wait_closed(0.01))
            self.assertTrue(session.workspace.leases.held("session"))
            gate.set()
            self.assertTrue(session.wait_closed(3))
            self.assertFalse(session.workspace.leases.held("session"))

    def test_container_background_cannot_use_an_extension_which_ignores_runner(self):
        manager = BackgroundManager(
            self.outer,
            lambda *args, **kwargs: '{"exit_code":0}',
            bash_policy=self.policy,
        )
        self.addCleanup(manager.close)
        with self.assertRaises(BashPolicyError):
            self.session(background_manager=manager)

    def test_missing_configuration_is_a_safe_policy_error(self):
        with self.assertRaises(BashPolicyError):
            load_bash_policy(self.root / "missing", self.outer)

    def test_invalid_image_type_and_host_container_fields_are_rejected(self):
        with self.assertRaises(BashPolicyError):
            replace(self.policy, image=None)
        path = self.root / "host-policy"
        path.write_text('{"mode":"host","cpus":2}')
        with self.assertRaises(BashPolicyError):
            load_bash_policy(path, self.outer)

    def test_interruption_during_finish_does_not_leave_unowned_process(self):
        from homebrew_harness.execution.background import ShellRunner

        class Interrupted(ShellRunner):
            def _finish(self, process):
                raise KeyboardInterrupt("fixture cleanup interruption")

        runner = Interrupted()
        self.addCleanup(runner.close)
        with self.assertRaises(KeyboardInterrupt):
            runner.run("printf fixture", self.outer, {}, 2)
        self.assertTrue(runner.wait_closed(1))

    def test_prepare_failure_with_pending_cleanup_returns_structured_unknown(self):
        gate = threading.Event()
        self.addCleanup(gate.set)

        class Failed(FakeBackend):
            cleanup_gate = gate

            def launch(self, request):
                self.request = request
                from homebrew_harness.execution.execution_backend import (
                    BackendUnavailable,
                )

                raise BackendUnavailable("fixture create uncertainty")

        with patch(
            "homebrew_harness.execution.bash_runner.BashContainerBackend", Failed
        ):
            runner = ContainerShellRunner(self.policy)
            self.addCleanup(runner.close)
            output = json.loads(run_bash("printf fixture", self.outer, runner=runner))
            self.assertEqual(output["outcome"], "unknown")
            self.assertEqual(output["details"]["stage"], "bash_container_prepare")
            self.assertFalse(runner.wait_closed(0.01))
            gate.set()
            self.assertTrue(runner.wait_closed(2))

    def test_explicit_host_mode_runs_in_assigned_cwd(self):
        output = json.loads(run_bash("printf native", self.outer))
        self.assertEqual(output["exit_code"], 0)
        self.assertEqual(output["stdout"], "native")
