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

from homebrew_harness import ServiceSpec
from homebrew_harness.cli import main
from homebrew_harness.runs.spec import RunSpec
from homebrew_harness.services.maintenance_spec import validate_policy
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

    def test_scope_attention_defaults_do_not_duplicate_tools_or_widen_host_defaults(self):
        options = dict(workspace=str(self.workspace), profile="repository_maintenance")
        maintenance = {"scopes": {
            "narrow": {"tools": ["read_file"]},
            "existing": {"tools": ["request_attention", "read_file"]},
            "disabled": {"tools": ["read_file"], "allow_attention": False},
        }}
        spec = ServiceSpec(**options, maintenance=maintenance)
        scopes = spec.maintenance["scopes"]
        self.assertEqual(scopes["narrow"]["tools"], ["read_file", "request_attention"])
        self.assertEqual(scopes["existing"]["tools"], ["request_attention", "read_file"])
        self.assertNotIn("allow_attention", scopes["narrow"])
        self.assertNotIn("allow_attention", scopes["existing"])
        self.assertEqual(scopes["disabled"]["tools"], ["read_file"])
        self.assertIs(scopes["disabled"]["allow_attention"], False)
        self.assertEqual(ServiceSpec(**spec.data()).maintenance, spec.maintenance)

        # Native RunSpec supplies its own control tools. Exercise the policy
        # compiler separately with a host tool catalog that lacks attention.
        from types import SimpleNamespace

        native = RunSpec("Use the configured tools.")
        host = SimpleNamespace(
            tools=["read_file"], write_paths=native.write_paths,
            allow_tests=native.allow_tests, data=native.data,
        )
        limited = validate_policy({"scopes": {"code": {"allow_attention": True}}}, host)
        self.assertEqual(limited["scopes"]["code"]["tools"], ["read_file"])

    def test_scope_attention_rejects_contradictions_and_non_booleans(self):
        for scope, message in [
            ({"tools": ["request_attention"], "allow_attention": False}, "conflicts"),
            ({"allow_attention": 1}, "must be Boolean"),
        ]:
            with self.subTest(scope=scope), self.assertRaisesRegex(ValueError, message) as caught:
                ServiceSpec(
                    str(self.workspace), profile="repository_maintenance",
                    maintenance={"scopes": {"code": scope}},
                )
            self.assertEqual(caught.exception.code, "service_configuration")

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

    def test_webhook_credentials_validate_without_posting(self):
        credentials = self.private / "webhook.env"
        url = "https://webhook.invalid/private/digest"
        token = "notification-fixture-token"
        credentials.write_text(f"WEBHOOK_URL={url}\nWEBHOOK_TOKEN={token}\n")
        credentials.chmod(0o600)
        self.spec["notifications"] = {
            "url_env": "WEBHOOK_URL",
            "token_env": "WEBHOOK_TOKEN",
            "credentials_file": str(credentials),
        }
        with patch(
            "homebrew_harness.platforms.http.JsonTransport.request",
            side_effect=AssertionError("validation attempted a network request"),
        ) as request:
            result = self.validate()
            credentials.write_text(f"WEBHOOK_URL={url}\n")
            with (
                patch.dict(os.environ, {"WEBHOOK_TOKEN": token}),
                self.assertRaisesRegex(ValueError, "Notification host credentials are missing"),
            ):
                self.validate()
        request.assert_not_called()
        self.assertEqual(result["state"], "validated")
        self.assertTrue(any("Webhook reachability" in check for check in result["pending"]))
        for value in (url, token, str(credentials)):
            self.assertNotIn(value, json.dumps(result))
        self.assertFalse(list(self.control.iterdir()))

    def test_webhook_credentials_file_must_be_external_and_owner_only(self):
        credentials = self.private / "webhook.env"
        credentials.write_text("WEBHOOK_URL=https://webhook.invalid/digest\n")
        credentials.chmod(0o600)
        self.spec["notifications"] = {"url_env": "WEBHOOK_URL"}
        for parent in (self.workspace, self.control):
            path = parent / credentials.name
            path.write_bytes(credentials.read_bytes())
            path.chmod(0o600)
            self.spec["notifications"]["credentials_file"] = str(path)
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "outside|external"):
                self.validate()

        alias = self.private / "webhook-alias.env"
        alias.symlink_to(credentials)
        self.spec["notifications"]["credentials_file"] = str(alias)
        with self.assertRaisesRegex(ValueError, "without symlinks"):
            self.validate()

        self.spec["notifications"]["credentials_file"] = str(credentials)
        credentials.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "owner-only"):
            self.validate()

    def test_source_and_publisher_cannot_bind_different_instances(self):
        raw = self.publisher.read_text().replace(self.endpoint, "https://" + "other.invalid")
        self.publisher.write_text(raw)
        with self.assertRaisesRegex(ValueError, "same declared platform instance"):
            self.validate()

    def test_source_can_reuse_publisher_credentials_file_without_model_env_token(self):
        self.environment.write_text(
            "OPENAI_API_KEY=model-fixture\nOPENAI_MODEL=fixture\nOPENAI_BASE_URL="
            + self.model_endpoint
            + "\n"
        )
        self.spec["sources"] = [
            {
                **self.source,
                "credentials_file": str(self.publisher),
                "token_env": "PROJECT_PUBLISH_TOKEN",
            }
        ]
        result = self.validate()
        self.assertEqual(result["state"], "validated")
        for value in ("model-fixture", "publish-fixture", self.endpoint, self.model_endpoint):
            self.assertNotIn(value, json.dumps(result))

    def test_source_credentials_file_boundaries_and_multi_source_instance_matching(self):
        # Source credentials file must be outside control/workspace and owner-only.
        local = self.workspace / "source.env"
        local.write_bytes(self.publisher.read_bytes())
        local.chmod(0o600)
        self.spec["sources"] = [
            {**self.source, "credentials_file": str(local), "token_env": "PROJECT_PUBLISH_TOKEN"}
        ]
        with self.assertRaisesRegex(ValueError, "outside|external"):
            self.validate()

        second_creds = self.private / "read-other.env"
        second_creds.write_text(
            "ALT_ENDPOINT=" + self.endpoint + "\nALT_TOKEN=read-second\n"
        )
        second_creds.chmod(0o600)
        self.spec["sources"] = [
            self.source,
            {
                "name": "project-secondary",
                "provider": "github",
                "instance": "primary",
                "project": "example/project-two",
                "credentials_file": str(second_creds),
                "api_url_env": "ALT_ENDPOINT",
                "token_env": "ALT_TOKEN",
            },
        ]
        self.assertEqual(self.validate()["state"], "validated")

        # Same instance resolving to a different endpoint in another credentials_file is rejected.
        second_creds.write_text(
            "ALT_ENDPOINT=https://other.invalid\nALT_TOKEN=read-second\n"
        )
        with self.assertRaisesRegex(ValueError, "same declared platform instance"):
            self.validate()

        # Two sources sharing one credentials_file with distinct var names pointing to the same endpoint succeed.
        shared_creds = self.private / "shared.env"
        shared_creds.write_text(
            "API_A=" + self.endpoint + "\nAPI_B=" + self.endpoint + "\nTOKEN_SHARED=read-shared\n"
        )
        shared_creds.chmod(0o600)
        self.spec["sources"] = [
            {
                "name": "project-a",
                "provider": "github",
                "instance": "primary",
                "project": "example/project",
                "credentials_file": str(shared_creds),
                "api_url_env": "API_A",
                "token_env": "TOKEN_SHARED",
            },
            {
                "name": "project-b",
                "provider": "github",
                "instance": "primary",
                "project": "example/project-two",
                "credentials_file": str(shared_creds),
                "api_url_env": "API_B",
                "token_env": "TOKEN_SHARED",
            },
        ]
        self.assertEqual(self.validate()["state"], "validated")
        shared_creds.write_text(
            "API_A=" + self.endpoint + "\nAPI_B=https://other.invalid\nTOKEN_SHARED=read-shared\n"
        )
        with self.assertRaisesRegex(ValueError, "same declared platform instance"):
            self.validate()

    def test_startup_admission_rejects_source_and_publisher_instance_mismatch(self):
        from homebrew_harness import Harness, HostConfig
        from homebrew_harness.services.runtime import prepare_binding
        from homebrew_harness.services.spec import ServiceSpec

        raw = self.publisher.read_text().replace(self.endpoint, "https://" + "other.invalid")
        self.publisher.write_text(raw)
        communication = self.spec.pop("communication")

        # Delivery-only mismatch at startup admission (not just offline validate).
        self.spec["delivery"]["required_tests"] = []
        self.save()
        settings = HostConfig.from_file(self.host_file, workdir=self.control)
        spec = ServiceSpec.load(self.spec_file, self.control).with_host_defaults(settings.settings)
        with Harness(workdir=self.control, config=settings) as core:
            with self.assertRaisesRegex(ValueError, "same declared platform instance"):
                prepare_binding(core, spec)

        # Communication-only mismatch at startup admission as well.
        self.spec.pop("delivery")
        self.spec["communication"] = communication
        self.save()
        spec = ServiceSpec.load(self.spec_file, self.control).with_host_defaults(settings.settings)
        with Harness(workdir=self.control, config=settings) as core:
            with self.assertRaisesRegex(ValueError, "same declared platform instance"):
                prepare_binding(core, spec)

    def test_credentials_file_size_limit_matches_across_validate_and_startup(self):
        from homebrew_harness import Harness, HostConfig
        from homebrew_harness.services.runtime import ServiceRuntime, prepare_binding
        from homebrew_harness.services.spec import ServiceSpec
        from homebrew_harness.services.store import ServiceStore

        self.spec.pop("delivery")
        self.spec.pop("communication")
        source_creds = self.private / "sized-source.env"
        self.spec["sources"] = [
            {
                **self.source,
                "credentials_file": str(source_creds),
            }
        ]

        # Lowered limit (4 KiB): ~5 KiB credentials file rejected by both validate and startup.
        source_creds.write_text(
            "PROJECT_API_ENDPOINT=" + self.endpoint + "\nPROJECT_READ_TOKEN=read-sized\n"
            + "# " + ("x" * 5000) + "\n"
        )
        source_creds.chmod(0o600)
        self.host["runtime_limits"] = {"config_file_bytes": 4096}
        with self.assertRaisesRegex(ValueError, "size limit"):
            self.validate()
        settings = HostConfig.from_file(self.host_file, workdir=self.control)
        spec = ServiceSpec.load(self.spec_file, self.control).with_host_defaults(settings.settings)
        with Harness(workdir=self.control, config=settings) as core:
            with self.assertRaisesRegex(ValueError, "size limit"):
                prepare_binding(core, spec)

        # Raised limit (128 KiB): ~70 KiB credentials file accepted by validate, prepare_binding,
        # and detached-style direct ServiceRuntime construction (outside core.operation()).
        source_creds.write_text(
            "PROJECT_API_ENDPOINT=" + self.endpoint + "\nPROJECT_READ_TOKEN=read-sized\n"
            + "# " + ("x" * 70000) + "\n"
        )
        source_creds.chmod(0o600)
        self.host["runtime_limits"] = {"config_file_bytes": 131072}
        self.assertEqual(self.validate()["state"], "validated")
        settings = HostConfig.from_file(self.host_file, workdir=self.control)
        spec = ServiceSpec.load(self.spec_file, self.control).with_host_defaults(settings.settings)
        with Harness(workdir=self.control, config=settings) as core:
            binding = prepare_binding(core, spec)
            store = ServiceStore(self.control)
            meta = store.create(spec, settings.settings.identity(), binding, "sized-service")
            runtime = ServiceRuntime(core, store, meta["id"])
            try:
                self.assertIn("project", runtime.maintenance.sources)
            finally:
                runtime.close()
                runtime.wait_closed(5)

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
