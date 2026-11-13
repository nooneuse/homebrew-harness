"""Host snapshots and execution guards for immediate approval review."""

import copy
from pathlib import Path
from homebrew_harness.security.safe_files import canonical_root
from contextlib import contextmanager
from contextvars import ContextVar

from homebrew_harness.execution.model_gateway import current_scope
from homebrew_harness.runs.requests import workspace_digest

_OWNER = ContextVar("approval_session_owner", default=None)
_GUARD = ContextVar("reviewed_call_guard", default=None)
_EXECUTION = ContextVar("reviewed_call_execution", default=None)


@contextmanager
def owner_scope(owner):
    token = _OWNER.set(owner)
    try:
        yield
    finally:
        _OWNER.reset(token)


def owner():
    return _OWNER.get()


@contextmanager
def permission_scope(execution=None, intent=None):
    token = _GUARD.set(None)
    parent = _EXECUTION.get()
    actor = _EXECUTION.set((execution, intent or (parent[1] if parent else None)))
    try:
        yield
    finally:
        _GUARD.reset(token)
        _EXECUTION.reset(actor)


def bind_guard(call_id, check):
    _GUARD.set((call_id, check))


def verify_guard(call_id):
    value = _GUARD.get()
    if value and value[0] == call_id and not value[1]():
        raise ValueError(
            "Auto-reviewed approval is stale; the action was not executed."
        )


def session_context(session, tool, arguments, cwd, *, requester=None):
    cwd = canonical_root(Path(cwd))
    actor = _EXECUTION.get()
    execution, intent = actor if actor else (None, None)
    task_id = execution.task_id if execution else None
    value = {
        "intent": session.intent.to_dict(),
        "cwd": str(cwd),
        "cwd_identity": (cwd.stat().st_dev, cwd.stat().st_ino),
        "role": execution.role if execution else current_scope().role,
        "task_id": task_id,
        "input_epoch": session.runtime_store.input_guard(session.inputs.recipients),
        "lead_binding": session.lead_binding.identity(),
        "requester_binding": requester.identity() if requester else None,
        "policy": session.approval_reviewer.config.data(),
        "backend": session.bash_policy.identity(),
        "tool": tool,
        "arguments": copy.deepcopy(arguments),
    }
    if tool in {"write_file", "edit_file"}:
        value["workspace"] = workspace_digest(cwd, {"kind": "paths", "values": [arguments["path"]]})
    if intent:
        value["requester_intent"] = intent.to_dict()
    if task_id:
        task = session.workspace.store.load(task_id)
        value["assignment"] = {
            "id": task.id,
            "status": task.status,
            "owner": task.owner,
            "worktree": task.worktree,
            "stage": task.stage,
        }
    if tool == "run_tests":
        recipe = session.tests._recipe(arguments["name"])
        value["workspace"] = workspace_digest(cwd, {"kind": "patterns", "values": list(recipe.input_globs), "limits": recipe.snapshot_limits})
        value["test_recipe"] = {
            "argv": list(recipe.argv),
            "timeout": recipe.timeout,
            "roles": list(recipe.roles),
            "input_globs": list(recipe.input_globs),
            "backend": session.tests.backend.name,
        }
    if tool.startswith("mcp__"):
        value["mcp"] = session.mcp.descriptor(tool, workdir=cwd, task_id=task_id)
    if tool == "publish_artifacts":
        value["artifacts"] = session.artifacts.approval(
            arguments["bundle_id"],
            workdir=cwd,
            task_id=task_id,
        )
    return value
