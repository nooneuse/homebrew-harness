"""registry for the harness."""

from __future__ import annotations

from functools import partial

import homebrew_harness.tools.catalog
import homebrew_harness.tools.files
import homebrew_harness.tools.queries
import homebrew_harness.tools.shell
from homebrew_harness.context.compaction import ContextCompactor
from homebrew_harness.context.skill_loader import SkillLoader
from homebrew_harness.context.todo import TodoManager
from homebrew_harness.execution.background import BackgroundManager
from homebrew_harness.execution.execution_leases import lease_scope
from homebrew_harness.execution.test_runner import TEST_TOOL_NAMES, TestRunner
from homebrew_harness.orchestration.cron_scheduler import CRON_TOOL_NAMES, CronScheduler
from homebrew_harness.orchestration.task_store import TASK_TOOL_NAMES, TaskStore
from homebrew_harness.orchestration.teams import TEAM_TOOL_NAMES, TeamManager
from homebrew_harness.orchestration.workflow_runtime import WorkflowRuntime
from homebrew_harness.runtime.loop_components import LoopComponents
from homebrew_harness.tools.mcp.runtime import MCPRuntime
from homebrew_harness.tools.tool_pool import ToolPool

TOOL_HANDLERS = {
    "bash": homebrew_harness.tools.shell.run_bash,
    "read_file": homebrew_harness.tools.files.run_read,
    "write_file": homebrew_harness.tools.files.run_write,
    "edit_file": homebrew_harness.tools.files.run_edit,
    "glob": homebrew_harness.tools.files.run_glob,
    "search_text": homebrew_harness.tools.queries.search_text,
    "git_status": homebrew_harness.tools.queries.git_status,
    "git_diff": homebrew_harness.tools.queries.git_diff,
}


def create_tool_handlers(
    todo_manager: TodoManager,
    *,
    task_runner=None,
    skill_loader: SkillLoader | None = None,
    compactor: ContextCompactor | None = None,
    task_store: TaskStore | None = None,
    background_manager: BackgroundManager | None = None,
    cron_scheduler: CronScheduler | None = None,
    team_manager: TeamManager | None = None,
    mcp_runtime: MCPRuntime | None = None,
    workflow_runtime: WorkflowRuntime | None = None,
    test_runner: TestRunner | None = None,
) -> dict:
    handlers = {**TOOL_HANDLERS, "todo_write": todo_manager.run_tool}
    if skill_loader is not None:
        handlers["load_skill"] = skill_loader.run_tool
    if task_runner is not None:
        handlers["task"] = task_runner
    if compactor is not None:
        handlers["compact"] = compactor.request_compact
        if handlers["bash"] is homebrew_harness.tools.shell.run_bash:
            handlers["bash"] = partial(
                homebrew_harness.tools.shell.run_bash, archive_store=compactor.store
            )
    if task_store is not None:
        handlers.update(
            {name: partial(task_store.run_tool, name) for name in TASK_TOOL_NAMES}
        )
        for name in ("write_file", "edit_file", "task"):
            if name not in handlers:
                continue
            original_handler = handlers[name]

            def scoped_file(_handler=original_handler, **arguments):
                with lease_scope(task_store.leases):
                    return _handler(**arguments)

            handlers[name] = scoped_file
    if background_manager is not None:
        if background_manager is not None:
            handlers["check_background"] = background_manager.check
    if cron_scheduler is not None:
        handlers.update(
            {name: partial(cron_scheduler.run_tool, name) for name in CRON_TOOL_NAMES}
        )
    if team_manager is not None:
        handlers.update(team_manager.handlers())
    if mcp_runtime is not None:
        handlers["connect_mcp"] = mcp_runtime.run_connect
    if test_runner is not None:
        handlers.update(
            {name: partial(test_runner.run_tool, name) for name in TEST_TOOL_NAMES}
        )
    if workflow_runtime is not None:
        handlers["workflow"] = workflow_runtime.run_tool
    return handlers


def build_tool_pool(client, model, workdir, components: LoopComponents):
    """Compile only prepared dependencies, without constructing or choosing defaults."""
    import homebrew_harness.runtime.turns

    hooks, todo_manager = components.hooks, components.todo_manager
    skill_loader, compactor, test_runner = (
        components.skill_loader,
        components.compactor,
        components.test_runner,
    )
    task_store, background_manager = (
        components.task_store,
        components.background_manager,
    )
    cron_scheduler, team_manager, team_member = (
        components.cron_scheduler,
        components.team_manager,
        components.team_member,
    )
    mcp_runtime, workflow_runtime = components.mcp_runtime, components.workflow_runtime
    managed = (
        components.run_control
        if not components.subagent and components.team_member is None
        else None
    )
    subagent, stop_event = components.subagent, components.stop_event
    capabilities, tool_allowlist = components.capabilities, components.tool_allowlist
    shell_runner, worktree_manager = (
        components.shell_runner,
        components.worktree_manager,
    )
    member_run_tools = frozenset()
    if team_member is not None:
        tools = homebrew_harness.tools.catalog.TEAMMATE_TOOLS
        if getattr(team_member.manager, "managed", None):
            tools = [
                tool for tool in tools if tool["function"]["name"] != "submit_plan"
            ] + [
                tool
                for tool in homebrew_harness.tools.catalog.TOOLS
                if tool["function"]["name"] == "get_handoff"
            ]
            member_run_tools = frozenset({"get_handoff"})
        handlers = {
            name: TOOL_HANDLERS[name]
            for name in homebrew_harness.tools.catalog.BASE_TOOL_NAMES
        }
        handlers.update(team_member.handlers())
        if member_run_tools:
            handlers["get_handoff"] = partial(
                team_member.manager.managed.control.stages.read_member_handoff,
                team_member,
            )
        handlers.update(
            load_skill=skill_loader.run_tool,
            compact=compactor.request_compact,
            todo_write=todo_manager.run_tool,
        )
        handlers["bash"] = partial(
            homebrew_harness.tools.shell.run_bash, archive_store=compactor.store
        )
        handlers.update(
            {name: partial(test_runner.run_tool, name) for name in TEST_TOOL_NAMES}
        )
    elif subagent:
        todo_manager = None
        tools = homebrew_harness.tools.catalog.SUB_TOOLS
        # Restrict execution as well as discovery: invented parent task/plan
        # calls cannot use the parent handlers or recursively delegate.
        handlers = {
            name: TOOL_HANDLERS[name]
            for name in homebrew_harness.tools.catalog.BASE_TOOL_NAMES
        }
        handlers["load_skill"] = skill_loader.run_tool
        handlers["compact"] = compactor.request_compact
        if background_manager is not None:
            handlers["check_background"] = background_manager.check
        handlers.update(
            {name: partial(test_runner.run_tool, name) for name in TEST_TOOL_NAMES}
        )
        if handlers["bash"] is homebrew_harness.tools.shell.run_bash:
            handlers["bash"] = partial(
                homebrew_harness.tools.shell.run_bash, archive_store=compactor.store
            )
    else:
        tools = (
            homebrew_harness.tools.catalog.TOOLS
            if team_manager is not None
            else [
                tool
                for tool in homebrew_harness.tools.catalog.TOOLS
                if tool["function"]["name"]
                not in set(TEAM_TOOL_NAMES) - {"create_worktree"}
            ]
        )
        handlers = create_tool_handlers(
            todo_manager,
            skill_loader=skill_loader,
            compactor=compactor,
            task_store=task_store,
            background_manager=background_manager,
            cron_scheduler=cron_scheduler,
            team_manager=team_manager,
            mcp_runtime=mcp_runtime,
            test_runner=test_runner,
            task_runner=partial(
                homebrew_harness.runtime.turns.run_subagent,
                client=client,
                model=model,
                hooks=hooks,
                skill_loader=skill_loader,
                context_chars=compactor.context_chars,
                stop_event=stop_event,
                mcp_runtime=mcp_runtime,
                parent_intent=compactor.intent,
                test_runner=test_runner,
                bash_policy=components.bash_policy,
                shell_runner=shell_runner,
                model_registry=components.model_registry,
                worker_output_tokens=components.worker_output_tokens,
            ),
        )
        if worktree_manager is not None:
            handlers["create_worktree"] = worktree_manager.run_tool
    if components.artifact_store is not None:
        from homebrew_harness.storage.artifact_store import current_artifact_task

        def artifact_handler(name, **arguments):
            return components.artifact_store.run_tool(
                name, task_id=current_artifact_task(), **arguments
            )

        for name in ("list_artifacts", "get_artifact", "publish_artifacts"):
            handlers[name] = partial(artifact_handler, name)
    if not subagent and team_member is None and capabilities.has("workflow"):
        if workflow_runtime is not None:
            handlers["workflow"] = workflow_runtime.run_tool
    if (
        components.bash_policy is not None
        and components.bash_policy.mode == "container"
        and "bash" in handlers
    ):
        candidate = handlers["bash"]
        if candidate is not homebrew_harness.tools.shell.run_bash and not (
            isinstance(candidate, partial)
            and candidate.func is homebrew_harness.tools.shell.run_bash
        ):
            raise ValueError(
                "Container Bash cannot be replaced by an unconfined host handler."
            )
    if "bash" in handlers and (
        handlers["bash"] is homebrew_harness.tools.shell.run_bash
        or isinstance(handlers["bash"], partial)
        and handlers["bash"].func is homebrew_harness.tools.shell.run_bash
    ):
        handlers["bash"] = partial(
            homebrew_harness.tools.shell.run_bash,
            runner=shell_runner,
            archive_store=compactor.store,
        )
    mcp_cwd, mcp_task, mcp_allow_workspace = workdir, None, True
    if getattr(mcp_runtime, "workspace_scoped", False) is True:
        from homebrew_harness.execution.test_runner import execution_context

        mcp_allow_workspace = team_member is None or team_member.assignment is not None
        mcp_cwd = (
            team_member.cwd()
            if team_member is not None and team_member.assignment is not None
            else team_manager.lead_cwd()
            if team_manager is not None
            and (
                getattr(team_manager, "session_owner", None) is None
                or getattr(team_manager.session_owner, "_runtime_started", False)
            )
            else workdir
        )
        assignment = (
            team_member.assignment
            if team_member
            else team_manager.lead_assignment
            if team_manager
            else None
        )
        mcp_task = execution_context("lead", assignment).task_id
    from homebrew_harness.runs.spec import RUN_CONTROL_TOOLS, stage_run_tools

    run_tools = member_run_tools
    if managed is not None:
        handlers.update(
            request_attention=managed.request_attention,
            execute_approved=lambda **_kwargs: None,
            read_run_event=managed.read_event,
        )
        available = (
            stage_run_tools(managed.spec.stages)
            if managed.stages is not None
            else frozenset()
        )
        endpoints = {
            "get_handoff": managed.read_handoff,
            "integrate_stage": managed.integrate_stage,
            "publish_stage": managed.publish_stage,
        }
        for name in available:
            if name == "get_handoff" or managed.transfers is not None:
                handlers[name] = endpoints[name]
        run_tools = RUN_CONTROL_TOOLS | frozenset(
            name for name in available if name in handlers
        )

    return ToolPool.build(
        tools,
        handlers,
        capabilities,
        tests=test_runner,
        workflow=workflow_runtime,
        background=background_manager,
        mcp=mcp_runtime,
        mcp_workdir=mcp_cwd,
        mcp_task_id=mcp_task,
        mcp_allow_workspace=mcp_allow_workspace,
        allow_connect=not subagent and team_member is None,
        allowlist=tool_allowlist,
        bash_policy=components.bash_policy,
        artifacts=components.artifact_store,
        models=components.model_registry,
        run_tools=run_tools,
    )
