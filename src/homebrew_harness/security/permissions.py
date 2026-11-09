"""s03: hard denials, host-owned rules, and per-call terminal approval."""

from __future__ import annotations

from homebrew_harness.runtime.output import print

import builtins
import json
import re
import select
import sys
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from homebrew_harness.execution.execution_control import (
    check as execution_check,
)
from homebrew_harness.execution.execution_control import (
    operation_deadline,
)
from homebrew_harness.orchestration.cron_scheduler import CRON_TOOL_NAMES
from homebrew_harness.orchestration.task_store import TASK_TOOL_NAMES
from homebrew_harness.orchestration.teams import TEAM_TOOL_NAMES
from homebrew_harness.security.data_boundary import sensitive_path

_INTERACTIVE_APPROVAL = ContextVar("interactive_approval", default=True)
_TEST_POLICY = ContextVar("test_policy", default=None)
_APPROVED_TEST = ContextVar("approved_test", default=None)
_APPROVED_SPAWN = ContextVar("approved_spawn", default=None)
_ARTIFACT_POLICY = ContextVar("artifact_policy", default=None)
_APPROVED_ARTIFACT = ContextVar("approved_artifact", default=None)
_APPROVAL_CALL = ContextVar("approval_call_id", default=None)


@contextmanager
def approval_call_scope(call_id):
    token = _APPROVAL_CALL.set(call_id)
    try:
        yield
    finally:
        _APPROVAL_CALL.reset(token)


@contextmanager
def artifact_approval_scope(policy=None):
    token = _ARTIFACT_POLICY.set(policy)
    approval = _APPROVED_ARTIFACT.set(None)
    try:
        yield
    finally:
        _ARTIFACT_POLICY.reset(token)
        _APPROVED_ARTIFACT.reset(approval)


def record_artifact_approval(arguments, workdir):
    _APPROVED_ARTIFACT.set((dict(arguments), workdir.resolve(), _ARTIFACT_POLICY.get()))


def consume_artifact_approval(arguments, workdir):
    value = _APPROVED_ARTIFACT.get()
    _APPROVED_ARTIFACT.set(None)
    return (
        value[2]
        if value and value[:2] == (dict(arguments), workdir.resolve())
        else None
    )


_MCP_POLICIES = ContextVar("mcp_policies", default={})


@contextmanager
def test_policy(policy):
    token = _TEST_POLICY.set(policy)
    try:
        yield
    finally:
        _TEST_POLICY.reset(token)


@contextmanager
def test_approval_scope():
    token = _APPROVED_TEST.set(None)
    try:
        yield
    finally:
        _APPROVED_TEST.reset(token)


def record_test_approval(arguments, workdir):
    _APPROVED_TEST.set((dict(arguments), workdir.resolve()))


def consume_test_approval(arguments, workdir):
    approved = _APPROVED_TEST.get()
    _APPROVED_TEST.set(None)
    return approved == (dict(arguments), workdir.resolve())


def test_approval_needed():
    policy = _TEST_POLICY.get()
    return policy is not None and policy[0] == "ask"


@contextmanager
def spawn_approval_scope():
    token = _APPROVED_SPAWN.set(None)
    try:
        yield
    finally:
        _APPROVED_SPAWN.reset(token)


def record_spawn_approval(arguments, workdir):
    _APPROVED_SPAWN.set((dict(arguments), workdir.resolve()))


def consume_spawn_approval(arguments, workdir):
    approved = _APPROVED_SPAWN.get()
    _APPROVED_SPAWN.set(None)
    return approved == (dict(arguments), workdir.resolve())


@contextmanager
def mcp_policies(policies: dict):
    token = _MCP_POLICIES.set(policies)
    try:
        yield
    finally:
        _MCP_POLICIES.reset(token)


@contextmanager
def noninteractive_permissions():
    token = _INTERACTIVE_APPROVAL.set(False)
    try:
        yield
    finally:
        _INTERACTIVE_APPROVAL.reset(token)


@dataclass(frozen=True)
class PermissionDecision:
    action: Literal["allow", "deny", "ask"]
    reason: str
    code: str = ""


# These patterns illustrate hard policy rules, not a complete Bash parser.
# Every Bash command that does not match them still requires approval.
_COMMAND_START = r"(?:^|[;&|()\n])\s*(?:(?:command|exec|env)\s+)*(?:/[^\s;&|()]*/)?"
DENY_PATTERNS = (
    (
        re.compile(
            _COMMAND_START
            + r"(?:sudo|shutdown|reboot|mkfs(?:\.[\w-]+)?|dd)(?=\s|$|[;&|()])"
        ),
        "Privileged, system-control, and raw-disk commands are blocked.",
    ),
    (
        re.compile(
            _COMMAND_START
            + r"""rm\s+(?:-[^\s;&|()]+\s+)*["']?/(?:\*{1,2}|\.)?["']?(?=\s|$|[;&|()])"""
        ),
        "Removing the filesystem root is blocked.",
    ),
    (
        re.compile(
            r">\s*[\"']?/dev/(?:sd[a-z]\d*|disk\d+(?:s\d+)*|nvme\d+n\d+(?:p\d+)?)\b"
        ),
        "Redirecting output to a disk device is blocked.",
    ),
)


def check_deny_list(command: str) -> str | None:
    for pattern, reason in DENY_PATTERNS:
        if pattern.search(command):
            return reason
    return None


def check_rules(tool_name: str, arguments: dict, workdir: Path) -> PermissionDecision:
    """Classify validated arguments without executing a tool or asking for input."""
    if tool_name in {"list_artifacts", "get_artifact"}:
        return PermissionDecision(
            "allow", "Inspect the assigned isolated artifact bundle."
        )
    if tool_name == "publish_artifacts":
        approval = _ARTIFACT_POLICY.get()
        return (
            PermissionDecision(
                "ask",
                "Create only these frozen artifact files: "
                + json.dumps(approval, ensure_ascii=True),
            )
            if approval
            else PermissionDecision(
                "deny", "No verified host artifact manifest is bound to this call."
            )
        )
    if tool_name == "run_tests":
        policy = _TEST_POLICY.get()
        return (
            PermissionDecision(*policy)
            if policy is not None
            else PermissionDecision(
                "deny", "No host TestRunner policy is bound to this call."
            )
        )
    if tool_name in {"list_tests", "get_test_result"}:
        return PermissionDecision(
            "allow", "Read host-registered test names and current-session receipts."
        )
    if tool_name == "connect_mcp":
        return PermissionDecision(
            "allow", "Connect only a server registered by the host configuration."
        )
    if tool_name.startswith("mcp__"):
        policy = _MCP_POLICIES.get().get(tool_name)
        if policy is None:
            return PermissionDecision(
                "deny", "No verified MCP binding is registered for this call."
            )
        action = {"allow": "allow", "confirm": "ask", "deny": "deny"}[policy.action]
        return PermissionDecision(
            action,
            "MCP authorization comes from host policy; server hints grant no permission.",
        )
    if tool_name == "workflow":
        return PermissionDecision(
            "allow",
            "Enabled host-registered workflows use tool-free inference steps and bounded execution.",
        )
    if tool_name in {"search_text", "git_status", "git_diff"}:
        return PermissionDecision("allow", "Read-only bounded workspace inspection.")
    if tool_name == "compact":
        return PermissionDecision(
            "allow",
            "Archive and summarize the current conversation after this tool batch.",
        )
    if tool_name == "load_skill":
        return PermissionDecision(
            "allow", "Read-only access to a registered skill snapshot."
        )
    if tool_name == "task":
        return PermissionDecision(
            "allow",
            "The subagent uses the same working directory and tool permission checks.",
        )
    if tool_name == "todo_write":
        return PermissionDecision(
            "allow", "Plan updates affect only the current session."
        )
    if tool_name == "check_background":
        return PermissionDecision(
            "allow",
            "Read-only status and results from this session's background manager.",
        )
    if tool_name in TASK_TOOL_NAMES:
        return PermissionDecision(
            "allow",
            "Task tools validate and update host-managed records inside the workspace.",
        )
    if tool_name in CRON_TOOL_NAMES:
        return PermissionDecision(
            "allow",
            "Cron tools manage local scheduled prompt definitions with normal execution permissions.",
        )
    if tool_name == "spawn_teammate":
        return PermissionDecision(
            "ask",
            "Starting this named teammate spends model calls; approve this exact role, prompt and assignment.",
        )
    if tool_name in TEAM_TOOL_NAMES or tool_name == "submit_plan":
        return PermissionDecision(
            "allow",
            "Team tools bind host identities, task ownership and protocol state; execution permissions remain in force.",
        )
    if tool_name == "bash":
        from homebrew_harness.execution.bash_policy import current_bash_policy

        if current_bash_policy().mode == "container":
            return PermissionDecision(
                "ask",
                "Approve this command in a filtered container workspace copy; network disabled and no host-file writeback. Policy: "
                + current_bash_policy().fingerprint()[:12],
            )
        return PermissionDecision(
            "ask", "Bash commands require approval because they can modify the host."
        )

    if tool_name in {"read_file", "write_file", "edit_file", "glob"}:
        path = arguments["pattern"] if tool_name == "glob" else arguments["path"]
        if tool_name == "glob" and (
            Path(path).is_absolute() or ".." in Path(path).parts
        ):
            return PermissionDecision(
                "deny", "glob requires a relative pattern without '..'."
            )
        try:
            root = workdir.resolve()
            resolved = (root / path).resolve()
        except (OSError, RuntimeError, ValueError):
            return PermissionDecision(
                "deny", "The requested file path could not be resolved."
            )
        from homebrew_harness.security.file_visibility import (
            assert_model_write,
            assert_visible,
            check_pattern,
        )

        try:
            if tool_name == "read_file":
                assert_visible(root, path)
            elif tool_name == "glob":
                check_pattern(path)
            else:
                assert_model_write(root, path)
                assert_model_write(root, resolved)
        except ValueError as error:
            return PermissionDecision("deny", str(error))
        if sensitive_path(Path(path).parts) or sensitive_path(
            resolved.relative_to(root).parts if resolved.is_relative_to(root) else ()
        ):
            return PermissionDecision(
                "deny",
                "Sensitive configuration and credential paths are not exposed to file tools.",
            )
        if not resolved.is_relative_to(root):
            return PermissionDecision(
                "deny", "File tools cannot access paths outside the working directory."
            )
        return PermissionDecision(
            "allow", "File operation is inside the working directory."
        )

    return PermissionDecision(
        "deny", "No permission policy is registered for this tool."
    )


def ask_user(
    tool_name: str, arguments: dict, workdir: Path, reason: str
) -> PermissionDecision:
    """Only a terminal response can approve this exact invocation."""
    from homebrew_harness.runs.controller import current_run
    from homebrew_harness.execution.model_gateway import current_scope
    from homebrew_harness.runs.requests import current_approval

    managed = current_run()
    if managed is not None:
        if (
            tool_name == "spawn_teammate"
            and managed.team_control is not None
            and current_scope().role == "lead"
        ):
            managed.team_control.authorize(dict(arguments))
            return PermissionDecision(
                "allow", "Exact host-declared managed member scope."
            )
        approved = current_approval()
        if (
            approved is not None
            and approved["tool"] == tool_name
            and approved["arguments"] == dict(arguments)
        ):
            return PermissionDecision("allow", "Exact durable host approval matches.")
        if current_scope().role != "lead":
            return PermissionDecision(
                "deny",
                "Delegate permission changes through the managed Lead.",
                "approval_requires_lead",
            )
        request = managed.requests.create(
            managed,
            "tool",
            tool=tool_name,
            arguments=dict(arguments),
            call_id=_APPROVAL_CALL.get(),
            reason=reason,
        )
        if request["state"] == "denied":
            return PermissionDecision("deny", "The host declined this exact request.")
        managed.waiting_request = request["id"]
        return PermissionDecision(
            "deny",
            "Waiting for exact host request " + request["id"],
            "approval_required",
        )
    from homebrew_harness.runtime.input_router import current_broker

    broker = current_broker()
    if broker is not None and _INTERACTIVE_APPROVAL.get():
        return broker.request(tool_name, arguments, workdir, reason)
    if (
        not _INTERACTIVE_APPROVAL.get()
        or threading.current_thread() is not threading.main_thread()
    ):
        return PermissionDecision(
            "deny",
            "Scheduled or worker turns cannot request interactive approval.",
            "approval_required",
        )
    if not sys.stdin.isatty():
        target = "Bash" if tool_name == "bash" else "this external tool call"
        return PermissionDecision(
            "deny",
            "Interactive approval is unavailable; run from a terminal to approve "
            + target
            + ".",
            "approval_required",
        )

    print(f"\n[permission] {reason}", file=sys.stderr)
    # JSON escaping prevents command text from injecting terminal control sequences.
    print(
        "  Directory: " + json.dumps(str(workdir.resolve()), ensure_ascii=False),
        file=sys.stderr,
    )
    print(
        "  Request: "
        + json.dumps({"tool": tool_name, "arguments": arguments}, ensure_ascii=False),
        file=sys.stderr,
    )
    try:
        deadline = operation_deadline(120)
        if getattr(builtins.input, "__module__", None) == "builtins":
            print("Allow this call? [y/N] ", end="", flush=True)
            while True:
                execution_check(deadline)
                remaining = deadline.remaining()
                if select.select(
                    [sys.stdin],
                    [],
                    [],
                    min(0.05, remaining) if remaining is not None else 0.05,
                )[0]:
                    break
            choice = input().strip().lower()
        else:
            choice = input("Allow this call? [y/N] ").strip().lower()
        execution_check(deadline)
    except EOFError:
        return PermissionDecision("deny", "Approval input ended without confirmation.")
    if choice in {"y", "yes"}:
        return PermissionDecision("allow", "User approved this invocation.")
    return PermissionDecision("deny", "User declined this invocation.")


def check_permission(
    tool_name: str, arguments: dict, workdir: Path
) -> PermissionDecision:
    """Hard denial first, then rule matching, then approval when required."""
    from homebrew_harness.runs.controller import current_run

    managed = current_run()
    if (
        managed is not None
        and tool_name in {"write_file", "edit_file"}
        and not managed.write_allowed(workdir, arguments["path"])
    ):
        return PermissionDecision(
            "deny",
            "The path is outside this Run's host-authorized write prefixes.",
            "write_boundary",
        )
    if managed is not None and tool_name in {
        "request_attention",
        "read_run_event",
        "get_handoff",
        "integrate_stage",
        "publish_stage",
    }:
        return PermissionDecision("allow", "Use the owning Run's control records.")
    if (
        managed is not None
        and tool_name == "publish_artifacts"
        and _ARTIFACT_POLICY.get() is not None
    ):
        if any(
            not managed.write_allowed(workdir, row["target"])
            for row in _ARTIFACT_POLICY.get()["files"]
        ):
            return PermissionDecision(
                "deny",
                "Artifact targets exceed this Run's authorized write prefixes.",
                "write_boundary",
            )
    if tool_name == "bash":
        reason = check_deny_list(arguments["command"])
        if reason:
            return PermissionDecision("deny", reason)

    decision = check_rules(tool_name, arguments, workdir)
    if decision.action == "ask":
        return ask_user(tool_name, arguments, workdir, decision.reason)
    return decision


def interactive_available():
    return (
        _INTERACTIVE_APPROVAL.get()
        and threading.current_thread() is threading.main_thread()
        and sys.stdin.isatty()
    )
