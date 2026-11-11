"""assembly for the harness."""

from __future__ import annotations

from homebrew_harness.runtime.output import print

import sys
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path

import homebrew_harness.runtime.errors
import homebrew_harness.runtime.hooks
import homebrew_harness.tools.registry
import homebrew_harness.tools.shell
from homebrew_harness.context.compaction import ContextCompactor
from homebrew_harness.context.skill_loader import SkillLoader
from homebrew_harness.context.todo import TodoManager
from homebrew_harness.execution.background import BackgroundManager, ShellRunner
from homebrew_harness.execution.bash_policy import (
    bash_policy_scope,
    current_bash_policy,
)
from homebrew_harness.execution.bash_runner import (
    make_shell_runner,
    validate_bash_resources,
)
from homebrew_harness.execution.model_gateway import ensure_gateway
from homebrew_harness.execution.test_runner import TestRunner
from homebrew_harness.orchestration.cron_scheduler import CronScheduler
from homebrew_harness.orchestration.goal_loop import GoalError
from homebrew_harness.orchestration.task_store import TaskStore
from homebrew_harness.runtime.capabilities import capability_scope
from homebrew_harness.runtime.capabilities import current as current_capabilities
from homebrew_harness.runtime.loop_components import LoopComponents
from homebrew_harness.storage.durable_runtime import active_writer, save_boundary
from homebrew_harness.tools.tool_pool import ToolPool, intersect_access


@dataclass(frozen=True)
class _PreparedLoop:
    """One invocation's actual dependencies and first effective tool snapshot."""

    client: object
    model: str
    workdir: Path
    components: LoopComponents
    pool: ToolPool

    def run(
        self,
        messages,
        max_turns=20,
        *,
        active_request=None,
        on_first_response=None,
        collect_background=True,
        goal_controller=None,
        stop_block_cap=8,
        goal_observer=None,
    ):
        if type(stop_block_cap) is not int or not 1 <= stop_block_cap <= 100:
            raise GoalError("Stop continuation cap must be between 1 and 100.")
        if self.components.subagent or self.components.team_member is not None:
            goal_controller = goal_observer = None
        import homebrew_harness.runtime.loop

        try:
            controller = self.components.steering
            context = (
                controller.turn()
                if controller and controller.active_id is None
                else nullcontext()
            )
            with context:
                return homebrew_harness.runtime.loop._agent_loop(
                    self.client,
                    self.model,
                    messages,
                    self.workdir,
                    max_turns,
                    components=self.components,
                    first_pool=self.pool,
                    active_request=active_request,
                    on_first_response=on_first_response,
                    collect_background=collect_background,
                    goal_controller=goal_controller,
                    stop_block_cap=stop_block_cap,
                    goal_observer=goal_observer,
                )
        except BaseException:
            from homebrew_harness.execution.execution_control import cleanup_scope

            try:
                with cleanup_scope():
                    writer = active_writer()
                    operations = (
                        list(
                            writer.store.items("operations", owner=writer.key).values()
                        )
                        if writer
                        else []
                    )
                    if any(
                        m.get("role") == "assistant" and m.get("tool_calls")
                        for m in messages
                    ):
                        from homebrew_harness.storage.durable_runtime import (
                            repair_history,
                        )

                        repaired, _ = repair_history(messages, operations)
                        messages[:] = repaired
                        if writer:
                            save_boundary(
                                messages,
                                self.components.todo_manager,
                                self.components.compactor.intent,
                            )
            except Exception:
                pass  # Preserve the execution failure after attempting protocol repair.
            raise


def prepare_context(client, model, workdir, components: LoopComponents):
    """Create missing private context objects at their owning context boundary."""
    hooks = (
        components.hooks
        if components.hooks is not None
        else homebrew_harness.runtime.hooks.create_hooks()
    )
    skills = (
        components.skill_loader
        if components.skill_loader is not None
        else SkillLoader(workdir / "skills", include_builtin=True)
    )
    compactor = components.compactor
    if compactor is None:
        compactor = ContextCompactor(
            client,
            model,
            workdir,
            context_chars=components.context_chars,
            archive_namespace=components.archive_namespace,
        )
    compactor.worker_output_tokens = components.worker_output_tokens
    todo = None if components.subagent else components.todo_manager
    if not components.subagent and todo is None:
        todo = getattr(components.team_member, "todo_manager", None)
        if todo is None:
            todo = TodoManager()
        if components.team_member is not None:
            components.team_member.todo_manager = todo
    return replace(
        components,
        hooks=hooks,
        skill_loader=skills,
        compactor=compactor,
        todo_manager=todo,
    )


def prepare_core(client, model, workdir, components: LoopComponents, owned):
    """Resolve core defaults once; callers retain the lifetime of created resources."""
    components = prepare_context(client, model, workdir, components)
    tests = components.test_runner
    if tests is None:
        host = (
            components.team_member.manager
            if components.team_member is not None
            else components.team_manager
        )
        tests = getattr(host, "test_runner", None)
        if tests is None:
            tests = TestRunner.default(workdir)
            owned.append(("tests", tests.close))
    return replace(components, test_runner=tests)


@contextmanager
def _prepare_loop(client, model, workdir, components: LoopComponents):
    """Prepare once before recall; release only resources created by this scope."""
    task_store, background_manager = (
        components.task_store,
        components.background_manager,
    )
    cron_scheduler, team_manager, team_member = (
        components.cron_scheduler,
        components.team_manager,
        components.team_member,
    )
    mcp_runtime = components.mcp_runtime
    shell_runner = components.shell_runner
    subagent = components.subagent
    capabilities, tool_allowlist = components.capabilities, components.tool_allowlist
    client = ensure_gateway(client)
    host = team_member.manager if team_member is not None else team_manager
    selected = (
        capabilities or getattr(host, "capabilities", None) or current_capabilities()
    )
    if mcp_runtime is not None:
        selected = selected.include("mcp")
    tool_allowlist = intersect_access(tool_allowlist)
    policy = (
        components.bash_policy
        or getattr(host, "bash_policy", None)
        or getattr(shell_runner, "bash_policy", None)
        or current_bash_policy()
    )
    owned, cleanup_errors = [], []
    failure = None
    with capability_scope(selected), bash_policy_scope(policy):
        try:
            components = prepare_core(client, model, workdir, components, owned)
            compactor = components.compactor
            if shell_runner is None:
                shell_runner = getattr(host, "shell_runner", None)
            artifacts = (
                components.artifact_store
                or getattr(host, "artifact_store", None)
                or getattr(shell_runner, "artifact_store", None)
            )
            if policy.outputs and artifacts is None:
                from homebrew_harness.storage.artifact_store import ArtifactStore

                artifacts = ArtifactStore(
                    workdir, policy, test_runner=components.test_runner
                )
            if shell_runner is None:
                shell_runner = (
                    ShellRunner()
                    if policy.mode == "host"
                    else make_shell_runner(policy, artifacts)
                )
                shell_runner.bash_policy = policy
                owned.append(("shell", shell_runner.close))
            if artifacts is not None:
                if (
                    artifacts.policy.fingerprint() != policy.fingerprint()
                    or not workdir.resolve().is_relative_to(artifacts.root)
                ):
                    raise ValueError(
                        "Artifact store must match the inherited Bash policy and owning workspace."
                    )
                if getattr(shell_runner, "artifact_store", None) is None:
                    shell_runner.artifact_store = artifacts
                elif shell_runner.artifact_store is not artifacts:
                    raise ValueError(
                        "The shared Bash runner requires its owning artifact store."
                    )
            validate_bash_resources(policy, shell_runner)
            if task_store is None:
                task_store = (
                    getattr(team_member, "store", None)
                    if team_member is not None
                    else getattr(host, "store", None)
                )
                if (
                    task_store is None
                    and host is None
                    and selected.has("tasks")
                    and not subagent
                ):
                    task_store = TaskStore(workdir)
                    owned.append(("tasks", task_store.close))
            if background_manager is None and not subagent and team_member is None:
                background_manager = getattr(host, "background_manager", None)
                if background_manager is None and selected.has("background"):
                    background_manager = BackgroundManager(
                        workdir,
                        homebrew_harness.tools.shell.run_bash,
                        bash_policy=policy,
                        artifact_store=artifacts,
                    )
                    background_manager.allow_async = False
                    owned.append(("background", background_manager.close))
            if artifacts is not None and background_manager is not None:
                if getattr(background_manager.runner, "artifact_store", None) is None:
                    background_manager.runner.artifact_store = artifacts
                elif background_manager.runner.artifact_store is not artifacts:
                    raise ValueError(
                        "Background Bash requires the same owning artifact store."
                    )
            validate_bash_resources(policy, shell_runner, background_manager)
            if (
                background_manager is not None
                and background_manager.run_command
                is homebrew_harness.tools.shell.run_bash
            ):
                background_manager.run_command = partial(
                    homebrew_harness.tools.shell.run_bash, archive_store=compactor.store
                )
            if (
                cron_scheduler is None
                and selected.has("cron")
                and not subagent
                and team_member is None
            ):
                cron_scheduler = CronScheduler(workdir)
            worktree_manager = None
            if (
                host is None
                and task_store is not None
                and selected.has("worktrees")
                and not subagent
            ):
                from homebrew_harness.orchestration.worktrees import WorktreeManager

                worktree_manager = WorktreeManager(workdir, task_store)
            if team_manager is not None and team_member is None:
                team_manager.begin_lead_turn()
                owned.append(("lead_turn", team_manager.end_lead_turn))
            components = replace(
                components,
                task_store=task_store,
                background_manager=background_manager,
                cron_scheduler=cron_scheduler,
                team_manager=team_manager,
                team_member=team_member,
                capabilities=selected,
                tool_allowlist=tool_allowlist,
                shell_runner=shell_runner,
                artifact_store=artifacts,
                bash_policy=policy,
                worktree_manager=worktree_manager,
            )
            pool = homebrew_harness.tools.registry.build_tool_pool(
                client, model, workdir, components
            )
            yield _PreparedLoop(client, model, workdir, components, pool)
        except BaseException as error:
            failure = error
            raise
        finally:
            from homebrew_harness.execution.execution_control import cleanup_scope

            with cleanup_scope():
                for name, close in reversed(owned):
                    try:
                        if (
                            name == "tasks"
                            and hasattr(shell_runner, "after_executions")
                            and not shell_runner.wait_closed(0)
                        ):
                            shell_runner.after_executions(close)
                        else:
                            close()
                    except Exception as error:
                        cleanup_errors.append((name, error))
            if cleanup_errors:
                detail = "Loop resource cleanup failed: " + ", ".join(
                    name for name, _ in cleanup_errors
                )
                if failure is not None:
                    failure.loop_cleanup_errors = tuple(
                        name for name, _ in cleanup_errors
                    )
                    print("[cleanup] " + detail, file=sys.stderr)
                else:
                    raise homebrew_harness.runtime.errors.AgentError(
                        detail
                    ) from cleanup_errors[0][1]
