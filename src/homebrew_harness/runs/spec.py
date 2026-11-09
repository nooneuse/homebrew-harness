"""Host-selected managed execution settings and structured results."""

import copy
import math
import re
from pathlib import PurePosixPath
from dataclasses import asdict, dataclass, field

from homebrew_harness.runtime.capabilities import CORE_TOOLS
from homebrew_harness.orchestration.hard_conditions import validate_spec
from homebrew_harness.security.data_boundary import redact
from homebrew_harness.execution.model_gateway import ModelGatewayError

TERMINAL = {"completed", "failed", "cancelled"}
RUN_CONTROL_TOOLS = frozenset(
    {"request_attention", "execute_approved", "read_run_event"}
)
RUN_STAGE_TOOLS = frozenset({"get_handoff", "integrate_stage", "publish_stage"})
RUN_TOOLS = RUN_CONTROL_TOOLS | RUN_STAGE_TOOLS
DEFAULT_TOOLS = tuple(
    sorted(
        (CORE_TOOLS - {"bash", "list_artifacts", "get_artifact", "publish_artifacts"})
        | RUN_CONTROL_TOOLS
    )
)


def stage_run_tools(stages):
    result = set()
    if stages:
        result.add("get_handoff")
    if any(stage.get("integrates") for stage in stages):
        result.add("integrate_stage")
    if any(stage.get("publish") for stage in stages):
        result.add("publish_stage")
    return frozenset(result)


@dataclass(frozen=True)
class RunSpec:
    prompt: str
    min_free_bytes: int = 134217728
    goal: dict | None = None
    stages: tuple = ()
    members: tuple = ()
    enabled: tuple = ()
    memory_mode: str = "off"
    mcp: dict = field(default_factory=dict)
    mcp_delegation: str = "lead_only"
    tools: tuple = DEFAULT_TOOLS
    allow_tests: tuple = ()
    write_paths: tuple = (".",)
    approval_seconds: float = 300
    repeat_limit: int = 3
    replan_limit: int = 1
    model_profile: str | None = None
    fallback_models: tuple = ()
    fallback_errors: tuple = (
        "provider_transport",
        "provider_timeout",
        "provider_server_error",
    )
    context_chars: int = 50000
    output_tokens: int = 4096
    total_seconds: float = 3600
    episode_seconds: float = 300
    episode_model_calls: int = 20
    request_seconds: float = 60
    retries: int = 3
    retry_delay: float = 2
    heartbeat_seconds: float = 2
    heartbeat_timeout: float = 30
    shutdown_seconds: float = 5
    snapshot_limits: dict = field(
        default_factory=lambda: {
            "max_files": 5000,
            "max_bytes": 32000000,
            "max_paths": 100000,
        }
    )

    def __post_init__(self):
        from homebrew_harness.tools.mcp.selection import validate_selection

        if (
            not isinstance(self.enabled, (list, tuple))
            or any(not isinstance(value, str) or not value for value in self.enabled)
            or len(set(self.enabled)) != len(self.enabled)
        ):
            raise ValueError("Run collections must contain unique names: enabled")
        object.__setattr__(self, "mcp", validate_selection(self.mcp))
        if (
            not isinstance(self.mcp_delegation, str)
            or self.mcp_delegation not in {"lead_only", "read_only"}
            or not self.mcp
            and self.mcp_delegation != "lead_only"
        ):
            raise ValueError(
                "MCP delegation requires an explicit selected MCP capability."
            )
        if self.mcp:
            object.__setattr__(
                self, "enabled", tuple(sorted(set(self.enabled) | {"mcp"}))
            )
        elif "mcp" in self.enabled:
            raise ValueError("Managed MCP requires explicit server/tool selection.")
        if not isinstance(self.memory_mode, str) or self.memory_mode not in {
            "off",
            "recall",
            "auto",
        }:
            raise ValueError("Invalid managed memory mode.")
        if (
            not isinstance(self.prompt, str)
            or not self.prompt.strip()
            or len(self.prompt) > 64000
            or redact(self.prompt) != self.prompt
        ):
            raise ValueError(
                "Run prompt must be nonempty text without private connection values, within 64,000 characters."
            )
        from homebrew_harness.execution.source_snapshot import validate_limits

        object.__setattr__(
            self, "snapshot_limits", validate_limits(self.snapshot_limits)
        )
        if self.goal is not None:
            object.__setattr__(self, "goal", validate_spec(self.goal))
        for name in (
            "enabled",
            "tools",
            "allow_tests",
            "write_paths",
            "fallback_models",
            "fallback_errors",
        ):
            values = getattr(self, name)
            if (
                not isinstance(values, (list, tuple))
                or any(not isinstance(v, str) or not v for v in values)
                or len(set(values)) != len(values)
            ):
                raise ValueError("Run collections must contain unique names: " + name)
            object.__setattr__(self, name, tuple(values))
        if (
            not self.write_paths
            or len(self.write_paths) > 64
            or any(
                len(v) > 1000
                or PurePosixPath(v).is_absolute()
                or ".." in PurePosixPath(v).parts
                or "\\" in v
                for v in self.write_paths
            )
        ):
            raise ValueError(
                "Run write paths must be relative workspace path prefixes."
            )
        from .stages import validate_stages, STAGE_TOOLS

        object.__setattr__(self, "stages", validate_stages(self.stages))
        if self.goal and any(
            predicate["workspace"].startswith("stage:")
            and predicate["workspace"][6:]
            not in {stage["key"] for stage in self.stages}
            for predicate in self.goal["predicates"]
        ):
            raise ValueError("Final goal references an unknown managed stage.")
        from .team import validate_members

        object.__setattr__(self, "members", validate_members(self.members, self.stages))
        if ("teams" in self.enabled and not self.members) or (
            "worktrees" in self.enabled
            and not any(stage["isolated"] for stage in self.stages)
        ):
            raise ValueError(
                "Managed team/worktree capabilities require their declared members and isolated stages."
            )
        if self.members:
            object.__setattr__(
                self,
                "enabled",
                tuple(sorted(set(self.enabled) | {"teams", "worktrees"})),
            )
        if any(stage["isolated"] for stage in self.stages):
            object.__setattr__(
                self, "enabled", tuple(sorted(set(self.enabled) | {"worktrees"}))
            )
        if self.stages:
            if self.goal is None:
                raise ValueError(
                    "Staged runs require a final structured delivery goal."
                )
            object.__setattr__(
                self, "enabled", tuple(sorted(set(self.enabled) | {"tasks"}))
            )
        elif "tasks" in self.enabled:
            raise ValueError("Managed tasks require host stage definitions.")
        if set(self.enabled) - {
            "subagents",
            "background",
            "workflow",
            "tasks",
            "teams",
            "worktrees",
            "mcp",
        }:
            raise ValueError(
                "Managed single-agent runs enable subagents/background/workflow; team and scheduler supervision are separate capabilities."
            )
        allowed = (
            CORE_TOOLS
            | (STAGE_TOOLS if self.stages else set())
            | (
                {"spawn_teammate", "list_teammates", "send_message", "request_shutdown"}
                if self.members
                else set()
            )
            | RUN_CONTROL_TOOLS
            | stage_run_tools(self.stages)
            | ({"task"} if "subagents" in self.enabled else set())
            | ({"check_background"} if "background" in self.enabled else set())
            | ({"workflow"} if "workflow" in self.enabled else set())
            | ({"connect_mcp"} if self.mcp else set())
        )
        if self.tools == DEFAULT_TOOLS:
            extra = (
                stage_run_tools(self.stages)
                | (STAGE_TOOLS if self.stages else set())
                | (
                    {
                        "spawn_teammate",
                        "list_teammates",
                        "send_message",
                        "request_shutdown",
                    }
                    if self.members
                    else set()
                )
                | ({"task"} if "subagents" in self.enabled else set())
                | ({"check_background"} if "background" in self.enabled else set())
                | ({"workflow"} if "workflow" in self.enabled else set())
                | ({"connect_mcp"} if self.mcp else set())
            )
            object.__setattr__(self, "tools", tuple(sorted(set(self.tools) | extra)))
        if not self.tools or set(self.tools) - allowed:
            raise ValueError("Run tool selection exceeds its enabled capabilities.")
        object.__setattr__(
            self, "tools", tuple(sorted(set(self.tools) | RUN_CONTROL_TOOLS))
        )
        if len(self.effective_tools()) > 128:
            raise ValueError("Effective managed tool pool exceeds 128 tools.")
        for name, low, high in (
            ("min_free_bytes", 0, 100_000_000_000),
            ("context_chars", 10000, 2000000),
            ("output_tokens", 128, 32768),
            ("episode_model_calls", 1, 100),
            ("retries", 0, 10),
            ("repeat_limit", 2, 20),
            ("replan_limit", 0, 3),
        ):
            if (
                type(getattr(self, name)) is not int
                or not low <= getattr(self, name) <= high
            ):
                raise ValueError("Invalid managed integer limit: " + name)
        for name, low, high in (
            ("total_seconds", 0.1, 604800),
            ("episode_seconds", 0.01, 3600),
            ("request_seconds", 0.01, 3600),
            ("retry_delay", 0.01, 300),
            ("heartbeat_seconds", 0.01, 30),
            ("heartbeat_timeout", 0.05, 300),
            ("shutdown_seconds", 0.01, 60),
            ("approval_seconds", 1, 86400),
        ):
            value = getattr(self, name)
            if (
                type(value) not in {int, float}
                or not math.isfinite(value)
                or not low <= value <= high
            ):
                raise ValueError("Invalid managed time limit: " + name)
        if self.heartbeat_timeout <= self.heartbeat_seconds:
            raise ValueError("Heartbeat timeout must exceed its update interval.")
        if len(self.fallback_models) > 3 or any(
            not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value)
            for value in self.fallback_models
        ):
            raise ValueError("Declare at most three fallback model aliases.")
        if set(self.fallback_errors) - {
            "provider_transport",
            "provider_timeout",
            "provider_server_error",
            "provider_rate_limit",
            "provider_authentication",
            "provider_quota",
        }:
            raise ValueError("Invalid automatic model handoff failure class.")
        if self.model_profile in self.fallback_models:
            raise ValueError("The primary model cannot also be a fallback.")
        if self.model_profile is not None and (
            not isinstance(self.model_profile, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", self.model_profile)
        ):
            raise ValueError("Run model profile must be a host alias.")

    def effective_tools(self):
        from homebrew_harness.tools.mcp.selection import selected_names

        return frozenset(self.tools) | selected_names(self.mcp)

    def data(self):
        return copy.deepcopy(asdict(self))

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict) or set(data) - set(cls.__dataclass_fields__):
            raise ValueError("Unknown managed run settings.")
        return cls(**data)


@dataclass(frozen=True)
class RunOutcome:
    run_id: str
    episode: int
    status: str
    reason_code: str
    checkpoint: str
    text: str = ""
    pending_work: tuple = ()
    required_host_actions: tuple = ()
    next_retry_at: float | None = None
    cleanup_complete: bool = False
    details: dict = field(default_factory=dict)
    verification_ref: dict | None = None

    def data(self):
        return asdict(self)


class EpisodeYield(Exception):
    """A complete loop boundary reached its host-selected segment limit."""


class RunStopped(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason_code = reason


class RunAuthorizationLimit(ModelGatewayError):
    reason_code = "test_authorization_exhausted"
