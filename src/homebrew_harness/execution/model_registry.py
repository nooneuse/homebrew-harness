"""Host model aliases; connections stay private and selections bind at creation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit

from homebrew_harness.configuration import register_model_environment

from homebrew_harness.execution.execution_control import acquired, check
from homebrew_harness.execution.model_profile import profile_fingerprint
from homebrew_harness.execution.model_protocol import ModelCapabilities
from homebrew_harness.execution.providers.common import PROTOCOLS, frozen_options
from homebrew_harness.runtime.runtime_control import ClosePending
from homebrew_harness.security.data_boundary import register_private_values

_ALIAS = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}\Z")


def _alias(value):
    if not isinstance(value, str) or not _ALIAS.fullmatch(value):
        raise ValueError("Model aliases must be short identifiers.")
    return value


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class ModelSpec:
    name: str
    model: str = field(repr=False)
    api_key: str = field(repr=False)
    base_url: str = field(repr=False)
    description: str = ""
    context_chars: int | None = None
    max_output_tokens: int | None = None
    protocol: str = "chat_completions"
    connection_id: str | None = None
    options_json: str = "{}"
    api_version: str = "2023-06-01"
    capabilities: ModelCapabilities = field(default_factory=ModelCapabilities)
    transport: str = "auto"

    def __post_init__(self):
        _alias(self.name)
        if self.transport not in {"auto", "http", "sdk"}:
            raise ValueError("Model transport must be auto, http or sdk.")
        if self.protocol not in PROTOCOLS:
            raise ValueError("Unsupported model protocol.")
        object.__setattr__(
            self, "options_json", frozen_options(json.loads(self.options_json))
        )
        if self.connection_id is not None:
            _alias(self.connection_id)
        register_private_values(self.api_key, self.base_url)
        register_model_environment(values=(self.api_key, self.base_url))
        if any(
            not isinstance(v, str) or not v.strip() or "\0" in v
            for v in (self.model, self.api_key, self.base_url)
        ):
            raise ValueError(
                "Model configuration requires nonempty external connection values."
            )
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
        ):
            raise ValueError(
                "Model endpoint must be an HTTP(S) address without embedded credentials."
            )
        if not isinstance(self.description, str) or len(self.description) > 500:
            raise ValueError("Model description must contain at most 500 characters.")
        if self.context_chars is not None and (
            type(self.context_chars) is not int
            or not 10000 <= self.context_chars <= 2000000
        ):
            raise ValueError("Model context_chars must be between 10000 and 2000000.")
        if self.max_output_tokens is not None and (
            type(self.max_output_tokens) is not int
            or not 128 <= self.max_output_tokens <= 32768
        ):
            raise ValueError("Model max_output_tokens must be between 128 and 32768.")

    def identity(self):
        return _hash(
            {
                "name": self.name,
                "model": self.model,
                "endpoint": _hash(self.base_url),
                "context_chars": self.context_chars,
                "max_output_tokens": self.max_output_tokens,
                "protocol": self.protocol,
                "transport": self.transport,
                "options": self.options_json,
                "api_version": self.api_version,
                "capabilities": self.capabilities.__dict__,
            }
        )


@dataclass(frozen=True)
class ModelBinding:
    client: object = field(repr=False, compare=False)
    model: str = field(repr=False)
    profile: str | None
    fingerprint: str
    context_chars: int
    max_output_tokens: int
    transport: dict | None = None

    def identity(self):
        return {
            "profile": self.profile,
            "fingerprint": self.fingerprint,
            **({"transport": dict(self.transport)} if self.transport else {}),
        }


class ModelRegistry:
    def __init__(
        self, specs=(), *, lead_default=None, teammate_roles=None, client_factory=None
    ):
        values = list(specs)
        if (
            len(values) > 16
            or any(not isinstance(s, ModelSpec) for s in values)
            or len({s.name for s in values}) != len(values)
        ):
            raise ValueError(
                "Model registry requires at most 16 uniquely named ModelSpec entries."
            )
        connections = {}
        for spec in values:
            key = spec.connection_id or spec.name
            identity = (
                spec.protocol,
                spec.base_url,
                spec.api_key,
                spec.api_version,
                spec.transport,
            )
            if key in connections and connections[key] != identity:
                raise ValueError(
                    "One connection cannot have conflicting credentials or protocol settings."
                )
            connections[key] = identity
        self._specs = MappingProxyType({s.name: s for s in values})
        roles = dict(teammate_roles or {})
        for role, alias in roles.items():
            _alias(role)
            self.validate(alias)
        if lead_default is not None:
            self.validate(lead_default)
        self._lead_default, self._teammate_roles = lead_default, MappingProxyType(roles)
        from homebrew_harness.execution.providers.backends import backend_pins

        self.backend_pins = backend_pins()
        self.client_factory = client_factory
        self._clients = {}
        self._profile_clients = {}
        self._closed_clients = set()
        self._lock = threading.RLock()
        self._closed = False
        self._owner = None

    @property
    def specs(self):
        return self._specs

    @property
    def lead_default(self):
        return self._lead_default

    @property
    def teammate_roles(self):
        return self._teammate_roles

    def pin_backends(self, *bindings):
        from homebrew_harness.execution.providers.backends import (
            backend_scope,
            backend_pins,
        )

        with acquired(self._lock):
            with backend_scope(*bindings):
                expected = backend_pins()
            for key, value in expected.items():
                client = self._clients.get(key)
                if (
                    client is not None
                    and getattr(client, "transport_identity", None) is not None
                    and getattr(client, "transport_identity", None)
                    != {
                        k: value.get(k)
                        for k in ("backend", "revision", "sdk", "sdk_version")
                    }
                ):
                    raise ValueError(
                        "Restore requires the saved model backend; close the changed client first."
                    )
                if key in self.backend_pins and self.backend_pins[key] != value:
                    raise ValueError(
                        "Model connection already has a different saved binding."
                    )
            self.backend_pins.update(expected)

    def claim(self, owner):
        with acquired(self._lock):
            if self._closed or self._owner is not None and self._owner is not owner:
                raise ValueError("Model registry requires one owning session.")
            self._owner = owner

    def validate(self, alias):
        _alias(alias)
        if alias not in self.specs:
            raise ValueError("Unknown model profile; choose a host-registered alias.")

    def catalog(self):
        return [
            {"name": s.name, "description": s.description} for s in self.specs.values()
        ]

    def manifest(self):
        return {
            "profiles": {name: s.identity() for name, s in self.specs.items()},
            "lead_default": self.lead_default,
            "teammate_roles": dict(self.teammate_roles),
        }

    def select(
        self,
        alias=None,
        *,
        client=None,
        model=None,
        context_chars=50000,
        max_output_tokens=4096,
        role=None,
    ):
        chosen = alias if alias is not None else self.teammate_roles.get(role)
        with acquired(self._lock):
            check()
            if self._closed:
                raise ValueError("Model registry is closed.")
            if chosen is not None:
                self.validate(chosen)
                spec = self.specs[chosen]
                key = spec.connection_id or spec.name
                if key not in self._clients:
                    from homebrew_harness.execution.providers.chat_completions import (
                        ChatCompletionsAdapter,
                    )

                    if (
                        spec.protocol == "chat_completions"
                        and self.client_factory is not None
                    ):
                        raw = self.client_factory(
                            api_key=spec.api_key,
                            base_url=spec.base_url,
                            timeout=60.0,
                            max_retries=0,
                        )
                        base = ChatCompletionsAdapter(raw, connection_id=key)
                    else:
                        from homebrew_harness.execution.providers.adapter import (
                            ProtocolAdapter,
                        )

                        base = ProtocolAdapter(
                            spec.protocol,
                            api_key=spec.api_key,
                            base_url=spec.base_url,
                            api_version=spec.api_version,
                            connection_id=key,
                            transport=spec.transport,
                            expected_transport=self.backend_pins.get(key),
                        )
                    self._clients[key] = base
                if chosen not in self._profile_clients:
                    base = self._clients[key]
                    if base.protocol != spec.protocol or (
                        base.base_url is not None
                        and str(base.base_url).rstrip("/") != spec.base_url.rstrip("/")
                    ):
                        raise ValueError(
                            "One model connection cannot have conflicting protocol or endpoint definitions."
                        )
                    import copy

                    variant = copy.copy(base)
                    variant.options_json = spec.options_json
                    variant.capabilities = spec.capabilities
                    self._profile_clients[chosen] = variant
                client, model = self._profile_clients[chosen], spec.model
                context_chars = spec.context_chars or context_chars
                max_output_tokens = spec.max_output_tokens or max_output_tokens
            if client is None or not isinstance(model, str) or not model.strip():
                raise ValueError(
                    "An inherited model or a registered model profile is required."
                )
            from homebrew_harness.execution.providers import adapter_for
            from homebrew_harness.execution.model_gateway import ModelGateway

            adapter = (
                client._adapter
                if isinstance(client, ModelGateway)
                else adapter_for(client)
            )
            fingerprint = profile_fingerprint(
                client,
                model,
                profile=chosen,
                settings={
                    "context_chars": context_chars,
                    "max_output_tokens": max_output_tokens,
                    "protocol": adapter.protocol,
                    "adapter_revision": adapter.revision,
                    "options": adapter.options_json,
                    "transport": getattr(adapter, "transport_identity", None),
                    **(
                        {"model_spec": self.specs[chosen].identity()}
                        if chosen is not None
                        else {}
                    ),
                },
            )
            return ModelBinding(
                client,
                model,
                chosen,
                fingerprint,
                context_chars,
                max_output_tokens,
                {
                    **adapter.transport_identity,
                    "connection_id": adapter.connection_id or "@inherited",
                }
                if getattr(adapter, "transport_identity", None)
                else None,
            )

    def own_client(self, client):
        with acquired(self._lock):
            if self._closed or "@inherited" in self._clients:
                raise ValueError("Inherited model client already has an owner.")
            self._clients["@inherited"] = client

    def close(self):
        # Session invokes this only after shared gateway and member execution end.
        with acquired(self._lock, inherit=False):
            self._closed = True
            failures = []
            for name, client in self._clients.items():
                if name in self._closed_clients:
                    continue
                try:
                    client.close()
                    self._closed_clients.add(name)
                except Exception:
                    failures.append(name)
            if failures:
                raise ClosePending("Model client cleanup is pending; retry host close.")


def load_model_registry(path, workdir, *, client_factory=None, environment=None):
    """Current two-level configuration; no prerelease-format migration."""
    environment = os.environ if environment is None else environment
    path = Path(path).expanduser().resolve()
    if path.is_relative_to(Path(workdir).resolve()):
        raise ValueError("Model configuration must be outside the workspace.")
    try:
        with path.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("Model configuration exceeds 64 KiB.")

        def unique(pairs):
            result = {}
            for name, value in pairs:
                if name in result:
                    raise ValueError("Duplicate configuration field.")
                result[name] = value
            return result

        data = json.loads(raw, object_pairs_hook=unique)
        if (
            not isinstance(data, dict)
            or set(data) - {"version", "connections", "profiles", "defaults"}
            or type(data.get("version")) is not int
            or data["version"] != 2
        ):
            raise ValueError("Model configuration requires current version 2.")
        connections, profiles, defaults = (
            data.get("connections"),
            data.get("profiles"),
            data.get("defaults", {}),
        )
        if (
            not isinstance(connections, dict)
            or not isinstance(profiles, dict)
            or not isinstance(defaults, dict)
            or set(defaults) - {"lead", "teammate_roles"}
        ):
            raise ValueError("Invalid model connections, profiles or defaults.")

        def external(name):
            if not isinstance(name, str) or not _ENV.fullmatch(name):
                raise ValueError(
                    "Model configuration requires an environment variable name."
                )
            value = str(environment.get(name) or "").strip()
            if not value:
                raise ValueError("A registered model environment variable is missing.")
            register_model_environment(names=(name,))
            return value

        prepared = {}
        for name, value in connections.items():
            _alias(name)
            if not isinstance(value, dict) or set(value) - {
                "protocol",
                "api_key_env",
                "base_url_env",
                "api_version",
                "options",
                "transport",
            }:
                raise ValueError("Invalid model connection fields.")
            protocol = value.get("protocol")
            if protocol not in PROTOCOLS:
                raise ValueError("Invalid model connection protocol.")
            prepared[name] = (
                protocol,
                external(value.get("api_key_env")),
                external(value.get("base_url_env")),
                value.get("api_version", "2023-06-01"),
                value.get("options", {}),
                value.get("transport", "auto"),
            )
        specs = []
        for name, value in profiles.items():
            if not isinstance(value, dict) or set(value) - {
                "connection",
                "model_env",
                "description",
                "context_chars",
                "max_output_tokens",
                "options",
                "capabilities",
            }:
                raise ValueError("Invalid model profile fields.")
            if value.get("connection") not in prepared:
                raise ValueError("Unknown model connection reference.")
            protocol, key, endpoint, version, options, transport = prepared[
                value["connection"]
            ]
            options = {**options, **value.get("options", {})}
            caps = value.get("capabilities", {})
            if (
                not isinstance(caps, dict)
                or set(caps) - {"tools", "json_schema", "thinking"}
                or any(type(v) is not bool for v in caps.values())
            ):
                raise ValueError("Invalid model capabilities.")
            specs.append(
                ModelSpec(
                    name,
                    external(value.get("model_env")),
                    key,
                    endpoint,
                    value.get("description", ""),
                    value.get("context_chars"),
                    value.get("max_output_tokens"),
                    protocol,
                    value["connection"],
                    frozen_options(options),
                    version,
                    ModelCapabilities(**caps),
                    transport,
                )
            )
        roles = defaults.get("teammate_roles", {})
        if not isinstance(roles, dict):
            raise ValueError("Invalid teammate role defaults.")
        return ModelRegistry(
            specs,
            lead_default=defaults.get("lead"),
            teammate_roles=roles,
            client_factory=client_factory,
        )
    except (OSError, ValueError, TypeError, UnicodeError) as error:
        raise ValueError(
            "Cannot load external model registry (" + type(error).__name__ + ")."
        ) from error
