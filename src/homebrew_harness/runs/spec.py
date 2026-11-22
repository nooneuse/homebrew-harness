"""Host-selected managed execution settings and structured results."""

from homebrew_harness.configuration import (
    DEFAULT_SHUTDOWN_SECONDS,
    DEFAULT_CONTEXT_CHARS,
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_MODEL_REQUEST_SECONDS,
    DEFAULT_RUN_SECONDS,
    DEFAULT_TASK_SECONDS,
)

import copy
import math
import re
from pathlib import PurePosixPath
from dataclasses import asdict, dataclass, field

from homebrew_harness.runtime.errors import boundary_error
from homebrew_harness.runtime.limits import current_limits
from homebrew_harness.runtime.capabilities import CORE_TOOLS
from homebrew_harness.orchestration.hard_conditions import validate_spec
from homebrew_harness.security.data_boundary import redact
from homebrew_harness.execution.model_gateway import ModelGatewayError

# Admission bounds retained input; the selected model checks usable context.
MAX_RUN_PROMPT_CHARS = 2_000_000

TERMINAL = {"completed", "failed", "cancelled"}
RUN_CONTROL_TOOLS = frozenset(
    {"request_attention", "execute_approved", "read_run_event", "wait_for_source"}
)
RUN_STAGE_TOOLS = frozenset({"get_handoff", "integrate_stage", "publish_stage"})
RUN_RECOVERY_TOOLS = frozenset({"report_run_blocker"})
from homebrew_harness.services.model_work_tools import NAMES as SERVICE_WORK_TOOLS
RUN_WORK_TOOLS = frozenset({"submit_work_progress", "get_work_progress"}) | SERVICE_WORK_TOOLS
RUN_TOOLS = RUN_CONTROL_TOOLS | RUN_RECOVERY_TOOLS | RUN_STAGE_TOOLS | RUN_WORK_TOOLS
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
    work_context: dict = field(default_factory=dict)
    mcp: dict = field(default_factory=dict)
    mcp_delegation: str = "lead_only"
    tools: tuple = DEFAULT_TOOLS
    allow_tests: tuple = ()
    write_paths: tuple = (".",)
    approval_seconds: float | None = None
    approval_review: dict | None = None
    repeat_limit: int | None = None
    repeat_warning: int | None = 3
    replan_limit: int | None = None
    model_profile: str | None = None
    fallback_models: tuple = ()
    fallback_errors: tuple = (
        "provider_transport",
        "provider_timeout",
        "provider_server_error",
    )
    context_chars: int = DEFAULT_CONTEXT_CHARS
    output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    total_seconds: float | None = None
    total_warning_seconds: float | None = DEFAULT_RUN_SECONDS
    episode_seconds: float | None = DEFAULT_TASK_SECONDS
    episode_model_calls: int | None = 20
    request_seconds: float = DEFAULT_MODEL_REQUEST_SECONDS
    retries: int | None = None
    retry_warning: int | None = 3
    runtime_limits: dict = field(default_factory=lambda: current_limits().data())
    retry_delay: float = 2
    heartbeat_seconds: float = 2
    heartbeat_timeout: float = 30
    shutdown_seconds: float = DEFAULT_SHUTDOWN_SECONDS
    snapshot_limits: dict = field(
        default_factory=lambda: {
            "max_files": 5000,
            "max_bytes": 32000000,
            "max_paths": 100000,
        }
    )

    def __post_init__(self):
        import json
        from homebrew_harness.runtime.limits import RuntimeLimits, positive
        object.__setattr__(self, "runtime_limits", RuntimeLimits.parse(self.runtime_limits).data())
        positive(self.repeat_warning, "repeat_warning", nullable=True, integer=True)
        positive(self.retry_warning, "retry_warning", nullable=True, integer=True)
        positive(self.total_warning_seconds, "total_warning_seconds", nullable=True)

        try:
            context_size = len(json.dumps(self.work_context, allow_nan=False))
        except (TypeError, ValueError):
            raise ValueError("Invalid Service work context.") from None
        if (
            not isinstance(self.work_context, dict)
            or context_size > self.runtime_limits["work_context_chars"]
            or self.work_context
            and (
                not isinstance(self.work_context.get("work_id"), str)
                or not re.fullmatch(r"work_[0-9a-f]{32}", self.work_context["work_id"])
                or not isinstance(self.work_context.get("service_id"), str)
                or not re.fullmatch(
                    r"svc_[0-9a-f]{32}", self.work_context["service_id"]
                )
                or type(self.work_context.get("requirements_revision")) is not int
                or self.work_context["requirements_revision"] < 0
            )
        ):
            raise ValueError("Invalid Service work context.")
        if self.approval_review is not None:
            from homebrew_harness.security.approval_review import ReviewConfig

            ReviewConfig.parse(self.approval_review)
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
        if not isinstance(self.prompt, str) or not self.prompt.strip():
            raise ValueError("Run prompt must be nonempty text.")
        if len(self.prompt) > self.runtime_limits["run_prompt_chars"]:
            raise boundary_error(
                ValueError,
                "run_prompt_limit",
                "Combined Run prompt exceeds its character limit.",
                source="run",
                phase="prepare",
                limit_name="run.prompt_characters",
                limit_value=self.runtime_limits["run_prompt_chars"],
                observed_value=len(self.prompt),
                unit="characters",
            )
        if redact(self.prompt) != self.prompt:
            raise ValueError("Run prompt contains private connection values.")
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

        object.__setattr__(self, "stages", validate_stages(self.stages, self.runtime_limits))
        if self.goal and any(
            predicate["workspace"].startswith("stage:")
            and predicate["workspace"][6:]
            not in {stage["key"] for stage in self.stages}
            for predicate in self.goal["predicates"]
        ):
            raise ValueError("Final goal references an unknown managed stage.")
        from .team import validate_members

        object.__setattr__(self, "members", validate_members(self.members, self.stages, self.runtime_limits))
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
            | RUN_RECOVERY_TOOLS
            | stage_run_tools(self.stages)
            | (
                {"task", "read_subagent_result"}
                if "subagents" in self.enabled
                else set()
            )
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
        if self.runtime_limits["tool_pool_size"] is not None and len(self.effective_tools()) > self.runtime_limits["tool_pool_size"]:
            raise ValueError("Effective managed tool pool exceeds 128 tools.")
        for name, zero in (("min_free_bytes", True), ("context_chars", False), ("output_tokens", False),
                           ("episode_model_calls", False), ("retries", True), ("repeat_limit", False),
                           ("replan_limit", True)):
            positive(getattr(self, name), name, nullable=name in {"retries", "repeat_limit", "replan_limit", "episode_model_calls"},
                     integer=True, zero=zero)
        for name in ("total_seconds", "episode_seconds", "request_seconds", "retry_delay", "heartbeat_seconds",
                     "heartbeat_timeout", "shutdown_seconds", "approval_seconds"):
            positive(getattr(self, name), name, nullable=name in {"total_seconds", "approval_seconds", "episode_seconds"})
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

        controls = (
            {"report_run_blocker"} if "request_attention" in self.tools else set()
        )
        if "task" in self.tools:
            controls.add("read_subagent_result")
        return (
            frozenset(self.tools)
            | selected_names(self.mcp)
            | controls
            | (
                {"submit_work_progress", "get_work_progress"}
                if self.work_context
                else set()
            )
        )

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
