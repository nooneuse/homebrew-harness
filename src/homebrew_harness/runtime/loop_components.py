"""Actual loop dependencies; building prompts and tools never constructs resources."""

from dataclasses import dataclass


@dataclass(frozen=True)
class LoopComponents:
    hooks: object = None
    todo_manager: object = None
    skill_loader: object = None
    compactor: object = None
    test_runner: object = None
    task_store: object = None
    background_manager: object = None
    cron_scheduler: object = None
    team_manager: object = None
    team_member: object = None
    mcp_runtime: object = None
    workflow_runtime: object = None
    bash_policy: object = None
    shell_runner: object = None
    artifact_store: object = None
    worktree_manager: object = None
    capabilities: object = None
    subagent: bool = False
    stop_event: object = None
    tool_allowlist: object = None
    context_chars: int = 50_000
    archive_namespace: str | None = None
    model_registry: object = None
    worker_output_tokens: int | None = None
    steering: object = None
    on_user_submit: object = None
    run_control: object = None
