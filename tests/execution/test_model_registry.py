"""External model aliases, private identities and explicit client ownership."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlunsplit

from homebrew_harness.execution.model_registry import (
    ModelRegistry,
    ModelSpec,
    load_model_registry,
)
from homebrew_harness.runtime.runtime_control import ClosePending
from homebrew_harness.security.data_boundary import redact


def spec(name="coder", **settings):
    return ModelSpec(
        name,
        "fixture-model",
        "synthetic-credential",
        urlunsplit(("http", "127.0.0.1:9", "/v1", "", "")),
        **settings,
    )


class ModelRegistryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.path = self.root / "models.json"
        self.env = {
            "TEST_MODEL": "fixture-model",
            "TEST_KEY": "synthetic-credential",
            "TEST_ENDPOINT": spec().base_url,
        }
        self.config = {
            "version": 2,
            "connections": {"provider": {"protocol": "chat_completions", "api_key_env": "TEST_KEY", "base_url_env": "TEST_ENDPOINT"}},
            "profiles": {
                "coder": {
                    "connection": "provider",
                    "model_env": "TEST_MODEL",
                    "description": "Focused implementation",
                }
            },
            "defaults": {"teammate_roles": {"coder": "coder"}},
        }

    def load(self, data=None):
        self.path.write_text(json.dumps(self.config if data is None else data))
        with patch.dict(os.environ, self.env):
            return load_model_registry(self.path, self.workspace)

    def test_external_configuration_is_lazy_and_does_not_expose_connections(self):
        registry = self.load()
        factory = Mock(return_value=SimpleNamespace(close=Mock()))
        registry.client_factory = factory
        self.assertEqual(registry.catalog()[0]["name"], "coder")
        factory.assert_not_called()
        a = registry.select("coder")
        b = registry.select("coder")
        self.assertIs(a.client, b.client)
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(factory.call_args.kwargs["max_retries"], 0)
        public = json.dumps(
            [registry.catalog(), registry.manifest(), a.identity()]
        ) + repr(spec())
        self.assertNotIn(self.env["TEST_KEY"], public)
        self.assertNotIn(self.env["TEST_ENDPOINT"], public)
        self.assertEqual(redact(self.env["TEST_KEY"]), "[redacted]")
        registry.close()
        a.client.client.close.assert_called_once()

    def test_unknown_alias_never_creates_a_client(self):
        factory = Mock()
        registry = ModelRegistry([spec()], client_factory=factory)
        with self.assertRaises(ValueError):
            registry.select("invented")
        factory.assert_not_called()

    def test_defaults_inherit_and_exact_role_mapping_can_be_overridden(self):
        parent = SimpleNamespace(_harness_model_identity="parent")
        factory = Mock(return_value=SimpleNamespace(close=Mock()))
        registry = ModelRegistry(
            [spec()], teammate_roles={"coder": "coder"}, client_factory=factory
        )
        inherited = registry.select(
            client=parent,
            model="parent-model",
            role="coding agent",
            context_chars=40000,
            max_output_tokens=8192,
        )
        self.assertIs(inherited.client, parent)
        self.assertIsNone(inherited.profile)
        self.assertEqual(
            registry.select(client=parent, model="parent-model", role="coder").profile,
            "coder",
        )
        self.assertEqual(inherited.max_output_tokens, 8192)
        registry.close()

    def test_invalid_or_workspace_configuration_is_rejected_without_echoing_values(
        self,
    ):
        for mutate in [
            lambda d: d.update(extra=True),
            lambda d: d["defaults"].update(lead="missing"),
            lambda d: d["profiles"]["coder"].update(api_key="private-value"),
            lambda d: d["profiles"]["coder"].update(context_chars=True),
        ]:
            data = json.loads(json.dumps(self.config))
            mutate(data)
            with self.assertRaises(ValueError) as error:
                self.load(data)
            self.assertNotIn("private-value", str(error.exception))
        inner = self.workspace / "models.json"
        inner.write_text(json.dumps(self.config))
        with self.assertRaises(ValueError):
            load_model_registry(inner, self.workspace)
        self.path.write_text('{"version":1,"version":1,"profiles":{}}')
        with self.assertRaises(ValueError):
            load_model_registry(self.path, self.workspace)

    def test_close_retries_only_unclosed_owned_clients(self):
        a, b = (
            SimpleNamespace(close=Mock()),
            SimpleNamespace(close=Mock(side_effect=[OSError("fixture"), None])),
        )
        registry = ModelRegistry(
            [spec("a"), spec("b")], client_factory=Mock(side_effect=[a, b])
        )
        registry.select("a")
        registry.select("b")
        with self.assertRaises(ClosePending):
            registry.close()
        with self.assertRaises(ValueError):
            registry.select("a")
        registry.close()
        self.assertEqual(a.close.call_count, 1)
        self.assertEqual(b.close.call_count, 2)

    def test_registry_rejects_a_second_session_owner(self):
        registry = ModelRegistry()
        owner = object()
        registry.claim(owner)
        registry.claim(owner)
        with self.assertRaises(ValueError):
            registry.claim(object())

    def test_profile_changes_affect_identity_but_key_rotation_does_not(self):
        from dataclasses import replace

        a = spec()
        self.assertEqual(
            a.identity(), replace(a, api_key="rotated-credential").identity()
        )
        self.assertNotEqual(a.identity(), replace(a, model="another-model").identity())
        self.assertNotEqual(a.identity(), replace(a, max_output_tokens=2048).identity())

    def test_registered_model_environment_and_duplicate_credentials_are_not_passed_to_bash(
        self,
    ):
        from homebrew_harness.tools.shell import run_bash

        registry = self.load()
        env = {
            **self.env,
            "DUPLICATE_CREDENTIAL": self.env["TEST_KEY"],
            "DUPLICATE_ENDPOINT": self.env["TEST_ENDPOINT"],
            "PUBLIC_MODEL_CANARY": "public-value",
        }
        command = 'test -z "$TEST_KEY$TEST_ENDPOINT$TEST_MODEL$DUPLICATE_CREDENTIAL$DUPLICATE_ENDPOINT" && printf "%s" "$PUBLIC_MODEL_CANARY"'
        with patch.dict(os.environ, env):
            result = json.loads(run_bash(command, self.workspace))
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["stdout"], "public-value")
        registry.close()

    def test_programmatic_model_credentials_are_filtered_even_without_env_references(
        self,
    ):
        from homebrew_harness.configuration import public_environment

        value = spec()
        env = {
            "ALTERNATE_KEY_NAME": value.api_key,
            "ALTERNATE_ENDPOINT_NAME": value.base_url,
            "PUBLIC_MODEL_CANARY": "public-value",
        }
        self.assertEqual(
            public_environment(env), {"PUBLIC_MODEL_CANARY": "public-value"}
        )
