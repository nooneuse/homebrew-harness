"""The coding preset compiles authority and offline checks without external calls."""

from dataclasses import replace
import json
import shutil
import unittest
from unittest.mock import patch

from homebrew_harness.execution.bash_policy import BashPolicy, BashPolicyError, bash_policy_scope
from homebrew_harness.execution.test_runner import load_test_runner
from homebrew_harness.runs.spec import DEFAULT_TOOLS
from homebrew_harness.runtime.interaction_config import InteractionConfig
from homebrew_harness.security.permissions import check_permission, check_rules
from homebrew_harness.services.self_maintenance import _check_local_image
from homebrew_harness.services.spec import ServiceSpec
from homebrew_harness.tools.shell import run_bash
from tests.execution import test_bash_container
from tests.runs import test_human_control
from tests.services import test_setup_validation
from tests.support.clients import call, reply


class PresetTests(unittest.TestCase):
    setUp = test_setup_validation.SetupValidationTests.setUp
    save_tests = test_setup_validation.SetupValidationTests.save_tests
    save = test_setup_validation.SetupValidationTests.save
    validate = test_setup_validation.SetupValidationTests.validate

    def isolated_files(self):
        self.spec["preset"] = "self_maintenance"
        self.bash = self.private / "bash.json"
        self.host["bash_config"] = str(self.bash)
        self.bash.write_text(json.dumps({
            "mode": "container", "cli": shutil.which("git"), "image": "sha256:" + "a" * 64,
            "preauthorized": True,
        }))
        native_argv = load_test_runner(self.test_file, self.workspace).recipes["unit-tests"].argv
        self.test_file.write_text(json.dumps({"version": 1, "recipes": [self.recipe],
            "execution": {"backend": "container", "policy": {
                "cli": shutil.which("git"), "image": "sha256:" + "a" * 64,
                "native_argv": list(native_argv), "argv": ["/usr/local/bin/python", "-m", "unittest"],
            }},
        }))

    def test_preset_compiles_without_inheriting_publication_or_merge_authority(self):
        self.isolated_files()
        self.host["runtime_limits"] = {"file_read_chars": 18000, "auto_compact": False}
        self.spec["run_defaults"]["runtime_limits"] = {"file_read_chars": 22000}
        with patch("homebrew_harness.execution.execution_backend.ContainerBackend.probe",
                   side_effect=AssertionError("offline validation contacted a daemon")):
            report = self.validate()
        self.assertEqual(report["merge_mode"], "manual")
        caps = report["capabilities"]
        self.assertEqual(caps["base_update"], "periodic")
        self.assertEqual(caps["shell"], "preauthorized_container_copy")
        self.assertFalse(caps["shell_network"])
        self.assertFalse(caps["shell_workspace_writeback"])
        self.assertFalse(caps["auto_compact"])
        self.assertTrue(caps["independent_review"])
        self.assertIn("bash", caps["tools"])
        self.assertIn("read_execution_output", caps["tools"])
        self.assertIn("get_artifact", caps["tools"])
        self.assertNotIn("publish_artifacts", caps["tools"])
        spec = ServiceSpec.load(self.spec_file, self.control)
        self.assertEqual(spec.delivery["auto_actions"], [])
        self.assertEqual(spec.communication["auto_intents"], [])
        self.assertEqual(spec.maintenance["auto_categories"], [])
        effective = spec.with_host_defaults(InteractionConfig.load(self.host_file, self.control))
        self.assertEqual(effective.run_defaults["runtime_limits"]["file_read_chars"], 22000)
        self.assertFalse(effective.run_defaults["runtime_limits"]["auto_compact"])
        self.assertEqual(ServiceSpec(**effective.data()).data(), effective.data())

    def test_preset_preserves_explicit_tool_and_delivery_overrides(self):
        self.isolated_files()
        self.spec["run_defaults"]["tools"] = list(DEFAULT_TOOLS)
        self.spec["delivery"].update(base_update="manual", require_review=False)
        self.host.pop("bash_config")
        report = self.validate()["capabilities"]
        self.assertEqual(report["shell"], "not_exposed")
        self.assertEqual(report["base_update"], "manual")
        self.assertFalse(report["independent_review"])

    def test_preset_rejects_unsafe_or_missing_execution_before_use(self):
        self.isolated_files()
        self.host.pop("bash_config")
        with self.assertRaisesRegex(ValueError, "external preauthorized container Bash policy"):
            self.validate()
        self.host["bash_config"] = str(self.bash)
        self.bash.write_text('{"mode":"host"}')
        with self.assertRaisesRegex(ValueError, "matching container"):
            self.validate()
        self.isolated_files()
        self.save_tests()
        with self.assertRaisesRegex(ValueError, "isolated container TestRunner"):
            self.validate()
        self.spec["delivery"]["required_tests"] = []
        with self.assertRaisesRegex(ValueError, "named Delivery required_tests"):
            self.validate()

    def test_delivery_startup_accepts_the_isolated_preset_and_pins_executor(self):
        from homebrew_harness import Harness, HostConfig
        from homebrew_harness.services.runtime import prepare_binding

        self.isolated_files()
        self.save()
        settings = InteractionConfig.load(self.host_file, self.control)
        spec = ServiceSpec.load(self.spec_file, self.control).with_host_defaults(settings)
        host = HostConfig.from_files(env_file=self.environment, test_config=self.test_file, bash_config=self.bash)
        with Harness(workdir=self.control, config=host) as core, patch(
            "homebrew_harness.services.self_maintenance._check_local_image"
        ) as probe:
            binding = prepare_binding(core, spec)
        probe.assert_called_once()
        self.assertEqual(binding["coding_resources"]["tests"]["backend"], "container")
        self.assertEqual(binding["coding_resources"]["bash"], binding["execution"]["bash"])

    def test_live_local_probe_fails_closed_without_daemon(self):
        with patch("homebrew_harness.execution.execution_backend.ContainerBackend.probe", return_value={"available": False}), patch(
            "homebrew_harness.execution.execution_backend.ContainerBackend._control", side_effect=AssertionError("image queried before daemon"),
        ):
            with self.assertRaisesRegex(ValueError, "reachable Linux container daemon"):
                _check_local_image(shutil.which("git"), "sha256:" + "a" * 64, name="TestRunner")


class RunResourcePinTests(test_human_control.ManagedRunFixture):
    def test_replaced_test_backend_is_rejected_before_worker_issues_grants(self):
        from homebrew_harness.runs.spec import RunSpec
        from homebrew_harness.services.self_maintenance import test_registry_identity

        runner = self.make_tests()
        spec = RunSpec("Run the registered check", allow_tests=("check",))
        control, _, client, _ = self.session([reply("Do not sample")], spec=spec, tests=runner)
        expected = {**test_registry_identity(runner), "backend": "container"}
        control.state = self.store.update(control.header, {"coding_resources": {"bash": None, "tests": expected}})
        result = control.execute()
        self.assertEqual(result.reason_code, "coding_resources_changed")
        self.assertFalse(self.store.get(control.id)["test_grants"])
        client.chat.completions.create.assert_not_called()


class ShellAuthorityTests(unittest.TestCase):
    setUp = test_bash_container.BashContainerTests.setUp
    session = test_bash_container.BashContainerTests.session

    def test_only_the_same_isolated_backend_can_use_preauthorization(self):
        default = self.policy
        self.policy = replace(default, cli=shutil.which("git"), preauthorized=True).bind_cli(self.outer)
        with bash_policy_scope(default):
            self.assertEqual(check_rules("bash", {}, self.outer).action, "ask")
            with self.assertRaises(BashPolicyError), bash_policy_scope(self.policy):
                self.fail("child escalated container approval")
        with bash_policy_scope(self.policy):
            self.assertEqual(check_rules("bash", {}, self.outer).action, "allow")
            self.assertEqual(check_permission("bash", {"command": "sudo true"}, self.outer).action, "deny")
            self.assertFalse(json.loads(run_bash("printf NOT_RUN", self.outer))["executed"])
        with self.assertRaises(BashPolicyError):
            BashPolicy(preauthorized=True)
        session, _, requests = self.session([
            reply(calls=[call("isolated", "printf check")]), reply("verified"),
        ])
        with patch("homebrew_harness.execution.bash_runner.BashContainerBackend", test_bash_container.FakeBackend), patch(
            "homebrew_harness.security.permissions.ask_user", side_effect=AssertionError("unexpected command approval"),
        ):
            self.assertEqual(session.run_user("Run the synthetic isolated command"), "verified")
        self.assertIn("sandbox result", str(requests[-1]))
        self.assertTrue(test_bash_container.FakeBackend.instances)

    def test_replaced_preapproved_executable_is_rejected_before_launch(self):
        launcher = self.root / "container-cli"
        launcher.write_text("initial fixture")
        launcher.chmod(0o700)
        policy = replace(self.policy, cli=str(launcher), preauthorized=True).bind_cli(self.outer)
        launcher.write_text("replacement fixture")
        with self.assertRaisesRegex(BashPolicyError, "pinned container executable changed"):
            policy.bind_cli(self.outer)
        inside = self.outer / "container-cli"
        inside.write_text("workspace fixture")
        inside.chmod(0o700)
        with self.assertRaisesRegex(BashPolicyError, "outside the writable workspace"):
            replace(policy, cli=str(inside)).bind_cli(self.outer)
