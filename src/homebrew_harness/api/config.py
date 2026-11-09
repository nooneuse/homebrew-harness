"""Host references and explicit process-local extensions for the public SDK."""

from dataclasses import dataclass, field, replace
from pathlib import Path

from homebrew_harness.execution.model_registry import ModelRegistry
from homebrew_harness.runtime.interaction_config import (
    InteractionConfig,
    InteractionAssembler,
)
from .types import UnsupportedOperation


class _RegistryView:
    def __init__(self, registry):
        self.registry = registry

    def __getattr__(self, name):
        return getattr(self.registry, name)

    def close(self):
        pass


class _LocalModels:
    def __init__(self, components, config):
        self.components = components
        self.registry = _RegistryView(components.model_registry or ModelRegistry())
        self.alias = (
            config.lead_model
            if config.lead_model is not None
            else self.registry.lead_default
        )
        self.binding = None
        self.gateways = []

    def open(self, *, context_chars=50000, output_tokens=4096):
        if self.binding is None:
            self.binding = self.registry.select(
                self.alias,
                client=self.components.client,
                model=self.components.model,
                context_chars=context_chars,
                max_output_tokens=output_tokens,
            )
        return self.binding

    def track_gateway(self, gateway):
        self.gateways.append(gateway)

    def close(self):
        pass

    def close_when_idle(self, timeout=0.2, **_kw):
        return all(g.wait_idle(timeout) for g in self.gateways)


@dataclass(frozen=True)
class HostComponents:
    client: object = field(default=None, repr=False, compare=False)
    model: str | None = None
    model_registry: object = field(default=None, repr=False, compare=False)
    workflow_registry: object = field(default=None, repr=False, compare=False)
    hooks: object = field(default=None, repr=False, compare=False)
    environment: dict | None = field(default=None, repr=False, compare=False)
    own_client: bool = False

    def __post_init__(self):
        if type(self.own_client) is not bool:
            raise ValueError("own_client must be boolean.")
        if self.client is not None and self.model_registry is not None:
            raise ValueError("Choose a borrowed client/model pair or a model registry.")
        if self.environment is not None and (
            not isinstance(self.environment, dict)
            or any(
                not isinstance(k, str) or not isinstance(v, str)
                for k, v in self.environment.items()
            )
        ):
            raise ValueError("Environment must be a mapping of string keys and values.")

    @property
    def local_models(self):
        return self.client is not None or self.model_registry is not None


@dataclass(frozen=True)
class HostConfig:
    settings: InteractionConfig = field(default_factory=InteractionConfig)
    components: HostComponents = field(
        default_factory=HostComponents, repr=False, compare=False
    )

    def __post_init__(self):
        if not isinstance(self.settings, InteractionConfig) or not isinstance(
            self.components, HostComponents
        ):
            raise ValueError("HostConfig requires settings and components.")

    @classmethod
    def from_files(cls, **settings):
        for name in (
            "env_file",
            "models_config",
            "test_config",
            "bash_config",
            "mcp_config",
            "control_config",
        ):
            if name in settings and settings[name] is not None:
                settings[name] = Path(settings[name])
        if "allowed" not in settings:
            from homebrew_harness.runtime.capabilities import OPTIONAL

            settings["allowed"] = tuple(
                sorted(OPTIONAL - ({"mcp"} if "mcp_config" not in settings else set()))
            )
        return cls(InteractionConfig(**settings))

    @classmethod
    def from_file(cls, path, *, workdir):
        return cls(InteractionConfig.load(path, workdir))

    @classmethod
    def from_components(
        cls,
        *,
        client=None,
        model=None,
        model_registry=None,
        workflow_registry=None,
        hooks=None,
        environment=None,
        own_client=False,
        **settings,
    ):
        if client is not None and not model or model is not None and client is None:
            raise ValueError(
                "Process-local models require a client/model pair or a model registry."
            )
        base = cls.from_files(**settings)
        return cls(
            base.settings,
            HostComponents(
                client,
                model,
                model_registry,
                workflow_registry,
                hooks,
                environment,
                own_client,
            ),
        )

    def identity(self):
        return {
            "settings": self.settings.identity(),
            "process_local": self.components.local_models
            or self.components.workflow_registry is not None
            or self.components.hooks is not None,
        }

    def builder(self, workdir, *, settings=None):
        config = settings or self.settings
        components = self.components
        provider = (
            (lambda: _LocalModels(components, config))
            if components.local_models
            else None
        )
        if components.local_models:
            import os

            environment = dict(
                os.environ if components.environment is None else components.environment
            )
        else:
            environment = components.environment
        return InteractionAssembler(
            workdir,
            config,
            workflow_registry=components.workflow_registry,
            model_provider=provider,
            environment=environment,
        )

    def require_reconstructable(self):
        if (
            self.components.local_models
            or self.components.workflow_registry is not None
            or self.components.hooks is not None
            or self.components.environment is not None
        ):
            raise UnsupportedOperation(
                "Worker and detached execution require reconstructable host configuration. Use external model/tool references; process-local clients and callables belong to Session or direct Workflow."
            )


@dataclass(frozen=True)
class SessionOptions:
    enabled: tuple = ()
    model_profile: str | None = None
    mcp: dict = field(default_factory=dict)
    mcp_delegation: str | None = None
    memory_mode: str | None = None
    max_turns: int | None = None
    context_chars: int | None = None
    output_tokens: int | None = None
    turn_seconds: float | None = None

    def __post_init__(self):
        from homebrew_harness.runtime.capabilities import OPTIONAL
        from homebrew_harness.tools.mcp.selection import validate_selection

        if (
            not isinstance(self.enabled, (tuple, list))
            or any(not isinstance(v, str) for v in self.enabled)
            or len(set(self.enabled)) != len(self.enabled)
            or set(self.enabled) - OPTIONAL
        ):
            raise ValueError(
                "Session enabled capabilities must be unique catalog names."
            )
        object.__setattr__(self, "enabled", tuple(self.enabled))
        object.__setattr__(self, "mcp", validate_selection(self.mcp))
        if self.model_profile is not None:
            from homebrew_harness.execution.model_registry import _alias

            _alias(self.model_profile)
        self.settings(InteractionConfig())

    def settings(self, config):
        values = {
            "lead_model": self.model_profile,
            "mcp_delegation": self.mcp_delegation,
            "memory_mode": self.memory_mode,
            "max_turns": self.max_turns,
            "context_chars": self.context_chars,
            "output_tokens": self.output_tokens,
            "turn_seconds": self.turn_seconds,
        }
        return replace(
            config, **{key: value for key, value in values.items() if value is not None}
        )
