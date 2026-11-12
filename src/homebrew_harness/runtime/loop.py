"""loop for the harness."""

from __future__ import annotations

from homebrew_harness.runtime.activities import activity

from homebrew_harness.runtime.output import print

import json
import sys
import threading
from pathlib import Path

import homebrew_harness.runtime.assembly
import homebrew_harness.runtime.errors
import homebrew_harness.tools.dispatch
import homebrew_harness.tools.registry
from homebrew_harness.context.compaction import ContextCompactor, is_context_overflow
from homebrew_harness.context.skill_loader import SkillLoader
from homebrew_harness.context.todo import REMINDER_INTERVAL, TODO_REMINDER, TodoManager
from homebrew_harness.execution.background import BackgroundManager
from homebrew_harness.execution.model_gateway import (
    ModelCancelled,
    ModelDeadlineExceeded,
    ModelGatewayError,
)
from homebrew_harness.execution.test_runner import TestRunner
from homebrew_harness.orchestration.cron_scheduler import CronScheduler
from homebrew_harness.orchestration.goal_loop import GoalController, GoalError
from homebrew_harness.orchestration.task_store import TaskStore
from homebrew_harness.orchestration.teams import TeamManager
from homebrew_harness.orchestration.workflow_runtime import WorkflowRuntime
from homebrew_harness.runtime.hooks import HookRegistry
from homebrew_harness.runtime.loop_components import LoopComponents
from homebrew_harness.security.data_boundary import model_request, redact
from homebrew_harness.storage.durable_runtime import (
    RecoveryRequired,
    active_writer,
    save_boundary,
)
from homebrew_harness.tools.mcp.runtime import MCPRuntime
from homebrew_harness.tools.tool_outcome import needs_inspection
from homebrew_harness.tools.tool_pool import ToolPool


def inject_background_results(
    messages: list[dict],
    manager: BackgroundManager,
    goal_controller: GoalController | None = None,
) -> int:
    results = manager.collect()
    for result in results:
        if goal_controller is not None:
            goal_controller.observe_notification("background", result)
        messages.append(
            {
                "role": "user",
                "name": "background_notification",
                "content": "<task_notification>\n"
                + json.dumps(result, ensure_ascii=False)
                + "\n</task_notification>",
            }
        )
        print(f"[background] {result['bg_id']}: {result['status']}", file=sys.stderr)
    if results:
        if active_writer():
            save_boundary(
                messages, ack=[("background", None, [r["bg_id"] for r in results])]
            )
        else:
            manager.acknowledge(results)
    return len(results)


def set_runtime_context(
    messages: list[dict], workdir: Path, *, team_manager=None, team_member=None
):
    if team_member is not None:
        assignment = team_member.assignment
        cwd = team_member.cwd() if assignment else None
    else:
        cwd = team_manager.lead_cwd() if team_manager else workdir
        assignment = team_manager.lead_assignment if team_manager else None
    content = {
        "base_workspace": str(workdir),
        "execution_cwd": str(cwd) if cwd else None,
        "task_id": assignment.id if assignment else None,
        "path_rule": "Workspace file/Bash paths are relative to execution_cwd. Use summary.txt, not .worktrees/name/summary.txt when cwd is already that worktree.",
        "workspace_tools_available": cwd is not None,
    }
    if team_member is not None:
        content["allowed_task_ids"] = sorted(team_member.allowed_tasks)
    value = {
        "role": "system",
        "name": "runtime_context",
        "content": json.dumps(content, ensure_ascii=False),
    }
    position = (
        2
        if len(messages) > 1 and messages[1].get("name") == "task_intent"
        else 1
        if messages and messages[0].get("role") == "system"
        else 0
    )
    from homebrew_harness.context.tracked_history import set_context

    set_context(messages, "runtime_context", value, position=position)


def agent_loop(
    client: object,
    model: str,
    messages: list[dict],
    workdir: Path,
    max_turns: int = 20,
    *,
    hooks: HookRegistry | None = None,
    todo_manager: TodoManager | None = None,
    subagent: bool = False,
    skill_loader: SkillLoader | None = None,
    compactor: ContextCompactor | None = None,
    active_request: str | None = None,
    task_store: TaskStore | None = None,
    background_manager: BackgroundManager | None = None,
    cron_scheduler: CronScheduler | None = None,
    on_first_response=None,
    stop_event: threading.Event | None = None,
    team_manager: TeamManager | None = None,
    team_member=None,
    mcp_runtime: MCPRuntime | None = None,
    collect_background: bool = True,
    workflow_runtime: WorkflowRuntime | None = None,
    goal_controller: GoalController | None = None,
    stop_block_cap: int = 8,
    goal_observer: GoalController | None = None,
    test_runner: TestRunner | None = None,
    tool_allowlist: frozenset[str] | None = None,
    capabilities=None,
    shell_runner=None,
    bash_policy=None,
    model_registry=None,
    worker_output_tokens=None,
    steering=None,
) -> str:
    """Use the same preparation path as recalled user and automatic turns."""
    if type(stop_block_cap) is not int or not 1 <= stop_block_cap <= 100:
        raise GoalError("Stop continuation cap must be between 1 and 100.")
    components = LoopComponents(
        hooks=hooks,
        todo_manager=todo_manager,
        subagent=subagent,
        skill_loader=skill_loader,
        compactor=compactor,
        task_store=task_store,
        background_manager=background_manager,
        cron_scheduler=cron_scheduler,
        stop_event=stop_event,
        team_manager=team_manager,
        team_member=team_member,
        mcp_runtime=mcp_runtime,
        workflow_runtime=workflow_runtime,
        test_runner=test_runner,
        tool_allowlist=tool_allowlist,
        capabilities=capabilities,
        shell_runner=shell_runner,
        bash_policy=bash_policy,
        model_registry=model_registry,
        worker_output_tokens=worker_output_tokens,
        steering=steering,
    )
    with homebrew_harness.runtime.assembly._prepare_loop(
        client, model, workdir, components
    ) as prepared:
        return prepared.run(
            messages,
            max_turns,
            active_request=active_request,
            on_first_response=on_first_response,
            collect_background=collect_background,
            goal_controller=goal_controller,
            stop_block_cap=stop_block_cap,
            goal_observer=goal_observer,
        )


def _agent_loop(
    client,
    model,
    messages,
    workdir,
    max_turns=20,
    *,
    components: LoopComponents,
    first_pool: ToolPool,
    active_request=None,
    on_first_response=None,
    collect_background=True,
    goal_controller=None,
    stop_block_cap=8,
    goal_observer=None,
):
    """Append each assistant turn and all matching tool results before continuing."""
    hooks, todo_manager, compactor = (
        components.hooks,
        components.todo_manager,
        components.compactor,
    )
    task_store, background_manager = (
        components.task_store,
        components.background_manager,
    )
    team_manager, team_member, mcp_runtime = (
        components.team_manager,
        components.team_member,
        components.mcp_runtime,
    )
    workflow_runtime, test_runner = components.workflow_runtime, components.test_runner
    subagent, stop_event = components.subagent, components.stop_event
    if active_request is None:
        # CLI and child entry points pass the actual request explicitly. This
        # convenience fallback is captured once, before any runtime messages.
        active_request = next(
            (
                message["content"]
                for message in reversed(messages)
                if message.get("role") == "user"
                and isinstance(message.get("content"), str)
            ),
            "",
        )
    if not compactor.intent.original:
        original = next(
            (
                message.get("content", "")
                for message in messages
                if message.get("role") == "user" and not message.get("name")
            ),
            active_request,
        )
        compactor.intent.update(original)
    rounds_since_todo = 0
    stop_blocks = 0
    turns = 0
    steering = components.steering if not subagent and team_member is None else None
    reactive_retries = 0
    first_response = True

    def commit_stop(text, proposal, pending=()):
        if components.run_control is not None:
            pending = components.run_control.pending_work()
        candidate = (
            goal_controller.propose_after_turn(text, pending_work=pending)
            if goal_controller
            else None
        )
        decision = candidate[0] if candidate else None
        if components.run_control is not None:
            components.run_control.stage_result(text, decision, pending)
        if steering is not None:
            if not steering.commit_end(
                messages,
                todo_manager,
                compactor,
                proposal.token,
                goal_controller,
                candidate,
            ):
                return False, None
        else:
            if proposal is not None and proposal():
                return False, None
            if candidate and candidate[1] is not None:
                goal_controller.store.save(candidate[1])
                goal_controller.accept_candidate(candidate[1])
            writer = active_writer()
            if writer is not None and proposal is not None:
                from homebrew_harness.storage.control_boundary import ProposalSuperseded

                try:
                    writer.save("executed", boundary={"guard": proposal.token})
                except ProposalSuperseded:
                    return False, None
            else:
                save_boundary(
                    messages,
                    todo_manager,
                    compactor.intent,
                    "running"
                    if decision and decision.action == "block"
                    else "executed",
                )
        return True, decision

    from homebrew_harness.context.project_guidance import ProjectGuidance
    guidance = components.project_guidance or ProjectGuidance(workdir)
    def guidance_cwd():
        return team_member.cwd() if team_member and team_member.assignment is not None else team_manager.lead_cwd() if team_manager else workdir

    while True:
        if components.run_control is not None:
            components.run_control.safe_point(turns)
        pool = (
            first_pool
            if turns == 0
            else homebrew_harness.tools.registry.build_tool_pool(
                client, model, workdir, components
            )
        )
        tools, mcp_bindings = pool.tools, pool.bindings
        if team_member is not None:
            team_member.before_model()
        if team_manager is not None or team_member is not None:
            set_runtime_context(
                messages, workdir, team_manager=team_manager, team_member=team_member
            )
        if goal_controller is not None and not subagent and team_member is None:
            from homebrew_harness.context.tracked_history import set_context

            set_context(
                messages,
                "goal_context",
                goal_controller.context(),
                position=1 if messages and messages[0].get("role") == "system" else 0,
            )
        if stop_event is not None and stop_event.is_set():
            raise homebrew_harness.runtime.errors.AgentError(
                "Agent turn stopped because the session is closing."
            )
        if turns >= max_turns:
            if goal_controller is not None and goal_controller.pending:
                return goal_controller.report(
                    "",
                    goal_controller.suspend(
                        "limit", "Global main-model turn limit reached; goal retained."
                    ),
                )
            raise homebrew_harness.runtime.errors.AgentError(
                "Model-turn limit reached; the task may still be unfinished."
            )
        turns += 1

        if collect_background and background_manager is not None:
            inject_background_results(messages, background_manager, goal_controller)
        if steering is not None:
            steering.apply(messages, todo_manager, compactor)
        if components.run_control is not None:
            components.run_control.before_model(messages, components)
        guidance.refresh(messages, guidance_cwd())
        plan = todo_manager.render() if todo_manager is not None else ""
        messages[:] = compactor.prepare(messages, active_request, plan, tools)
        if stop_event is not None and stop_event.is_set():
            raise homebrew_harness.runtime.errors.AgentError(
                "Agent turn stopped before starting another model request."
            )
        if goal_controller is not None:
            stopped = goal_controller.before_worker(max_turns)
            if stopped:
                return goal_controller.report("", stopped)
        save_boundary(messages, todo_manager, compactor.intent)
        assignment = (
            team_member.assignment
            if team_member
            else team_manager.lead_assignment
            if team_manager
            else task_store.owned()
            if task_store
            else None
        )
        if steering is not None:
            steering.phase = "model"
        from homebrew_harness.runtime.proposal import Proposal

        proposal = (
            Proposal(steering.session.runtime_store, steering.recipients)
            if steering is not None
            else Proposal(
                team_member.manager.bus.store,
                [team_member.manager.bus.proposal_recipient(team_member.name)],
                version=lambda: (team_member.version, team_member.user_revision),
            )
            if team_member is not None
            else None
        )
        member_revision = getattr(team_member, "user_revision", 0)
        try:
            with client.scope(cancel_event=steering.cancel if steering else None):
                response = model_request(
                    client,
                    purpose="worker",
                    role="teammate"
                    if team_member
                    else "subagent"
                    if subagent
                    else "lead",
                    task_id=assignment.id if assignment else None,
                    model=model,
                    messages=messages,
                    max_tokens=components.worker_output_tokens
                    or client.worker_output_tokens,
                    protocol_root=compactor.store.workdir,
                    **({"tools": tools} if tools else {}),
                )
        except ModelCancelled as error:
            if (
                steering is not None
                and not steering.session._closed
                and steering.superseded()
            ):
                steering.apply(messages, todo_manager, compactor)
                continue
            raise homebrew_harness.runtime.errors.AgentError(
                "Agent turn stopped before executing model actions."
            ) from error
        except ModelDeadlineExceeded as error:
            if components.run_control is not None:
                raise
            if goal_controller is not None and goal_controller.pending:
                return goal_controller.report(
                    "", goal_controller.suspend("limit", str(error))
                )
            raise
        except Exception as error:
            if is_context_overflow(error) and reactive_retries < 1:
                messages[:] = compactor.compact_history(
                    messages, active_request, plan, keep_recent=5
                )
                reactive_retries += 1
                continue
            raise
        reactive_retries = 0
        if goal_controller is not None:
            goal_controller.worker_response(response)
        if response.finish not in {"completed", "tool_calls"}:
            raise homebrew_harness.runtime.errors.AgentError(
                "Model output was incomplete or requires provider continuation; no tools were executed."
            )
        calls = response.tool_calls
        if stop_event is not None and stop_event.is_set():
            raise homebrew_harness.runtime.errors.AgentError(
                "Agent turn stopped before executing model actions."
            )
        if first_response and on_first_response is not None:
            # Persist receipt before any side effect in this model response.
            on_first_response()
        first_response = False

        assistant = {"role": "assistant", "content": redact(response.text)}
        if calls:
            assistant["tool_calls"] = [call.history() for call in calls]
        if response.replay:
            assistant["_protocol_state"] = response.replay
        messages.append(assistant)
        save_boundary(messages, todo_manager, compactor.intent)

        if team_member is not None and not calls:
            team_member.before_model()
        superseded = (
            steering is not None
            and steering.superseded()
            or proposal is not None
            and proposal()
            or getattr(team_member, "user_revision", 0) != member_revision
        )
        if not calls and superseded:
            if steering is not None:
                steering.apply(messages, todo_manager, compactor)
            continue
        if not calls and guidance.refresh(messages, guidance_cwd()):
            continue
        if not calls:
            if steering is not None and steering.apply(
                messages, todo_manager, compactor
            ):
                continue
            # A command may finish while the model is producing a final reply.
            # Deliver that observation before allowing Stop to end the turn.
            if (
                collect_background
                and background_manager is not None
                and inject_background_results(
                    messages, background_manager, goal_controller
                )
            ):
                continue
            continuation = hooks.trigger_hooks("Stop", messages)
            client.checkpoint()
            if continuation is not None:
                stop_blocks += 1
                if stop_blocks > stop_block_cap:
                    if goal_controller is not None and goal_controller.pending:
                        return goal_controller.report(
                            response.text or "",
                            goal_controller.suspend(
                                "limit",
                                "Stop hook continuation cap reached; goal retained.",
                            ),
                        )
                    raise homebrew_harness.runtime.errors.AgentError(
                        "Stop hook continuation cap reached; inspect unfinished work before continuing."
                    )
                messages.append({"role": "user", "content": continuation})
                continue
            if goal_controller is not None:
                pending = []
                if goal_controller.pending:
                    pending = (
                        goal_controller.pending_work()
                        if goal_controller.pending_work
                        else (
                            ["background"]
                            if background_manager is not None
                            and (
                                background_manager.running_paths()
                                or background_manager.has_ready()
                            )
                            else []
                        )
                    )
                committed, decision = commit_stop(
                    response.text or "", proposal, pending
                )
                if not committed:
                    continue
                if decision.action == "block":
                    stop_blocks += 1
                    if stop_blocks > stop_block_cap:
                        return goal_controller.report(
                            response.text or "",
                            goal_controller.suspend(
                                "limit",
                                "Stop hook continuation cap reached; goal retained.",
                            ),
                        )
                    messages.append(
                        {
                            "role": "user",
                            "name": "goal_feedback",
                            "content": goal_controller.feedback(decision),
                        }
                    )
                    continue
                return goal_controller.report(response.text or "", decision)
            committed, _ = commit_stop(response.text or "", proposal)
            if not committed:
                continue
            return response.text or ""

        revision_before = todo_manager.revision if todo_manager is not None else None
        batch_unknown = False
        guidance_stale = guidance.refresh(messages, guidance_cwd())
        for call in calls:
            from homebrew_harness.runtime.conversation_commands import active_evidence
            if active_evidence() is not None:
                active_evidence().before(call, guidance_cwd())
            guidance_stale = guidance_stale or guidance.refresh(messages, guidance_cwd())
            try:
                client.checkpoint()
                control_error = None
            except ModelGatewayError as error:
                control_error = error
            if (
                components.run_control is not None
                and components.run_control.permission_required
            ):
                output = json.dumps(
                    {
                        "error": "Earlier call needs host authorization; this call was not executed.",
                        "executed": False,
                    }
                )
            elif superseded or proposal is not None and proposal():
                superseded = True
                output = json.dumps(
                    {
                        "error": "Superseded by user steering; this tool was not executed.",
                        "executed": False,
                    }
                )
            elif batch_unknown:
                output = json.dumps(
                    {
                        "error": "Earlier action outcome unknown; this call was not executed.",
                        "executed": False,
                    }
                )
            elif guidance_stale:
                output = json.dumps({"error": "Project guidance changed; this proposal was not executed. Reconsider with the current conventions.", "executed": False})
            elif control_error is not None:
                output = json.dumps(
                    {
                        "error": "Tool call not executed: " + str(control_error),
                        "executed": False,
                    }
                )
            elif stop_event is not None and stop_event.is_set():
                output = json.dumps(
                    {"error": "Session closing; this tool call was not executed."}
                )
            else:
                with (
                    pool.scope(),
                    activity("tool", name=call.name, workdir=workdir) as activity_cancel,
                    client.scope(cancel_event=activity_cancel),
                    client.scope(cancel_event=steering.cancel if steering else None),
                ):
                    if steering is not None:
                        steering.phase = "tool"
                    output = homebrew_harness.tools.dispatch.execute_tool(
                        call,
                        workdir,
                        hooks=hooks,
                        handlers=pool.handlers,
                        background_manager=background_manager,
                        team_member=team_member,
                        team_manager=team_manager,
                        mcp_runtime=mcp_runtime,
                        mcp_bindings=mcp_bindings,
                        archive_store=compactor.store,
                        restrict_archives=subagent or team_member is not None,
                        workflow_runtime=workflow_runtime,
                        goal_controller=goal_observer or goal_controller,
                        test_runner=test_runner,
                        artifact_store=components.artifact_store,
                        before_start=proposal,
                    )
            from homebrew_harness.runtime.conversation_commands import active_evidence
            observer = components.plan_observer or (active_evidence().observe if active_evidence() else None)
            if observer is not None:
                observer(call, guidance_cwd(), output)
            tool_message = {
                "role": "tool",
                "tool_call_id": call.id,
                "content": output,
            }
            writer = active_writer()
            cached = writer.tool_messages.get(call.id) if writer else None
            messages.append(cached if cached == tool_message else tool_message)
            from homebrew_harness.execution.execution_control import cleanup_scope

            with cleanup_scope():
                save_boundary(messages, todo_manager, compactor.intent)
            if writer:
                writer.tool_messages.pop(call.id, None)
            try:
                batch_unknown |= needs_inspection(output)
            except (ValueError, AttributeError):
                pass
        changed_input = (
            steering.apply(messages, todo_manager, compactor)
            if steering is not None
            else False
        )
        if team_member is not None:
            team_member.before_model()
            changed_input = team_member.user_revision != member_revision
        if batch_unknown:
            raise RecoveryRequired(
                "Tool outcome unknown; inspect saved execution facts before continuing. Session hosts can use /recovery."
            )
        if (
            components.run_control is not None
            and components.run_control.permission_required
        ):
            from homebrew_harness.runs.spec import RunStopped

            raise RunStopped(
                getattr(
                    components.run_control, "permission_reason", "approval_required"
                )
            )
        try:
            client.checkpoint()
        except ModelGatewayError as error:
            control_error = error
        if control_error is not None:
            if isinstance(control_error, ModelCancelled):
                raise homebrew_harness.runtime.errors.AgentError(
                    "Agent turn stopped after closing its tool batch."
                ) from control_error
            raise control_error
        if stop_event is not None and stop_event.is_set():
            raise homebrew_harness.runtime.errors.AgentError(
                "Agent turn stopped after closing its tool batch."
            )
        if todo_manager is not None:
            updated_todo = todo_manager.revision != revision_before
            rounds_since_todo = 0 if updated_todo else rounds_since_todo + 1
            if rounds_since_todo >= REMINDER_INTERVAL:
                # OpenAI tool results are separate messages. Add the reminder only
                # after the whole batch is closed, without inventing a tool-call ID.
                messages.append({"role": "user", "content": TODO_REMINDER})
                rounds_since_todo = 0
        if compactor.compact_requested:
            compactor.compact_requested = False
            plan = todo_manager.render() if todo_manager is not None else ""
            messages[:] = compactor.compact_history(messages, active_request, plan)
        if changed_input:
            continue
        if (
            team_member is not None
            and getattr(team_member.manager, "managed", None)
            and getattr(team_member, "test_request", None)
        ):
            return "Waiting for the recorded fixed-test authorization decision."
        if (
            team_member is not None
            and getattr(team_member.manager, "managed", None)
            and team_member.assignment is not None
            and team_member.store.load(team_member.assignment.id).status == "completed"
        ):
            return ""
        if (
            goal_controller is not None
            and goal_controller.enabled
            and goal_controller.state.get("spec")
            and not goal_controller.state["spec"]["judge"]
        ):
            if any(
                call.name
                in {
                    "run_tests",
                    "write_file",
                    "edit_file",
                    "complete_task",
                    "publish_stage",
                }
                for call in calls
            ):
                pending = (
                    goal_controller.pending_work()
                    if goal_controller.pending_work
                    else []
                )
                if (
                    not pending
                    and goal_controller.hard_verifier.check(
                        goal_controller.state["spec"],
                        since=goal_controller.verification_since,
                    )["ok"]
                ):
                    committed, decision = commit_stop("", proposal)
                    if not committed:
                        continue
                    return goal_controller.report("", decision)
        if team_member is not None and team_member.gate == "pending":
            return ""  # Pause after closing the batch and its compaction request.
