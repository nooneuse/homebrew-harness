"""Explicit host choices for optional runtime modules and model tools."""

import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

OPTIONAL = frozenset(
    {
        "subagents",
        "tasks",
        "background",
        "teams",
        "cron",
        "workflow",
        "worktrees",
        "mcp",
    }
)
CORE_TOOLS = frozenset(
    {
        "bash",
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "search_text",
        "git_status",
        "git_diff",
        "todo_write",
        "compact",
        "load_skill",
        "list_tests",
        "run_tests",
        "get_test_result",
        "list_artifacts",
        "get_artifact",
        "publish_artifacts",
    }
)
MODULE_TOOLS = {
    "subagents": {"task"},
    "tasks": {
        "create_task",
        "update_task",
        "list_tasks",
        "get_task",
        "claim_task",
        "resume_task",
        "complete_task",
    },
    "background": {"check_background"},
    "teams": {
        "spawn_teammate",
        "list_teammates",
        "send_message",
        "request_shutdown",
        "request_plan",
        "review_plan",
        "submit_plan",
    },
    "cron": {"schedule_cron", "list_crons", "cancel_cron"},
    "workflow": {"workflow"},
    "worktrees": {"create_worktree"},
    "mcp": {"connect_mcp"},
}
_ACTIVE = ContextVar("harness_capabilities", default=None)


@dataclass(frozen=True)
class Capabilities:
    enabled: frozenset = field(default_factory=frozenset)
    memory_mode: str = "off"
    details: bool = False

    def __post_init__(self):
        enabled = frozenset(self.enabled)
        if (
            enabled - OPTIONAL
            or self.memory_mode not in {"off", "recall", "auto"}
            or type(self.details) is not bool
        ):
            raise ValueError("Invalid host capabilities.")
        if enabled & {"teams", "worktrees"}:
            enabled = enabled | {"tasks"}
        object.__setattr__(self, "enabled", enabled)

    @classmethod
    def full(cls, *, memory_mode="recall", details=False):
        return cls(OPTIONAL, memory_mode, details)

    @classmethod
    def default(cls):
        profile = os.getenv("HARNESS_PROFILE", "core").strip() or "core"
        if profile not in {"core", "full"}:
            raise ValueError("HARNESS_PROFILE must be core or full.")
        return cls(
            OPTIONAL if profile == "full" else frozenset(),
            os.getenv("HARNESS_MEMORY_MODE", "").strip()
            or ("recall" if profile == "full" else "off"),
        )

    def has(self, name):
        return name in self.enabled

    def tools(self):
        return CORE_TOOLS | frozenset().union(*(MODULE_TOOLS[m] for m in self.enabled))

    def filter(self, tools):
        return [tool for tool in tools if tool["function"]["name"] in self.tools()]

    def manifest(self):
        return {"enabled": sorted(self.enabled), "memory_mode": self.memory_mode}

    def include(self, *names):
        return Capabilities(self.enabled | set(names), self.memory_mode, self.details)


def current():
    return _ACTIVE.get() or Capabilities.default()


@contextmanager
def capability_scope(value):
    token = _ACTIVE.set(value)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def add_arguments(parser):
    parser.add_argument(
        "--profile",
        choices=("core", "full"),
        help="Host module profile; default HARNESS_PROFILE or core.",
    )
    parser.add_argument(
        "--enable",
        action="append",
        choices=sorted(OPTIONAL),
        default=[],
        help="Enable one optional host module.",
    )
    parser.add_argument(
        "--memory-mode",
        choices=("off", "recall", "auto"),
        help="Independent memory recall/maintenance policy.",
    )
    parser.add_argument(
        "--trace-details",
        action="store_true",
        help="Enable bounded, noncritical detailed model observations.",
    )


def from_args(args):
    base = (
        Capabilities.full()
        if args.profile == "full"
        else Capabilities()
        if args.profile == "core"
        else Capabilities.default()
    )
    return Capabilities(
        base.enabled | set(args.enable),
        args.memory_mode
        or os.getenv("HARNESS_MEMORY_MODE", "").strip()
        or base.memory_mode,
        args.trace_details,
    )
