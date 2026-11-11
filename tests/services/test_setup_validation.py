"""Offline hosting checks reject unusable declarations before any owner starts."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from homebrew_harness.cli import main
from homebrew_harness.services.setup_validation import validate_service


class SetupValidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.workspace = self.root / "project"
        self.control = self.root / "control"
        self.private = self.root / "private"
        for directory in (self.workspace, self.control, self.private):
            directory.mkdir(mode=0o700)
        self.endpoint = "https://" + "platform.invalid"
        self.model_endpoint = "https://" + "model.invalid/v1"
        self.environment = self.private / "model.env"
        self.publisher = self.private / "publisher.env"
        self.environment.write_text(
            "OPENAI_API_KEY=model-fixture\nOPENAI_MODEL=fixture\nOPENAI_BASE_URL=" + self.model_endpoint
            + "\nPROJECT_API_ENDPOINT=" + self.endpoint + "\nPROJECT_READ_TOKEN=read-fixture\n"
        )
        self.publisher.write_text(
            "PROJECT_API_ENDPOINT=" + self.endpoint + "\nPROJECT_PUBLISH_TOKEN=publish-fixture\n"
            + "PROJECT_GIT_REMOTE=" + str(self.root / "remote.git") + "\n"
            + "PROJECT_AUTHOR_NAME=fixture\nPROJECT_AUTHOR_EMAIL=fixture@invalid\n"
        )
        self.environment.chmod(0o600)
        self.publisher.chmod(0o600)
        self.test_file = self.private / "tests.json"
        self.recipe = {"name": "unit-tests", "builtin": "unittest", "roles": ["lead"], "authorization": "confirm"}
        self.save_tests()
        self.host_file = self.private / "host.json"
        self.host = {"env_file": str(self.environment), "test_config": str(self.test_file)}
        self.source = {
            "name": "project", "provider": "github", "instance": "primary", "project": "example/project",
            "api_url_env": "PROJECT_API_ENDPOINT", "token_env": "PROJECT_READ_TOKEN",
        }
        self.spec_file = self.private / "service.json"
        self.spec = {
            "version": 1, "workspace": str(self.workspace), "profile": "repository_maintenance",
            "sources": [self.source],
            "run_defaults": {"write_paths": ["src"], "allow_tests": ["unit-tests"]},
            "maintenance": {"objectives": "Keep documented compatibility.", "non_goals": "Do not add platforms.", "scopes": {"code": {"write_paths": ["src"]}}},
            "delivery": {
                "git_executable": shutil.which("git"), "provider": "github", "instance": "primary", "project": "example/project",
                "remote_env": "PROJECT_GIT_REMOTE", "api_url_env": "PROJECT_API_ENDPOINT", "api_token_env": "PROJECT_PUBLISH_TOKEN",
                "author_name_env": "PROJECT_AUTHOR_NAME", "author_email_env": "PROJECT_AUTHOR_EMAIL",
                "credentials_file": str(self.publisher), "required_tests": ["unit-tests"],
            },
            "communication": {"credentials_file": str(self.publisher), "projects": [{**self.source, "token_env": "PROJECT_PUBLISH_TOKEN"}]},
            "schedules": [{"name": "code-review", "every_seconds": 604800, "action": "review_repository"}],
        }
        self.save()
        clean = patch.dict(os.environ, {}, clear=True)
        clean.start()
        self.addCleanup(clean.stop)

    def save_tests(self):
        self.test_file.write_text(json.dumps({"version": 1, "recipes": [self.recipe]}))

    def save(self):
        self.host_file.write_text(json.dumps(self.host))
        self.spec_file.write_text(json.dumps(self.spec))

    def validate(self):
        self.save()
        return validate_service(self.spec_file, self.host_file, self.control)

    def test_complete_spec_validates_without_effects_or_secret_output(self):
        before = sorted(p.relative_to(self.root) for p in self.root.rglob("*"))
        with (
            patch("homebrew_harness.execution.model_connections.HostModels.open", side_effect=AssertionError("model opened")),
            patch("homebrew_harness.platforms.http.JsonTransport.request", side_effect=AssertionError("platform called")),
            patch("homebrew_harness.execution.test_runner.TestRunner.run", side_effect=AssertionError("test executed")),
            patch("homebrew_harness.services.runtime.ServiceRuntime.start", side_effect=AssertionError("owner started")),
        ):
            result = self.validate()
        self.assertEqual(result["state"], "validated")
        self.assertEqual(result["merge_mode"], "manual")
        self.assertEqual(result["fixed_tests"], ["unit-tests"])
        self.assertTrue(result["pending"])
        self.assertEqual(before, sorted(p.relative_to(self.root) for p in self.root.rglob("*")))
        for value in ("model-fixture", "publish-fixture", "read-fixture", self.endpoint, self.model_endpoint):
            self.assertNotIn(value, json.dumps(result))

    def test_cli_uses_offline_validation_and_creates_no_control_state(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["service", "--cwd", str(self.control), "validate", "--spec", str(self.spec_file), "--host-config", str(self.host_file)]), 0)
        self.assertEqual(json.loads(output.getvalue())["state"], "validated")
        self.assertFalse(list(self.control.iterdir()))

    def test_unknown_or_denied_test_is_rejected(self):
        self.recipe["authorization"] = "deny"
        self.save_tests()
        with self.assertRaisesRegex(ValueError, "not registered"):
            self.validate()

    def test_test_roles_and_candidate_workspaces_must_match(self):
        for fields, reason in [({"roles": ["subagent"]}, "lead role"), ({"allow_worktrees": False}, "isolated candidate")]:
            with self.subTest(fields=fields):
                self.recipe = {"name": "unit-tests", "builtin": "unittest", "roles": ["lead"], **fields}
                self.save_tests()
                with self.assertRaisesRegex(ValueError, reason):
                    self.validate()

    def test_unknown_service_review_model_is_rejected(self):
        self.spec["communication"]["review_model"] = "unregistered"
        with self.assertRaisesRegex(ValueError, "Unknown model profile"):
            self.validate()

    def test_registered_role_models_validate_without_opening_providers(self):
        catalog = self.private / "models.json"
        catalog.write_text(json.dumps({
            "version": 2,
            "connections": {"provider": {"protocol": "chat_completions", "api_key_env": "OPENAI_API_KEY", "base_url_env": "OPENAI_BASE_URL", "transport": "http"}},
            "profiles": {
                name: {"connection": "provider", "model_env": "OPENAI_MODEL", "description": name}
                for name in ("planner", "coder")
            },
            "defaults": {"lead": "planner"},
        }))
        self.host["models_config"] = str(catalog)
        self.spec["run_defaults"]["model_profile"] = "coder"
        self.spec["maintenance"]["model_profile"] = "planner"
        self.spec["communication"]["review_model"] = "planner"
        self.spec["delivery"]["review_model"] = "planner"
        self.spec["maintenance"]["instructions"] = {"review": "Use Summary and Validation PR headings.", "development": "Keep project typing conventions."}
        with patch("homebrew_harness.execution.model_connections.HostModels.open", side_effect=AssertionError("provider opened")):
            self.assertEqual(self.validate()["state"], "validated")

    def test_write_credentials_in_development_environment_are_rejected(self):
        os.environ["EXPORTED_CREDENTIAL"] = "publish-fixture"
        with self.assertRaisesRegex(ValueError, "development environment"):
            self.validate()

    def test_publisher_file_must_be_external_and_private(self):
        for path in (self.workspace / "publish.env", self.control / "publish.env"):
            path.write_bytes(self.publisher.read_bytes())
            path.chmod(0o600)
            self.spec["delivery"]["credentials_file"] = str(path)
            with self.assertRaisesRegex(ValueError, "outside|external"):
                self.validate()
        self.spec["delivery"]["credentials_file"] = str(self.publisher)
        self.publisher.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "owner-only"):
            self.validate()

    def test_source_and_publisher_cannot_bind_different_instances(self):
        raw = self.publisher.read_text().replace(self.endpoint, "https://" + "other.invalid")
        self.publisher.write_text(raw)
        with self.assertRaisesRegex(ValueError, "same declared platform instance"):
            self.validate()

    def test_remote_pr_execution_requires_container(self):
        self.spec["communication"]["required_tests"] = ["unit-tests"]
        with self.assertRaisesRegex(ValueError, "container backend"):
            self.validate()

    def test_test_requirement_cannot_silently_mean_no_tests(self):
        self.spec["communication"]["require_tests_for_approval"] = True
        with self.assertRaisesRegex(ValueError, "named PR tests"):
            self.validate()

    def test_host_file_and_references_cannot_live_in_either_workspace(self):
        local = self.workspace / "host.json"
        local.write_bytes(self.host_file.read_bytes())
        with self.assertRaisesRegex(ValueError, "outside control and project"):
            validate_service(self.spec_file, local, self.control)
        self.host["test_config"] = str(self.control / "tests.json")
        with self.assertRaisesRegex(ValueError, "outside control and project"):
            self.validate()

    def test_symlinked_private_reference_is_rejected(self):
        alias = self.private / "alias.env"
        alias.symlink_to(self.environment)
        self.host["env_file"] = str(alias)
        with self.assertRaisesRegex(ValueError, "without symlinks"):
            self.validate()

    def test_spec_symlink_is_rejected_before_loading(self):
        alias = self.private / "service-alias.json"
        alias.symlink_to(self.spec_file)
        with patch("homebrew_harness.services.setup_validation.ServiceSpec.load", side_effect=AssertionError("symlink read")):
            with self.assertRaisesRegex(ValueError, "without symlinks"):
                validate_service(alias, self.host_file, self.control)

    def test_control_permissions_and_runtime_location_are_checked(self):
        self.control.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "owner-only"):
            self.validate()

    def test_validation_cannot_use_remote_control_connection(self):
        output = io.StringIO()
        with redirect_stderr(output), redirect_stdout(output):
            result = main(["service", "--connection", str(self.private / "client.json"), "validate", "--spec", str(self.spec_file), "--host-config", str(self.host_file)])
        self.assertEqual(result, 1)
        self.assertIn("local service host", output.getvalue())


if __name__ == "__main__":
    unittest.main()
