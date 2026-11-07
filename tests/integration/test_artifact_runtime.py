from tests.support.tools import fixture_invocation
"""Tool discovery, Permission/Hook and durable context wiring without model costs."""

import io
import json
import subprocess
import sys
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from homebrew_harness import Capabilities, HarnessSession
from homebrew_harness.execution.background import BackgroundManager
from homebrew_harness.runtime.hooks import HookRegistry
from homebrew_harness.security.permissions import PermissionDecision, check_rules
from homebrew_harness.storage.artifact_store import artifact_context
from homebrew_harness.tools.dispatch import execute_tool
from homebrew_harness.tools.shell import run_bash
from tests.storage import test_artifact_store as store_tests
from tests.support.clients import call, fake_client, reply


class OutputBackend:
    def __init__(self, policy, command, profile=None):
        self.policy = SimpleNamespace(
            argv=("bash", command), native_argv=("bash", command)
        )
        self.profile = profile
        self.inputs = {}
        self.active = {}
        self.lock = threading.RLock()

    def launch(self, request):
        process = subprocess.Popen(
            [sys.executable, "-c", "print('separate stdout')"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self.active[process.pid] = ("fixture", None, request.cwd)
        return process

    def export(self, process):
        return [
            (self.profile.files[0], b"alpha\0\xff"),
            (self.profile.files[1], b"beta"),
        ], []

    def finish(self, process):
        self.active.pop(process.pid, None)
        return self.status()

    def status(self):
        return {
            "configuration_verified": True,
            "os_isolation_verified": True,
            "cleanup_complete": not self.active,
        }

    def wait_closed(self, timeout=0):
        return not self.active


# Reuse setup/helpers, avoiding inherited duplicate store tests.
class ArtifactRuntimeTests(unittest.TestCase):
    setUp = store_tests.ArtifactStoreTests.setUp
    collect = store_tests.ArtifactStoreTests.collect
    publish = store_tests.ArtifactStoreTests.publish

    def session(self, responses=(), **kwargs):
        client, requests = fake_client(responses)
        session = HarnessSession(
            client,
            "fixture",
            self.root,
            bash_policy=self.policy,
            shutdown_timeout=0.03,
            **kwargs,
        )
        self.addCleanup(lambda: session.wait_closed(3))
        self.addCleanup(session.close)
        return session, client, requests

    def invocation(self, name, arguments):
        from homebrew_harness.execution.model_protocol import ToolInvocation
        return ToolInvocation("fixture", name, json.dumps(arguments))

    def test_session_tool_pool_exposes_only_host_registered_rule_names(self):
        with redirect_stderr(io.StringIO()):
            session, _, _ = self.session()
        pool = session.tool_pool()
        self.assertTrue(
            {"list_artifacts", "get_artifact", "publish_artifacts"} <= pool.names
        )
        bash = next(
            t["function"] for t in pool.tools if t["function"]["name"] == "bash"
        )
        self.assertEqual(
            bash["parameters"]["properties"]["artifact_profile"]["enum"], ["report"]
        )
        self.assertIs(session.artifacts, session.shell.artifact_store)

    def test_unregistered_rule_and_model_approval_field_are_rejected(self):
        handlers = {"bash": run_bash, "publish_artifacts": lambda **_: "bad"}
        with patch("homebrew_harness.security.permissions.ask_user") as ask:
            for name, arguments in (
                ("bash", {"command": "fixture", "artifact_profile": "invented"}),
                ("publish_artifacts", {"bundle_id": "fixture", "approval": True}),
            ):
                output = json.loads(
                    execute_tool(
                        self.invocation(name, arguments),
                        self.root,
                        hooks=HookRegistry(),
                        handlers=handlers,
                        artifact_store=self.store,
                    )
                )
                self.assertIn("error", output)
            ask.assert_not_called()

    def test_custom_allow_hook_cannot_forge_publication_approval(self):
        bundle = self.collect()
        output = execute_tool(
            self.invocation("publish_artifacts", {"bundle_id": bundle["bundle_id"]}),
            self.root,
            hooks=HookRegistry(),
            handlers={"publish_artifacts": lambda **_: "bad"},
            artifact_store=self.store,
        )
        self.assertIn("recorded exact host approval", output)
        self.assertFalse((self.root / "reports").exists())

    def test_real_session_collect_inspect_authorize_and_publish(self):
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            session, client, _ = self.session()
            responses = 0

            def create(**kwargs):
                nonlocal responses
                responses += 1
                if responses == 1:
                    return reply(
                        calls=[
                            call(
                                "bash",
                                name="bash",
                                arguments=json.dumps(
                                    {"command": "produce", "artifact_profile": "report"}
                                ),
                            )
                        ]
                    )
                if responses == 2:
                    bundle = session.artifacts.list(workdir=self.root)["bundles"][0]
                    self.assertFalse((self.root / "reports").exists())
                    return reply(
                        calls=[
                            call(
                                "publish",
                                name="publish_artifacts",
                                arguments=json.dumps(
                                    {"bundle_id": bundle["bundle_id"]}
                                ),
                            )
                        ]
                    )
                return reply("done")

            client.chat.completions.create.side_effect = create
            with (
                patch(
                    "homebrew_harness.execution.bash_runner.BashContainerBackend",
                    OutputBackend,
                ),
                patch(
                    "homebrew_harness.security.permissions.ask_user",
                    return_value=PermissionDecision("allow", "fixture"),
                ) as ask,
            ):
                self.assertEqual(session.run_user("Produce and publish report"), "done")
                self.assertEqual(ask.call_count, 2)
            self.assertEqual((self.root / "reports/a").read_bytes(), b"alpha\0\xff")
            self.assertTrue(session.shell.wait_closed(1))

    def test_background_bundle_retains_original_task_assignment(self):
        manager = BackgroundManager(
            self.root, run_bash, bash_policy=self.policy, artifact_store=self.store
        )
        self.addCleanup(manager.close)
        with patch(
            "homebrew_harness.execution.bash_runner.BashContainerBackend", OutputBackend
        ):
            identity = manager.start(
                "produce",
                artifact_profile="report",
                assignment={"task_id": "task_12345678"},
            )["bg_id"]
            self.assertTrue(manager._workers.wait(3))
        result = json.loads(manager.check(identity, workdir=self.root))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.store.list(workdir=self.root)["bundles"], [])
        self.assertEqual(
            len(self.store.list(workdir=self.root, task_id="task_12345678")["bundles"]),
            1,
        )

    def test_denied_publication_and_raw_archive_access_leave_host_untouched(self):
        with redirect_stderr(io.StringIO()):
            session, _, _ = self.session()
            bundle = session.artifacts.collect(
                "fixture",
                self.root,
                self.rule,
                [(self.rule.files[0], b"alpha")],
                ["output/b"],
            )
            with patch(
                "homebrew_harness.security.permissions.ask_user",
                return_value=PermissionDecision("deny", "fixture"),
            ):
                output = execute_tool(
                    self.invocation(
                        "publish_artifacts", {"bundle_id": bundle["bundle_id"]}
                    ),
                    self.root,
                    hooks=session.hooks,
                    handlers=session.tool_pool().handlers,
                    artifact_store=session.artifacts,
                )
        self.assertIn("Permission denied", output)
        self.assertFalse((self.root / "reports").exists())
        self.assertEqual(
            check_rules(
                "read_file", {"path": bundle["files"][0]["reference"]}, self.root
            ).action,
            "deny",
        )

    def test_context_task_identity_is_not_a_model_argument(self):
        with artifact_context("task_12345678"):
            bundle = self.collect(task_id="task_12345678")
        self.assertEqual(self.store.list(workdir=self.root)["bundles"], [])
        self.assertEqual(
            self.store.get(
                bundle["bundle_id"], workdir=self.root, task_id="task_12345678"
            )["bundle_id"],
            bundle["bundle_id"],
        )

    def test_host_mode_omits_artifact_tools_and_output_profile(self):
        from homebrew_harness.execution.bash_policy import HOST_POLICY

        client, _ = fake_client([])
        with redirect_stderr(io.StringIO()):
            session = HarnessSession(
                client, "fixture", self.root, bash_policy=HOST_POLICY
            )
        self.addCleanup(session.close)
        pool = session.tool_pool()
        self.assertFalse(
            {"list_artifacts", "get_artifact", "publish_artifacts"} & pool.names
        )
        bash = next(
            t["function"] for t in pool.tools if t["function"]["name"] == "bash"
        )
        self.assertNotIn("artifact_profile", bash["parameters"]["properties"])

    def test_resume_session_keeps_bundle_owner_and_original_output_policy(self):
        with redirect_stderr(io.StringIO()):
            session, _, _ = self.session()
            session.run_user("/usage")
            bundle = session.artifacts.collect(
                "fixture",
                self.root,
                self.rule,
                [(self.rule.files[0], b"alpha")],
                ["output/b"],
            )
            sid = session.session_id
            session.close()
            self.assertTrue(session.wait_closed(3))
            resumed, client, _ = self.session(resume_session=sid)
            resumed.run_user("/usage")
            self.assertEqual(
                resumed.artifacts.get(bundle["bundle_id"], workdir=self.root)[
                    "bundle_id"
                ],
                bundle["bundle_id"],
            )
            client.chat.completions.create.assert_not_called()

    def test_subagent_collects_with_shared_store_and_independent_context(self):
        responses = [
            reply(
                calls=[
                    call(
                        "task",
                        name="task",
                        arguments=json.dumps({"prompt": "Produce report"}),
                    )
                ]
            ),
            reply(
                calls=[
                    call(
                        "produce",
                        name="bash",
                        arguments=json.dumps(
                            {"command": "fixture", "artifact_profile": "report"}
                        ),
                    )
                ]
            ),
            reply("child done"),
            reply("done"),
        ]
        with (
            redirect_stderr(io.StringIO()),
            redirect_stdout(io.StringIO()),
            patch(
                "homebrew_harness.execution.bash_runner.BashContainerBackend",
                OutputBackend,
            ),
            patch(
                "homebrew_harness.security.permissions.ask_user",
                return_value=PermissionDecision("allow", "fixture"),
            ),
        ):
            session, _, _ = self.session(
                responses, capabilities=Capabilities({"subagents"})
            )
            self.assertEqual(session.run_user("Produce with a subagent"), "done")
            self.assertEqual(
                len(session.artifacts.list(workdir=self.root)["bundles"]), 1
            )
            self.assertFalse((self.root / "reports").exists())
