"""dispatch for the harness."""

from __future__ import annotations

from homebrew_harness.storage.control_boundary import ProposalSuperseded

import json
import re
from pathlib import Path

import homebrew_harness.tools.catalog
from homebrew_harness.context.compaction import ArchiveStore
from homebrew_harness.execution.background import (
    BackgroundManager,
    should_run_background,
)
from homebrew_harness.execution.execution_leases import lease_scope
from homebrew_harness.execution.model_gateway import ModelGatewayError, current_scope
from homebrew_harness.execution.model_gateway import checkpoint as model_checkpoint
from homebrew_harness.execution.test_runner import (
    TestRunner,
    TestRunnerError,
    execution_context,
    test_context,
)
from homebrew_harness.orchestration.goal_loop import GoalController
from homebrew_harness.orchestration.teams import WORKSPACE_TOOLS, TeamManager
from homebrew_harness.orchestration.workflow_runtime import WorkflowRuntime
from homebrew_harness.orchestration.workflow_runtime import matches as workflow_matches
from homebrew_harness.orchestration.workflow_runtime import (
    schema_validator as workflow_schema_validator,
)
from homebrew_harness.runtime.hooks import HookError, HookRegistry, ToolEvent
from homebrew_harness.runtime.observability import trace_tool
from homebrew_harness.security.data_boundary import redact
from homebrew_harness.security.permissions import (
    consume_spawn_approval,
    consume_test_approval,
    mcp_policies,
    spawn_approval_scope,
    test_approval_scope,
    test_policy,
)
from homebrew_harness.storage.durable_runtime import active_writer
from homebrew_harness.tools.mcp.runtime import MCPError, MCPRuntime
from homebrew_harness.tools.tool_outcome import ToolExecutionError


def validate_arguments(arguments: dict, schema: dict) -> None:
    """Validate the small JSON Schema subset used by the registered tools."""

    def check(value, rule: dict, name: str):
        kinds = rule["type"] if isinstance(rule["type"], list) else [rule["type"]]
        if isinstance(value, dict) and "object" in kinds:
            properties = rule["properties"]
            missing = set(rule["required"]) - value.keys()
            unknown = value.keys() - properties.keys()
            if missing or unknown:
                raise ValueError(
                    f"{name}: missing={sorted(missing)}, unknown={sorted(unknown)}."
                )
            for key, item in value.items():
                check(item, properties[key], f"{name}.{key}")
        elif isinstance(value, list) and "array" in kinds:
            if len(value) > rule.get("maxItems", len(value)):
                raise ValueError(f"{name} allows at most {rule['maxItems']} items.")
            for index, item in enumerate(value):
                check(item, rule["items"], f"{name}[{index}]")
        elif isinstance(value, str) and "string" in kinds:
            if len(value) < rule.get("minLength", 0):
                raise ValueError(
                    f"{name} must be a string with the required minimum length."
                )
            if "pattern" in rule and not re.search(rule["pattern"], value):
                raise ValueError(f"{name} must contain non-whitespace characters.")
        elif type(value) is int and "integer" in kinds:
            if value < rule.get("minimum", value):
                raise ValueError(
                    f"{name} must be an integer at least {rule.get('minimum', 0)}."
                )
        elif type(value) is bool and "boolean" in kinds:
            pass
        else:
            raise ValueError(f"{name} must have type {' or '.join(kinds)}.")
        if "enum" in rule and value not in rule["enum"]:
            raise ValueError(f"{name} must be one of {rule['enum']}.")

    check(arguments, schema, "arguments")


def tool_error(error: Exception) -> str:
    if isinstance(error, ToolExecutionError):
        return error.serialize()
    if isinstance(error, ValueError):
        return json.dumps({"error": str(error)}, ensure_ascii=False)
    if isinstance(error, OSError):
        return json.dumps(
            {
                "error": f"{type(error).__name__}: {error.strerror or 'File operation failed.'}"
            }
        )
    return json.dumps({"error": f"Tool failed ({type(error).__name__})."})


def execute_tool(
    call,
    workdir: Path,
    *,
    hooks: HookRegistry,
    handlers: dict,
    background_manager: BackgroundManager | None = None,
    team_member=None,
    team_manager: TeamManager | None = None,
    mcp_runtime: MCPRuntime | None = None,
    mcp_bindings: dict | None = None,
    archive_store: ArchiveStore | None = None,
    restrict_archives: bool = False,
    workflow_runtime: WorkflowRuntime | None = None,
    goal_controller: GoalController | None = None,
    test_runner: TestRunner | None = None,
    artifact_store=None,
    before_start=None,
) -> str:
    if mcp_bindings is None:
        if getattr(mcp_runtime, "workspace_scoped", False) is True:
            allow_workspace = team_member is None or team_member.assignment is not None
            selected_cwd = (
                team_member.cwd()
                if team_member and team_member.assignment
                else team_manager.lead_cwd()
                if team_manager
                else workdir
            )
            assignment = (
                team_member.assignment
                if team_member
                else team_manager.lead_assignment
                if team_manager
                else None
            )
            mcp_bindings = mcp_runtime.snapshot(
                workdir=selected_cwd,
                task_id=execution_context(current_scope().role, assignment).task_id,
                allow_workspace=allow_workspace,
            )[1]
        else:
            mcp_bindings = mcp_runtime.snapshot()[1] if mcp_runtime else {}
    with (
        test_approval_scope(),
        spawn_approval_scope(),
        mcp_policies({name: binding.policy for name, binding in mcp_bindings.items()}),
    ):
        if team_member is not None:
            # Plan changes, task changes and one tool invocation form one step.
            # Model calls remain concurrent; short workspace operations serialize.
            with team_member.tool_step(call.name):
                return _execute_tool(
                    call,
                    workdir,
                    hooks=hooks,
                    handlers=handlers,
                    background_manager=background_manager,
                    team_member=team_member,
                    mcp_runtime=mcp_runtime,
                    mcp_bindings=mcp_bindings,
                    archive_store=archive_store,
                    restrict_archives=restrict_archives,
                    workflow_runtime=workflow_runtime,
                    goal_controller=goal_controller,
                    test_runner=test_runner,
                    artifact_store=artifact_store,
                    before_start=before_start,
                )
        return _execute_tool(
            call,
            workdir,
            hooks=hooks,
            handlers=handlers,
            background_manager=background_manager,
            team_manager=team_manager,
            mcp_runtime=mcp_runtime,
            mcp_bindings=mcp_bindings,
            archive_store=archive_store,
            restrict_archives=restrict_archives,
            workflow_runtime=workflow_runtime,
            goal_controller=goal_controller,
            test_runner=test_runner,
            artifact_store=artifact_store,
            before_start=before_start,
        )


def _execute_tool(
    call,
    workdir: Path,
    *,
    hooks: HookRegistry,
    handlers: dict,
    background_manager: BackgroundManager | None = None,
    team_member=None,
    team_manager: TeamManager | None = None,
    mcp_runtime: MCPRuntime | None = None,
    mcp_bindings: dict | None = None,
    archive_store: ArchiveStore | None = None,
    restrict_archives: bool = False,
    workflow_runtime: WorkflowRuntime | None = None,
    goal_controller: GoalController | None = None,
    test_runner: TestRunner | None = None,
    artifact_store=None,
    before_start=None,
) -> str:
    """Validate, trigger pre hooks, execute, and trigger post hooks for this call."""

    def finish(output):
        output = redact(output)
        if goal_controller is not None:
            try:
                goal_controller.observe_tool(call, workdir, output)
            except Exception as error:
                if goal_controller.pending:
                    goal_controller.state.update(
                        status="error",
                        last_reason="Goal evidence observation failed ("
                        + type(error).__name__
                        + "); goal retained.",
                    )
                    goal_controller._release()
        return trace_tool(
            getattr(call, "name", "unknown"),
            workdir,
            getattr(call, "id", ""),
            output,
        )

    if call.kind != "function":
        return finish(json.dumps({"error": "Only function tools are supported."}))
    handler = handlers.get(call.name)
    binding = (mcp_bindings or {}).get(call.name)
    schema = (
        binding.definition["function"]["parameters"]
        if binding
        else homebrew_harness.tools.catalog.TOOL_SCHEMAS.get(call.name)
    )
    if (handler is None and binding is None) or schema is None:
        return finish(json.dumps({"error": "Unknown tool."}))
    try:
        arguments = json.loads(call.arguments)
        if binding:
            binding.validate(arguments)
        elif call.name == "workflow":
            if not workflow_matches(workflow_schema_validator(schema), arguments):
                raise ValueError("Workflow tool input failed schema validation.")
        else:
            validate_arguments(arguments, schema)
        if call.name == "bash" and arguments.get("artifact_profile") is not None:
            if artifact_store is None or not any(
                p.name == arguments["artifact_profile"]
                for p in artifact_store.policy.outputs
            ):
                raise ValueError(
                    "Artifact output rule is not registered in this runtime."
                )
        external_mutating = binding is not None and not binding.policy.read_only
        workspace_bound = (
            binding is not None
            and getattr(getattr(binding, "connection", None), "workspace", None)
            is not None
        )
        connect_workspace = (
            call.name == "connect_mcp"
            and getattr(mcp_runtime, "workspace_scoped", False) is True
        )
        gate = (
            team_member.guard(call.name, arguments, external_mutating=external_mutating)
            if team_member
            else None
        )
        if team_member and (workspace_bound or connect_workspace):
            workdir = team_member.cwd()
        work_version = team_member.version if team_member else None
        if (
            call.name in WORKSPACE_TOOLS | {"task", "workflow"}
            or workspace_bound
            or connect_workspace
        ) and gate is None:
            if team_member:
                workdir = team_member.cwd()
            elif team_manager:
                workdir = team_manager.lead_cwd()
        assignment = (
            team_member.assignment
            if team_member
            else team_manager.lead_assignment
            if team_manager
            else None
        )
        if (
            assignment is not None
            and assignment.worktree
            and call.name in {"read_file", "write_file", "edit_file", "glob"}
        ):
            raw_path = (
                arguments["pattern"] if call.name == "glob" else arguments["path"]
            )
            if Path(raw_path).parts[:2] == (".worktrees", assignment.worktree):
                raise ValueError(
                    "Tool cwd is already this worktree; use a path relative to it without repeating .worktrees/name."
                )
        if call.name == "read_file" and archive_store is not None:
            if archive_store.owns(arguments["path"]):
                workdir = archive_store.workdir
            elif restrict_archives and archive_store.is_archive(
                arguments["path"], workdir
            ):
                raise ValueError(
                    "This context can only read its own generated context archives."
                )
        if (
            call.name == "read_file"
            and not restrict_archives
            and workflow_runtime is not None
            and workflow_runtime.owns_result(arguments["path"], workdir)
        ):
            workdir = workflow_runtime.store.workdir
        if (
            call.name == "read_file"
            and not restrict_archives
            and goal_controller is not None
            and goal_controller.owns_archive(arguments["path"])
        ):
            workdir = goal_controller.store.workdir
        if (
            call.name == "glob"
            and not restrict_archives
            and goal_controller is not None
            and goal_controller.owns_pattern(arguments["pattern"])
        ):
            workdir = goal_controller.store.workdir
        event = ToolEvent(call.name, arguments, workdir, getattr(call, "id", ""))
        test_call_context = execution_context(
            "teammate" if team_member else current_scope().role, assignment
        )
        if binding is not None and isinstance(mcp_runtime, MCPRuntime):
            mcp_runtime.validate_context(
                binding,
                workdir=workdir,
                task_id=test_call_context.task_id,
                check_inputs=False,
            )
        policy = (
            test_runner.permission(arguments["name"], workdir, test_call_context)
            if event.name == "run_tests" and test_runner
            else None
        )
        test_approval_identity = (
            test_runner.approval_identity(arguments["name"], workdir)
            if policy and policy[0] == "ask"
            else None
        )
        from homebrew_harness.security.permissions import (
            artifact_approval_scope,
            consume_artifact_approval,
        )
        from homebrew_harness.storage.artifact_store import (
            publication_scope,
            artifact_context,
        )

        approval_identity = (
            artifact_store.approval(
                arguments["bundle_id"],
                workdir=workdir,
                task_id=test_call_context.task_id,
            )
            if event.name == "publish_artifacts" and artifact_store
            else None
        )
        with test_policy(policy), artifact_approval_scope(approval_identity):
            blocked = hooks.trigger_hooks("PreToolUse", event)
            artifact_approval = consume_artifact_approval(arguments, workdir)
        if blocked is not None:
            return finish(blocked)
        try:
            model_checkpoint()
        except ModelGatewayError as error:
            return finish(
                json.dumps(
                    {
                        "error": "Tool call not executed: " + str(error),
                        "executed": False,
                    }
                )
            )
        if call.name == "run_tests" and consume_test_approval(arguments, workdir):
            if test_runner is None:
                raise TestRunnerError("Test execution requires its host runner.")
            test_runner.approve(
                arguments["name"],
                cwd=workdir,
                role=test_call_context.role,
                task_id=test_call_context.task_id,
                max_runs=1,
                expected=test_approval_identity,
            )
        if call.name == "spawn_teammate":
            if team_manager is None or not consume_spawn_approval(arguments, workdir):
                raise ValueError(
                    "approval_required: no host approval record exists for this teammate call."
                )
            team_manager.authorize_spawn(arguments)
        if team_member is not None:
            if team_member.version != work_version:
                raise ValueError(
                    "Task assignment changed during tool approval; retry with the current task."
                )
            gate = team_member.guard(
                call.name, arguments, external_mutating=external_mutating
            )
        if gate is not None:
            return finish(gate)
    except HookError as error:
        return finish(json.dumps({"error": str(error), "hook": "PreToolUse"}))
    except Exception as error:
        return finish(tool_error(error))

    if event.name == "publish_artifacts" and artifact_approval is None:
        return finish(
            json.dumps(
                {
                    "error": "Publication requires a recorded exact host approval.",
                    "executed": False,
                }
            )
        )
    if before_start is not None and before_start():
        return finish(
            json.dumps(
                {
                    "error": "User steering arrived before tool acceptance.",
                    "executed": False,
                }
            )
        )
    writer = active_writer()
    try:
        if writer is None and hasattr(before_start, "accept"):
            before_start.accept()
        operation_id = (
            writer.tool_started(
                call, workdir, track=True, guard=getattr(before_start, "token", None)
            )
            if writer
            else None
        )
    except ProposalSuperseded:
        return finish(
            json.dumps(
                {
                    "error": "User steering superseded this tool before acceptance.",
                    "executed": False,
                }
            )
        )
    except ModelGatewayError as error:
        return finish(json.dumps({"error": str(error), "executed": False}))
    try:
        try:
            if binding is not None:
                if mcp_runtime is None:
                    raise MCPError("MCP call requires its owning runtime.")
                # External argument names never become host Python keyword arguments.
                if getattr(mcp_runtime, "workspace_scoped", False) is True:
                    output = mcp_runtime.call(
                        binding,
                        arguments,
                        workdir=workdir,
                        task_id=test_call_context.task_id,
                    )
                else:
                    output = mcp_runtime.call(binding, arguments)
            elif should_run_background(event.name, arguments):
                if background_manager is None or not workdir.resolve().is_relative_to(
                    background_manager.workdir
                ):
                    raise ValueError(
                        "Background execution requires a manager bound to this session's working directory."
                    )
                assignment = (
                    team_manager.background_assignment() if team_manager else None
                )
                output = json.dumps(
                    background_manager.start(
                        arguments["command"],
                        workdir=workdir,
                        assignment=assignment,
                        artifact_profile=arguments.get("artifact_profile"),
                    ),
                    ensure_ascii=False,
                )
            else:
                # The flag selects dispatch only; synchronous/custom Bash handlers
                # retain their original command + workdir contract.
                if event.name == "bash":
                    arguments.pop("run_in_background", None)
                leases = (
                    team_member.manager.leases
                    if team_member
                    else getattr(team_manager, "leases", None)
                    if team_manager
                    else None
                )
                if leases is not None and event.name in WORKSPACE_TOOLS | {"task"}:
                    with (
                        lease_scope(leases),
                        leases.use("workspace:" + str(workdir.resolve())),
                        test_context(test_call_context.role, test_call_context.task_id),
                        artifact_context(test_call_context.task_id),
                        publication_scope(artifact_approval),
                    ):
                        output = handler(workdir=workdir, **arguments)
                else:
                    with (
                        test_context(test_call_context.role, test_call_context.task_id),
                        artifact_context(test_call_context.task_id),
                        publication_scope(artifact_approval),
                    ):
                        output = handler(workdir=workdir, **arguments)
        except Exception as error:
            output = tool_error(error)
            if not isinstance(error, ToolExecutionError) and (
                isinstance(error, ModelGatewayError)
                or getattr(error, "outcome", None) == "unknown"
            ):
                output = json.dumps({"error": str(error), "outcome": "unknown"})
        output = redact(output)
        if writer:
            writer.tool_finished(operation_id, output)
        hooks.trigger_hooks("PostToolUse", event, output)
        return finish(output)
    finally:
        if writer:
            writer.end_operation(operation_id)
