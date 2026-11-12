"""s17: a serialized Lead, automatic events and one explicit goal gate."""

from __future__ import annotations

from homebrew_harness.runtime.output import print

import copy
import json
import math
import sys
import threading
import time
import uuid
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime
from functools import partial
from pathlib import Path

import homebrew_harness.context.memory
import homebrew_harness.execution.background
import homebrew_harness.orchestration.cron_scheduler
import homebrew_harness.orchestration.teams
import homebrew_harness.orchestration.workflow_runtime
import homebrew_harness.runtime.assembly
import homebrew_harness.runtime.prompts
import homebrew_harness.runtime.turns
import homebrew_harness.tools.registry
import homebrew_harness.tools.shell
from homebrew_harness.context.tracked_history import ConversationHistory
from homebrew_harness.execution.execution_control import (
    acquired,
    cleanup_scope,
    control_scope,
    operation_deadline,
)
from homebrew_harness.execution.execution_control import (
    check as execution_check,
)
from homebrew_harness.execution.model_errors import quota_exhausted
from homebrew_harness.execution.model_gateway import (
    Deadline,
    ModelCancelled,
    ModelDeadlineExceeded,
    ModelGateway,
    UsageLedger,
    gateway_error,
)
from homebrew_harness.execution.test_runner import TestRunner, TestRunnerError
from homebrew_harness.tools.mcp.runtime import MCPRuntime
from homebrew_harness.orchestration.goal_loop import (
    GoalController,
    PromptGoalEvaluator,
)
from homebrew_harness.orchestration.hard_conditions import HardVerifier
from homebrew_harness.orchestration.workspace_runtime import WorkspaceRuntime
from homebrew_harness.runtime.capabilities import Capabilities, capability_scope
from homebrew_harness.runtime.host_commands import HostCommand
from homebrew_harness.runtime.loop_components import LoopComponents
from homebrew_harness.runtime.observability import Observations, RuntimeTrace
from homebrew_harness.runtime.runtime_control import (
    ClosePending,
    CloseProgress,
    Poller,
    Shutdown,
)
from homebrew_harness.storage.durable_runtime import (
    CheckpointWriter,
    RecoveryRequired,
    checkpoint_scope,
    inspected_output,
    operation_active,
    repair_history,
)
from homebrew_harness.tools.tool_outcome import needs_inspection
from homebrew_harness.security.approval_review import requester_scope
from homebrew_harness.security.review_context import owner_scope


class HarnessSession:
    def __init__(
        self,
        client,
        model: str,
        workdir: Path,
        *,
        hooks=None,
        skill_loader=None,
        compactor=None,
        todo_manager=None,
        memory_runtime=None,
        background_manager=None,
        cron_scheduler=None,
        team_manager=None,
        mcp_runtime=None,
        context_chars: int = 50_000,
        max_turns: int = 20,
        clock=datetime.now,
        poll_interval: float = 1,
        dispatch_interval: float = 0.2,
        retry_delay: float = 5,
        workflow_runtime=None,
        goal_controller=None,
        goal_evaluator_model=None,
        goal_check_tokens=2048,
        goal_input_chars=12000,
        stop_block_cap=8,
        mode="interactive",
        shutdown_timeout=5,
        gateway=None,
        gateway_limits=None,
        test_runner=None,
        resume_session=None,
        turn_timeout=300,
        model_profile=None,
        worker_output_tokens=None,
        capabilities=None,
        bash_policy=None,
        model_registry=None,
        lead_model_profile=None,
        model_selection=None,
        approval_review=None,
        conversation_mode=None,
        fork_seed=None,
        run_control=None,
    ):
        created, bindings = [], []
        self.run_control = run_control
        from homebrew_harness.runtime.conversation_commands import initial_state, validate_state
        from homebrew_harness.context.project_guidance import ProjectGuidance
        self.conversation = initial_state()
        if conversation_mode is not None:
            if conversation_mode not in {"normal", "plan"}:
                raise ValueError("Unknown conversation mode.")
            self.conversation["mode"] = conversation_mode
        if resume_session and conversation_mode is None:
            from homebrew_harness.storage.durable_runtime import DurableStore
            state_store = DurableStore(workdir)
            identity = state_store.get_meta("latest_session") if resume_session == "latest" else resume_session
            prior = state_store.get("checkpoints", "lead:" + str(identity), {})
            self.conversation = validate_state(prior.get("conversation", initial_state()))
        self.project_guidance = ProjectGuidance(workdir)
        self.fork_seed = fork_seed
        self.fork_source = None
        self._plan_evidence = None
        missing = object()

        def own(label, value):
            created.append((label, value.close))
            return value

        def bind(value, name, replacement):
            bindings.append((value, name, getattr(value, name, missing)))
            setattr(value, name, replacement)

        try:
            if (
                mode not in {"interactive", "one_shot", "service"}
                or not math.isfinite(shutdown_timeout)
                or shutdown_timeout < 0
            ):
                raise ValueError("Invalid host mode or shutdown timeout.")
            if not math.isfinite(turn_timeout) or turn_timeout <= 0:
                raise ValueError("Turn timeout must be finite positive seconds.")
            from homebrew_harness.execution.bash_policy import current_bash_policy

            self.bash_policy = (
                bash_policy
                or getattr(team_manager, "bash_policy", None)
                or current_bash_policy()
            )
            selected = capabilities or Capabilities.default()
            injected = {
                name
                for name, value in (
                    ("background", background_manager),
                    ("cron", cron_scheduler),
                    ("teams", team_manager),
                    ("workflow", workflow_runtime),
                    ("mcp", mcp_runtime),
                )
                if value is not None
            }
            self.capabilities = selected.include(*injected)
            if memory_runtime is not None and self.capabilities.memory_mode == "off":
                self.capabilities = Capabilities(
                    self.capabilities.enabled,
                    memory_runtime.mode
                    if isinstance(memory_runtime.mode, str)
                    else "recall",
                    self.capabilities.details,
                )
            from homebrew_harness.execution.model_registry import ModelRegistry

            self.models = model_registry or ModelRegistry()
            if resume_session:
                from homebrew_harness.execution.providers.backends import (
                    session_backend_bindings,
                )

                self.models.pin_backends(
                    session_backend_bindings(workdir, resume_session)
                )
            self.models.claim(self)
            self._model_source_client, self._model_source_name = client, model
            self._model_defaults = (context_chars, worker_output_tokens or 4096)
            self._memory_tracks_model = memory_runtime is None
            self._goal_tracks_model = goal_controller is None and goal_evaluator_model is None
            own("models", self.models)
            lead_alias = (
                lead_model_profile
                if lead_model_profile is not None
                else self.models.lead_default
            )
            initial = self.models.select(
                lead_alias,
                client=client,
                model=model,
                context_chars=context_chars,
                max_output_tokens=worker_output_tokens or 4096,
            )
            from homebrew_harness.runtime.model_selection import choose_binding, validate_choice
            self.model_selection = validate_choice(model_selection or {"profile": lead_alias, "model": model if lead_alias is None else None})
            initial = choose_binding(self.models, self._model_source_client, self._model_source_name, self.model_selection,
                context_chars=context_chars, output_tokens=worker_output_tokens or 4096)
            client, model = initial.client, initial.model
            self.gateway = gateway or (
                client
                if isinstance(client, ModelGateway) and gateway_limits is None
                else own(
                    "gateway",
                    ModelGateway(
                        client,
                        gateway_limits,
                        workdir=workdir,
                        ledger=UsageLedger(workdir, enabled=self.capabilities.details),
                    ),
                )
            )
            if worker_output_tokens is not None:
                self.gateway.configure_worker_output(worker_output_tokens)
            self._model_defaults = (context_chars, self.gateway.worker_output_tokens)
            self._selection_base_identity = self.models.select(lead_alias, client=self._model_source_client,
                model=self._model_source_name, context_chars=context_chars,
                max_output_tokens=self.gateway.worker_output_tokens).identity()
            self.lead_binding = choose_binding(self.models, self._model_source_client, self._model_source_name,
                self.model_selection, context_chars=context_chars, output_tokens=self.gateway.worker_output_tokens)
            self._committed_model_selection = dict(self.model_selection)
            self._committed_model_revision = self.model_revision = 0
            client, model = self.lead_binding.client, self.lead_binding.model
            context_chars = self.lead_binding.context_chars
            client = self.gateway.bind(client)
            self.client, self.model = client, model
            self.workdir = workdir.resolve()
            core = homebrew_harness.runtime.assembly.prepare_core(
                client,
                model,
                self.workdir,
                LoopComponents(
                    hooks=hooks,
                    skill_loader=skill_loader,
                    compactor=compactor,
                    todo_manager=todo_manager,
                    test_runner=test_runner,
                    context_chars=context_chars,
                    archive_namespace="lead-" + uuid.uuid4().hex,
                ),
                created,
            )
            self.hooks, self.skills, self.compactor, self.todo, self.tests = (
                core.hooks,
                core.skill_loader,
                core.compactor,
                core.todo_manager,
                core.test_runner,
            )
            self.trace = RuntimeTrace(self.workdir)
            self._observations = Observations()
            self.memory = memory_runtime or (
                homebrew_harness.context.memory.MemoryRuntime(
                    client, model, self.workdir, mode=self.capabilities.memory_mode
                )
                if self.capabilities.memory_mode != "off"
                else None
            )
            if self.memory:
                bind(self.memory, "mode", self.capabilities.memory_mode)
            self.mcp = mcp_runtime
            self.workflow = (
                (
                    workflow_runtime
                    or own(
                        "workflow",
                        homebrew_harness.orchestration.workflow_runtime.WorkflowRuntime(
                            self.workdir,
                            homebrew_harness.orchestration.workflow_runtime.ModelWorkflowRunner(
                                self.lead_binding,
                                gateway=self.gateway,
                                profile=model_profile,
                            ),
                            on_event=lambda kind, **fields: self._emit_trace(
                                "workflow_" + kind, source="workflow", **fields
                            ),
                        ),
                    )
                )
                if self.capabilities.has("workflow")
                else None
            )
            self.background = (
                (
                    background_manager
                    or own(
                        "background",
                        homebrew_harness.execution.background.BackgroundManager(
                            self.workdir,
                            homebrew_harness.tools.shell.run_bash,
                            bash_policy=self.bash_policy,
                        ),
                    )
                )
                if self.capabilities.has("background")
                else None
            )
            if (
                self.background
                and self.background.run_command is homebrew_harness.tools.shell.run_bash
            ):
                bind(
                    self.background,
                    "run_command",
                    partial(
                        homebrew_harness.tools.shell.run_bash,
                        archive_store=self.compactor.store,
                    ),
                )
            self.cron = (
                (
                    cron_scheduler
                    or homebrew_harness.orchestration.cron_scheduler.CronScheduler(
                        self.workdir
                    )
                )
                if self.capabilities.has("cron")
                else None
            )
            self.team = (
                (
                    team_manager
                    or own(
                        "workspace",
                        homebrew_harness.orchestration.teams.TeamManager(
                            self.workdir,
                            partial(
                                homebrew_harness.runtime.turns.run_member_loop,
                                client=client,
                                model=model,
                                hooks=self.hooks,
                                skill_loader=self.skills,
                                context_chars=context_chars,
                                mcp_runtime=mcp_runtime,
                            ),
                            background_manager=self.background,
                            bash_policy=self.bash_policy,
                            worktrees=self.capabilities.has("worktrees"),
                        ),
                    )
                )
                if self.capabilities.has("teams")
                else None
            )
            if self.team and (team_manager is None or self.models.specs):
                bind(
                    self.team,
                    "model_selector",
                    lambda role, alias: self.models.select(
                        alias,
                        client=self.client,
                        model=self.model,
                        role=role,
                        context_chars=self.compactor.context_chars,
                        max_output_tokens=self.lead_binding.max_output_tokens,
                    ),
                )
            self.workspace = self.team or own(
                "workspace",
                WorkspaceRuntime(
                    self.workdir,
                    tasks=self.capabilities.has("tasks"),
                    worktrees=self.capabilities.has("worktrees"),
                    background_manager=self.background,
                    bash_policy=self.bash_policy,
                ),
            )
            if isinstance(self.mcp, MCPRuntime):
                if any(
                    c.owner is not None and c.owner._leased for c in self.mcp._actors
                ):
                    raise ValueError(
                        "Bind the session before connecting container MCP services."
                    )
                bind(self.mcp, "leases", self.workspace.leases)
            self.shell = self.workspace.shell_runner
            from homebrew_harness.storage.artifact_store import ArtifactStore

            self.artifacts = (
                ArtifactStore(
                    self.workdir,
                    self.bash_policy,
                    workspace_resolver=lambda task_id: self.workspace.store.workspace(
                        self.workspace.store.load(task_id)
                    ),
                    test_runner=self.tests,
                    owner=lambda: self.workspace.runtime_session_id,
                )
                if self.bash_policy.outputs
                else None
            )
            if self.artifacts is not None:
                bind(self.workspace, "artifact_store", self.artifacts)
                bind(self.shell, "artifact_store", self.artifacts)
                if self.background:
                    bind(self.background.runner, "artifact_store", self.artifacts)
            from homebrew_harness.execution.bash_runner import validate_bash_resources

            validate_bash_resources(self.bash_policy, self.shell, self.background)
            bind(self.workspace, "capabilities", self.capabilities)
            bind(self.workspace, "session_owner", self)
            bind(self.workspace, "test_runner", self.tests)
            bind(self.workspace, "background_manager", self.background)
            bind(
                self.workspace,
                "model_pending",
                lambda: (
                    self.gateway.inflight > 0
                    or self.tests.active.is_set()
                    or not self.shell.wait_closed(0)
                    or any(
                        component and not component.wait_closed(0)
                        for component in (self.background, self.workflow)
                    )
                ),
            )
            if self.background:
                bind(self.background, "leases", self.workspace.leases)
            self.intent = self.compactor.intent
            bind(self.workspace, "task_intent", self.intent)
            if self.workspace.store:
                bind(self.workspace.store, "intent", self.intent)
            self.turn_timeout = turn_timeout
            self.mode, self.shutdown_timeout = mode, shutdown_timeout
            self._drain_changed = threading.Event()
            self._automatic_failed = False
            self._control_blocked = False
            self._control_limit = False
            self._async_failed = False
            self.wait_timed_out = False
            if self.team:
                runner = self.team.run_member

                def run_member(member):
                    from contextlib import nullcontext

                    with (
                        self.run_control.team_control.scope(member)
                        if self.run_control and self.run_control.team_control
                        else nullcontext(),
                        capability_scope(self.capabilities),
                        self.gateway.scope(
                            role="teammate",
                            context_id=member.name,
                            cancel_event=member.stop_event,
                            deadline=self.run_control.deadline()
                            if self.run_control
                            else None,
                            inherit_deadline=self.run_control is None,
                        ),
                        self.trace.scope("teammate", member.name),
                    ):
                        return runner(member)

                bind(self.team, "run_member", run_member)
            self._goal_changed = threading.Event()
            self._goal_ready = False
            self.last_goal_result = ""
            self.stop_block_cap = stop_block_cap
            self.goal = goal_controller or own(
                "goal",
                GoalController(
                    self.workdir,
                    PromptGoalEvaluator(
                        client,
                        goal_evaluator_model or model,
                        max_tokens=goal_check_tokens,
                    ),
                    block_cap=stop_block_cap,
                    input_chars=goal_input_chars,
                ),
            )
            bind(
                self.goal,
                "hard_verifier",
                HardVerifier(
                    self.workdir,
                    test_runner=self.tests,
                    workspace_resolver=self.resolve_task_workspace,
                ),
            )
            self._goal_observer = self.goal.on_event
            bind(self.goal, "on_event", self._goal_event)
            bind(self.goal, "pending_work", self._goal_pending_work)
            self.messages = ConversationHistory(
                [
                    {
                        "role": "system",
                        "content": homebrew_harness.runtime.prompts.build_system_prompt(
                            self.workdir,
                            skill_loader=self.skills,
                            capabilities=self.capabilities,
                        ),
                    }
                ]
            )
            self.compactor.validate_fixed_budget(
                self.messages, tools=self.tool_pool().tools
            )
            self.user_request = ""
            self.max_turns = max_turns
            self.clock, self.poll_interval, self.dispatch_interval = (
                clock,
                poll_interval,
                dispatch_interval,
            )
            self.retry_delay = retry_delay
            self.lock = threading.Lock()
            self._user_waiting = threading.Event()
            self._input_dispatching = threading.Event()
            self.stop_event = threading.Event()
            self._execution_paused = False
            self._execution_cancel = threading.Event()
            from homebrew_harness.runtime.activities import Activities
            self.activities = Activities()
            self._poller = Poller(self.stop_event)
            self._threads = self._poller.workers.threads
            self._observing = False
            self._pending_team = []
            self._pending_background = []
            self._observation_retry = 0.0
            self._cron_retry = {}
            self._last_source = "cron"
            self._closed = False
            self._shutdown = Shutdown()
            self._close_finished = self._shutdown.finished
            self._close_progress = CloseProgress()
            self._cleanup_errors = self._close_progress.errors
            self._cleanup_history = self._close_progress.history
            self._persistence_error = None
            self._runtime_init_lock = threading.RLock()
            self._quota_blocked = False
            self.runtime_store = self.workspace.bus.store
            self.session_id = uuid.uuid4().hex
            self.checkpoint_key = "lead:" + self.session_id
            self.resume_session = resume_session
            self._runtime_started = False
            from homebrew_harness.runtime.user_input import UserInputController

            self.inputs = UserInputController(self)
            from homebrew_harness.security.approval_review import ApprovalReviewer
            effective_review = run_control.spec.approval_review if run_control is not None and run_control.spec.approval_review is not None else approval_review
            self.approval_reviewer = ApprovalReviewer(effective_review, self.runtime_store, self.models, self.gateway, lambda: self.lead_binding)
            if self.approval_reviewer.config.mode == "auto_review" and self.approval_reviewer.config.model_source == "profile":
                self.models.validate(self.approval_reviewer.config.model_profile)
            self.run_control = run_control
            if run_control is not None:
                run_control.bind(self)
            self._pending_user = []
            self._recovery_blocked = []
            self._checkpoint_writer = CheckpointWriter(
                self.runtime_store,
                self.checkpoint_key,
                lambda: self._snapshot(borrow=True),
            )
            self._checkpoint_writer.run_control = run_control

        except BaseException as failure:
            errors = []
            for value, name, previous in reversed(bindings):
                try:
                    if previous is missing:
                        if hasattr(value, name):
                            delattr(value, name)
                    else:
                        setattr(value, name, previous)
                except Exception:
                    errors.append("binding_" + name)
            with cleanup_scope():
                for label, close in reversed(created):
                    try:
                        close()
                    except Exception:
                        errors.append(label)
            if errors:
                failure.construction_cleanup_errors = tuple(errors)
            raise

    def _snapshot(self, *, borrow=False):
        return {
            "session_id": self.session_id,
            "contract_version": 4,
            "conversation": copy.deepcopy(self.conversation),
            "approval_review": self.approval_reviewer.config.data(),
            "fork_source": self.fork_source,
            **(
                {
                    "managed_run": {
                        **self.run_control.identity(),
                        **self.run_control.header,
                    }
                }
                if self.run_control is not None
                else {}
            ),
            "input_control": self.inputs.data(),
            "api_query": getattr(self, "_api_turn_id", None)
            or getattr(self, "_api_pending_turn_id", None),
            **(
                {"handoff_applied": getattr(self, "_managed_handoff_applied", None)}
                if self.run_control
                else {}
            ),
            "messages": self.messages if borrow else copy.deepcopy(self.messages),
            "host_instance": self.workspace.leases.instance,
            "archives": self.compactor.store.checkpoint(),
            "intent": self.intent.to_dict(),
            "todo": [
                {"content": i.content, "status": i.status} for i in self.todo.items
            ],
            "todo_revision": self.todo.revision,
            "pending_team": copy.deepcopy(self._pending_team),
            "pending_background": copy.deepcopy(self._pending_background),
            "last_result": self.last_goal_result,
            "goal_id": self.goal.state["goal_id"] if self.goal.pending else None,
            "lead_task": self.workspace.lead_assignment.id
            if self.workspace.lead_assignment
            else None,
            "usage": self.gateway.summary(),
            **(
                {"test_handoff": self.tests.handoff_state()}
                if self.run_control is None and isinstance(self.tests, TestRunner)
                else {}
            ),
            "capabilities": self.capabilities.manifest(),
            **(
                {
                    "mcp": self.mcp.checkpoint(
                        workdir=self.workspace.lead_cwd(),
                        task_id=self.workspace.lead_assignment.id
                        if self.workspace.lead_assignment
                        else None,
                    )
                }
                if isinstance(self.mcp, MCPRuntime)
                else {}
            ),
            "bash_policy": self.bash_policy.identity(),
            "model_binding": self.lead_binding.identity(),
            "model_selection": {"base_binding": self._selection_base_identity, "choice": self.model_selection, "revision": self.model_revision},
            "model_registry": self.models.manifest(),
            **(
                {
                    "team": self.team.snapshot_for_storage()
                    if borrow
                    else self.team.checkpoint()
                }
                if self.team
                else {}
            ),
            "recovery_blocked": list(self._recovery_blocked),
            "phase": "idle",
        }

    def loop_components(self):
        from homebrew_harness.runtime.conversation_commands import PLAN_TOOLS
        return LoopComponents(
            hooks=self.hooks,
            todo_manager=self.todo,
            skill_loader=self.skills,
            compactor=self.compactor,
            team_manager=self.workspace,
            task_store=self.workspace.store,
            background_manager=self.background,
            cron_scheduler=self.cron,
            workflow_runtime=self.workflow,
            mcp_runtime=self.mcp,
            test_runner=self.tests,
            capabilities=self.capabilities,
            shell_runner=self.shell,
            artifact_store=self.artifacts,
            bash_policy=self.bash_policy,
            stop_event=getattr(self, "stop_event", None),
            model_registry=self.models,
            worker_output_tokens=self.lead_binding.max_output_tokens,
            steering=getattr(self, "inputs", None),
            on_user_submit=self._ack_user_input
            if getattr(self, "_pending_user", None)
            else None,
            run_control=self.run_control,
            tool_allowlist=self.run_control.spec.effective_tools() if self.run_control else
                PLAN_TOOLS if self.conversation["mode"] == "plan" else None,
            project_guidance=self.project_guidance,
            planning=self.conversation["mode"] == "plan",
            plan_observer=self._plan_evidence.observe if self._plan_evidence else None,
        )

    def tool_pool(self):
        return homebrew_harness.tools.registry.build_tool_pool(
            self.client, self.model, self.workdir, self.loop_components()
        )

    def _ensure_runtime(self, deadline=None):
        with acquired(self._runtime_init_lock, deadline):
            if self._closed:
                raise ValueError("Harness session is closed.")
            try:
                self._initialize_runtime(deadline)
            except BaseException:
                self._runtime_started = False
                with cleanup_scope():
                    try:
                        sid = self.session_id
                        current = self.runtime_store.get("session_meta", sid)
                        if (
                            current
                            and current.get("instance")
                            == self.workspace.leases.instance
                        ):
                            if getattr(self, "_prior_meta", None):
                                self.runtime_store.put(
                                    "session_meta", sid, self._prior_meta
                                )
                            else:
                                self.runtime_store.remove("session_meta", sid)
                            if self.runtime_store.get_meta("latest_session") == sid:
                                self.runtime_store.set_meta(
                                    "latest_session",
                                    getattr(self, "_prior_latest", None),
                                )
                    except Exception as error:
                        self._close_progress.record_error("initialization", error)
                    finally:
                        try:
                            if self.workspace.store:
                                self._critical_close(
                                    "initialization_tasks",
                                    self.workspace.store.close,
                                    repeat=True,
                                )
                        finally:
                            self._critical_close(
                                "initialization_lease",
                                self.workspace.release_session,
                                repeat=True,
                            )
                raise

    def _initialize_runtime(self, deadline=None):
        if self._runtime_started:
            return
        self.workspace.acquire_session(
            (deadline or operation_deadline(self.turn_timeout)).expires_at
        )
        state = self.runtime_store.load_session(self.resume_session)
        self._prior_meta = None
        self._prior_latest = self.runtime_store.get_meta("latest_session")
        if self.resume_session:
            sid = (
                state.get("latest_session")
                if self.resume_session == "latest"
                else self.resume_session
            )
            from homebrew_harness.storage.checkpoint_lifecycle import require_retained

            require_retained(state.get("session_meta", {}).get(sid))
            if state.get("session_meta", {}).get(sid, {}).get("handoff_to"):
                raise RecoveryRequired(
                    "This Session was handed off; continue its target Run instead of resuming the source."
                )
            self._prior_meta = copy.deepcopy(state["session_meta"].get(sid))
            saved = state["checkpoints"].get("lead:" + str(sid))
            if saved is None:
                raise RecoveryRequired(
                    "Session checkpoint not found; choose a saved session ID."
                )
            self.session_id, self.checkpoint_key = sid, "lead:" + sid
            operations = [
                v
                for v in state["operations"].values()
                if v["checkpoint"] == self.checkpoint_key
                or v["checkpoint"].startswith("member:" + sid + ":")
            ]
            if saved.get("contract_version") != 4:
                raise RecoveryRequired(
                    "Checkpoint requires the current protocol/input contract; start a new session for prerelease formats."
                )
            if saved.get("bash_policy") != self.bash_policy.identity():
                raise RecoveryRequired(
                    "Resume requires the saved Bash execution policy; a sandbox cannot silently become host execution."
                )
            from homebrew_harness.security.approval_review import ReviewConfig
            if ReviewConfig.parse(saved.get("approval_review")).data() != self.approval_reviewer.config.data():
                raise RecoveryRequired("Resume requires the saved approval reviewer policy.")
            selection = saved.get("model_selection")
            if selection:
                if saved.get("model_registry") != self.models.manifest():
                    raise RecoveryRequired("Resume requires the original model catalog.")
                if saved.get("model_binding") != self.lead_binding.identity():
                    if selection.get("base_binding") != self._selection_base_identity:
                        raise RecoveryRequired("Saved model choice belongs to a different original binding.")
                    from homebrew_harness.runtime.session_commands import select
                    select(self, selection["choice"], restore=True)
                self._selection_base_identity = selection["base_binding"]
                self.model_selection = selection["choice"]
                self._committed_model_selection = dict(self.model_selection)
                self._committed_model_revision = self.model_revision = selection.get("revision", 0)
            if (
                saved.get("model_binding") != self.lead_binding.identity()
                or saved.get("model_registry") != self.models.manifest()
            ):
                raise RecoveryRequired(
                    "Resume requires the saved model binding and host model registry; start a new session for changed models."
                )
            if saved.get("capabilities") != self.capabilities.manifest():
                raise RecoveryRequired(
                    "Resume requires the saved host capability configuration; choose the matching profile."
                )
            restored, uncertain = repair_history(saved["messages"], operations)
            if self.run_control is not None:
                uncertain = self.run_control.recovered(
                    saved, operations, restored, uncertain
                )
            from homebrew_harness.runtime.conversation_commands import validate_state, initial_state
            self.conversation = validate_state(saved.get("conversation", initial_state()))
            self.fork_source = saved.get("fork_source")
            self.messages[:] = restored
            homebrew_harness.runtime.turns.set_memory_context(self.messages, "")
            if self.run_control is None and saved.get("test_handoff"):
                self.tests.restore_handoff_state(saved["test_handoff"])
            if saved.get("archives"):
                self.compactor.store.restore_checkpoint(saved["archives"])
            self.intent.restore(saved["intent"])
            self.inputs.restore(saved["input_control"])
            self._managed_handoff_applied = saved.get("handoff_applied")
            self.user_request = self.intent.original
            self.todo.update(saved.get("todo", []))
            self.todo._revision = saved.get("todo_revision", self.todo.revision)
            self.last_goal_result = saved.get("last_result", "")
            query_id = saved.get("api_query")
            query = self.runtime_store.get("api_turns", query_id) if query_id else None
            if (
                query
                and query["state"] == "running"
                and saved.get("phase") in {"executed", "idle"}
            ):
                self._api_pending_turn_id = query_id
                self.last_goal_result = query.get("output") or self.last_goal_result
            self._pending_background = saved.get("pending_background", [])
            self._recovery_blocked = list(
                dict.fromkeys(
                    [
                        *saved.get("recovery_blocked", []),
                        *uncertain,
                        *[
                            v["call_id"]
                            for v in operations
                            if v["state"] in {"started", "unknown"}
                        ],
                    ]
                )
            )
            if (
                saved.get("phase") == "running"
                and not self._recovery_blocked
                and self.run_control is None
            ):
                self._recovery_blocked.append("interrupted_turn")
            if saved.get("lead_task"):
                record = self.workspace.store.load(saved["lead_task"])
                if record.status == "in_progress":
                    self.workspace.lead_assignment = self.workspace.store.resume(
                        record.id
                    )
            if saved.get("goal_id"):
                self.goal.restore(
                    saved["goal_id"],
                    reactivate=False,
                    reset_episode=self.run_control is None,
                )
            if self.team:
                self.team.restore_checkpoint(sid)
            # A saved model call may have been billed after the old host died.
            self.gateway.restore_usage(saved.get("usage", {}))
            if saved.get("phase") == "running":
                self.gateway.mark_observation_incomplete()
            self.runtime_store.start_session(sid, self.workspace.leases.instance)
        if not self.resume_session and self.fork_seed:
            from homebrew_harness.runtime.conversation_commands import seed_fork
            seed_fork(self, self.fork_seed)
        self.project_guidance.refresh(self.messages, self.workspace.lead_cwd())
        if not self.resume_session:
            self.runtime_store.start_session(
                self.session_id, self.workspace.leases.instance
            )
        self._checkpoint_writer = CheckpointWriter(
            self.runtime_store, self.checkpoint_key, lambda: self._snapshot(borrow=True)
        )
        self._checkpoint_writer.run_control = self.run_control
        self.workspace.runtime_session_id = self.session_id
        self.workspace.bus.recover("lead")
        self._pending_team.clear()  # Durable claims reappear once, after the lease is acquired.
        if self.cron:
            self.cron.reconcile()
        self._recovery_blocked.extend((self.cron.unresolved() if self.cron else []))
        if self.background:
            self.background.recover()
        unknown = [
            t["bg_id"]
            for t in (self.background.snapshot() if self.background else [])
            if t["status"] == "unknown" and not t.get("inspected")
        ]
        self._recovery_blocked.extend(
            x for x in unknown if x not in self._recovery_blocked
        )
        self._execution_paused = bool(self.runtime_store.get("session_meta", self.session_id, {}).get("execution_paused"))
        if self._execution_paused:
            self._execution_cancel.set()
        self._runtime_started = True
        self.runtime_store.set_meta("latest_session", self.session_id)
        self._save_checkpoint()

    def _save_checkpoint(self, phase="idle", *, ack=()):
        if self._runtime_started:
            operations = self.runtime_store.items(
                "operations", owner=self.checkpoint_key
            )
            self._recovery_blocked.extend(
                v["call_id"]
                for v in operations.values()
                if v["checkpoint"] == self.checkpoint_key
                and v["state"] in {"started", "unknown"}
                and v["call_id"] not in self._recovery_blocked
            )
            for message in self.messages:
                if message.get("role") == "tool":
                    try:
                        unknown = needs_inspection(message.get("content", "{}"))
                    except (ValueError, AttributeError):
                        unknown = False
                    if (
                        unknown
                        and message["tool_call_id"] not in self._recovery_blocked
                    ):
                        self._recovery_blocked.append(message["tool_call_id"])
            expected = self.runtime_store.checkpoint_revision(self.checkpoint_key)
            snapshot = self._snapshot(borrow=True)
            snapshot["phase"] = phase
            boundary = (
                self.run_control.boundary(snapshot, phase, None)
                if self.run_control
                else None
            )
            try:
                with cleanup_scope():
                    self.runtime_store.checkpoint(
                        self.checkpoint_key,
                        snapshot,
                        ack=ack,
                        expected_revision=expected,
                        boundary=boundary,
                    )
            except Exception as error:
                self._persistence_error = type(error).__name__
                self._automatic_failed = True
                raise
            else:
                self._persistence_error = None

    def _refresh_recovery(self):
        for row in self.recovery_operations():
            if not row["active"] and row["call_id"] not in self._recovery_blocked:
                self._recovery_blocked.append(row["call_id"])
        for key in self.cron.unresolved() if self.cron else []:
            if key not in self._recovery_blocked:
                self._recovery_blocked.append(key)
        for item in self.background.snapshot() if self.background else []:
            if (
                item["status"] == "unknown"
                and not item.get("inspected")
                and item["bg_id"] not in self._recovery_blocked
            ):
                self._recovery_blocked.append(item["bg_id"])
        if (
            self._persistence_error
            and "persistence_error" not in self._recovery_blocked
        ):
            self._recovery_blocked.append("persistence_error")

    def recovery_operations(self):
        prefix = (
            "lead:" + self.session_id,
            "member:" + self.session_id + ":",
            "sub:" + self.session_id + ":",
        )
        records = self.runtime_store.items("operations", owner=prefix[0])
        for scope in prefix[1:]:
            records.update(self.runtime_store.items("operations", owner_prefix=scope))
        return [
            {
                "operation_id": key,
                "revision": value.get("revision", 0),
                "call_id": value["call_id"],
                "checkpoint": value["checkpoint"],
                "state": value["state"],
                "batch_id": value.get("batch_id"),
                "active": operation_active(self.workdir, key),
            }
            for key, value in records.items()
            if (
                value["checkpoint"] == prefix[0]
                or value["checkpoint"].startswith(prefix[1:])
            )
            and value["state"] in {"started", "unknown"}
        ]

    def inspect_operation(self, identity, revision):
        from homebrew_harness.storage.durable_runtime import _LIVE_LOCK

        with acquired(_LIVE_LOCK):
            candidates = {
                row["operation_id"]: row for row in self.recovery_operations()
            }
            row = candidates.get(identity)
            if row is None or row["revision"] != revision:
                raise RecoveryRequired(
                    "Operation inspection is stale; read /recovery again."
                )
            if row["active"]:
                raise RecoveryRequired(
                    "Operation is still executing; cancel or wait for its result before inspection."
                )
            snapshot = self._snapshot()
            target = (
                self.runtime_store.get("checkpoints", row["checkpoint"])
                if row["checkpoint"] != self.checkpoint_key
                else snapshot
            )
            if not isinstance(target, dict):
                raise RecoveryRequired(
                    "Operation checkpoint is missing; inspect the stored execution facts."
                )
            original = copy.deepcopy(target)
            confirmed = row["call_id"]
            snapshot["recovery_blocked"] = [
                item for item in snapshot["recovery_blocked"] if item != confirmed
            ]

            def update(state):
                current = state["operations"].get(identity)
                if (
                    current is None
                    or current.get("revision", 0) != revision
                    or current["state"] not in {"started", "unknown"}
                ):
                    raise RecoveryRequired("Operation changed during inspection.")
                current.update(
                    state="inspected", revision=revision + 1, host_inspected=True
                )
                operations = [
                    value
                    for value in state["operations"].values()
                    if value["checkpoint"] == row["checkpoint"]
                ]
                for effect in target.get("effects", []):
                    if effect.get("operation_id") == identity:
                        effect["state"] = "inspected"
                target["messages"], _ = repair_history(
                    target.get("messages", []), operations
                )
                if row["checkpoint"] != self.checkpoint_key:
                    if state["checkpoints"].get(row["checkpoint"]) != original:
                        raise RecoveryRequired(
                            "Target checkpoint changed during inspection."
                        )
                    state["checkpoints"][row["checkpoint"]] = target
                state["checkpoints"][self.checkpoint_key] = snapshot

            with cleanup_scope():
                self.runtime_store.transaction(update)
            for effect in self._checkpoint_writer.effects:
                if effect.get("operation_id") == identity:
                    effect["state"] = "inspected"
            self.messages[:] = snapshot["messages"]
            self._recovery_blocked = snapshot["recovery_blocked"]
            self._refresh_recovery()
            return (
                "Host inspection recorded for "
                + identity
                + ". Original execution facts were retained."
            )

    def recovery_status(self):
        self._refresh_recovery()
        return {
            "session_id": self.session_id,
            "blocked": list(self._recovery_blocked),
            "operations": self.recovery_operations(),
            "background": [
                {
                    "operation_id": b["bg_id"],
                    "revision": b.get("revision", 0),
                    "state": b["status"],
                }
                for b in (self.background.snapshot() if self.background else [])
                if b["status"] == "unknown" and not b.get("inspected")
            ],
            "cron": [
                {
                    "operation_id": k,
                    "revision": v.get("revision", 0),
                    "state": v["state"],
                }
                for k, v in (
                    self.cron.runtime.items("cron").items() if self.cron else []
                )
                if v["state"] in {"received", "unknown"}
            ],
            "recovered_members": list(self.team.recovered_members) if self.team else [],
            "subagents": [
                {
                    "run_id": v.get("run_id"),
                    "phase": v.get("phase"),
                    "effects": v.get("effects", []),
                }
                for k, v in self.runtime_store.items(
                    "checkpoints", prefix="sub:" + self.session_id + ":"
                ).items()
            ],
            "persistence_error": self._persistence_error,
            "shutdown": self.shutdown_status(),
            "instruction": "Inspect current files, task state and external results before /recovery continue. Tools are never automatically replayed.",
        }

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def resolve_task_workspace(self, identity):
        if self.workspace.store is None:
            raise ValueError("Enable the tasks capability for task predicates.")
        return self.workspace.store.workspace(self.workspace.store.load(identity))

    @property
    def members(self):
        return self.team.members if self.team else {}

    def start(self, *, include_clock=True, deadline=None):
        if self.run_control is not None:
            raise ValueError("Managed run scheduling belongs to RunController.")
        if self._closed:
            raise ValueError("Harness session is closed.")
        self._ensure_runtime(Deadline(deadline) if deadline is not None else None)
        jobs = [(self.process_once, self.dispatch_interval, "harness-events")]
        if include_clock and self.cron:
            jobs.append(
                (
                    lambda: self.cron.poll_due(self.clock()) if self.conversation["mode"] != "plan" else None,
                    self.poll_interval,
                    "harness-clock",
                )
            )
        try:
            self._poller.start(
                jobs,
                lambda error: self._emit_trace(
                    "dispatch_error", error_type=type(error).__name__
                ),
            )
        except BaseException:
            self.close()
            raise

    def _goal_event(self, kind, **fields):
        self._emit_trace(kind, source="goal", **fields)
        self._goal_changed.set()
        if self._goal_observer:
            self._observations.run(
                "goal_callback", lambda: self._goal_observer(kind, **fields)
            )

    def _goal_pending_work(self):
        # The current Lead task and this Goal are the work being verified.
        return [
            item
            for item in self.pending_work(for_goal=True)
            if item not in {"goal", "lead_task"}
        ]

    def _wait_lock(self, deadline):
        if deadline is None:
            self.lock.acquire()
            return True
        return self.lock.acquire(timeout=max(0, deadline - time.monotonic()))

    def wait_for_goal(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            self._goal_changed.clear()
            if not self._wait_lock(deadline):
                self.wait_timed_out = True
                return "Host wait timeout reached; work is pending and the goal is retained."
            try:
                if not self.goal.enabled or self.stop_event.is_set():
                    return self.last_goal_result
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    self.wait_timed_out = True
                    return self.goal.report(
                        "",
                        self.goal.suspend(
                            "limit", "Host wait timeout reached; goal retained."
                        ),
                    )
            finally:
                self.lock.release()
            self._goal_changed.wait(remaining)

    def pending_work(self, deadline=None, *, for_goal=False, include_dispatch=True):
        pending = ["recovery"] if self._recovery_blocked else []
        if (
            include_dispatch and not for_goal and (self._input_dispatching.is_set() or self._pending_user or self._user_waiting.is_set())
        ) or (
            self.inputs.pending(active_only=True)
            if for_goal
            else self.runtime_store.mailbox("peek", self.inputs.recipient)
            or self.inputs.active_id is not None
        ):
            pending.append("user_input")
        if self.tests.active.is_set():
            pending.append("tests")
        if self.workflow is not None and self.workflow.pending():
            pending.append("workflow")
        if (
            (self.background.running_paths() if self.background else [])
            or (self.background is not None and self.background.has_ready())
            or self._pending_background
            and not (for_goal and self._observing)
        ):
            pending.append("background")
        acquired = (
            self.workspace.lock.acquire(blocking=False)
            if deadline is None
            else self.workspace.lock.acquire(
                timeout=max(0, deadline - time.monotonic())
            )
        )
        if not acquired:
            return [*pending, "team_busy"]
        try:
            if any(
                member.status in {"working", "stopping", "waiting_approval"}
                or member.assignment is not None
                for member in self.members.values()
                if member.status not in {"stopped", "failed"}
            ):
                pending.append("team")
        finally:
            self.workspace.lock.release()
        queued = (
            self.workspace.bus.available("lead")
            if for_goal and self._observing
            else self.workspace.bus.peek("lead")
        )
        if queued or self._pending_team and not (for_goal and self._observing):
            pending.append("team_events")
        if (
            self._goal_ready
            and self.goal.enabled
            or self.goal.enabled
            and self.goal.state["status"] == "waiting"
        ):
            pending.append("goal")
        if (self.cron is not None and self.cron.has_pending()) or (
            self.cron.unresolved() if self.cron else []
        ):
            pending.append("cron")
        if (self.workspace.store.owned() if self.workspace.store else None) is not None:
            pending.append("lead_task")
        return pending

    def drain(self, timeout=120):
        """Observe accepted work before one-shot exit: 0 complete, 1 failed, 2 pending."""
        deadline = time.monotonic() + max(0, timeout)
        try:
            self.start(include_clock=False, deadline=deadline)
        except ValueError:
            if time.monotonic() >= deadline:
                self.wait_timed_out = True
                return 2
            raise
        while True:
            self._drain_changed.clear()
            if not self._wait_lock(deadline):
                self.wait_timed_out = True
                return 2
            try:
                pending = self.pending_work(deadline)
                failed = self._async_failed or any(
                    member.status == "failed" for member in self.members.values()
                )
                if self._recovery_blocked:
                    return 2
                if pending and (
                    self._quota_blocked
                    or self._automatic_failed
                    or self._control_blocked
                    or self.gateway.blocked
                ):
                    reason = self.gateway.blocked
                    return 2 if self._control_limit or "deadline" in reason else 1
                if pending and set(pending) <= {"lead_task"}:
                    return 2
                if not pending:
                    return (
                        1
                        if failed or self.goal.status()["status"] == "failed"
                        else 2
                        if self.goal.pending
                        else 0
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.wait_timed_out = True
                    return 2
            finally:
                self.lock.release()
            # Also wake periodically for workers with no mailbox event yet.
            self._drain_changed.wait(min(remaining, self.dispatch_interval))

    def exit_report(self, status):
        return {
            "exit_status": status,
            "background": [
                {"bg_id": item["bg_id"], "status": item["status"]}
                for item in (self.background.snapshot() if self.background else [])
            ],
            "tasks": [
                {"task_id": task.id, "status": task.status}
                for task in (
                    self.workspace.store.list() if self.workspace.store else []
                )
            ],
            "goal": self.goal.status()["status"],
            "model_usage": self.gateway.summary(),
        }

    def submit_user_input(
        self,
        text,
        *,
        mode="steer",
        target="lead",
        expected_turn_id=None,
        input_id=None,
        literal=False,
        sdk_turn=False,
        request_id=None,
        goal=None,
        target_input_id=None,
    ):
        if self.run_control is not None:
            return self.inputs.submit(
                text,
                mode=mode,
                target=target,
                expected_turn_id=expected_turn_id,
                input_id=input_id,
            )
        if not self._runtime_started:
            self._ensure_runtime()
        receipt = self.inputs.submit(
            text,
            mode=mode,
            target=target,
            expected_turn_id=expected_turn_id,
            input_id=input_id,
            literal=literal,
            sdk_turn=sdk_turn,
            request_id=request_id,
            goal=goal,
            target_input_id=target_input_id,
        )
        if not self._poller.started and self.inputs.active_id is None:
            self.start(include_clock=False)
        return receipt

    def input_status(self):
        return self.inputs.status()

    def _ack_user_input(self, messages, components):
        self.inputs.commit_records(
            self._pending_user,
            messages,
            components.todo_manager,
            components.compactor,
            append=False,
        )
        self._pending_user = []

    def _consume_user_input(self):
        self._input_dispatching.set()
        try:
            return self._dispatch_user_input()
        finally:
            self._input_dispatching.clear()
            self._drain_changed.set()

    def _dispatch_user_input(self):
        if getattr(self, "_api_pending_turn_id", None):
            if self._api_pending_work():
                return False
            self._finish_api_pending()
        if (
            self.inputs.active_id
            or self._user_waiting.is_set()
            or not self.inputs.pending()
        ):
            return False
        if self._execution_paused and not any(
            not row.get("metadata", {}).get("literal") and HostCommand.parse(row["content"]).local
            for row in self.inputs.pending()
        ):
            return False
        values = self.runtime_store.mailbox(
            "claim", self.inputs.recipient, owner=self.inputs.consumer
        )
        if not values:
            return False
        if self._recovery_blocked or self._execution_paused:
            controls = [
                r
                for r in values
                if not r.get("metadata", {}).get("literal")
                and (HostCommand.parse(r["content"]).allowed_during_recovery if self._recovery_blocked else HostCommand.parse(r["content"]).local)
            ]
            if not controls:
                self.inputs.release(values)
                return False
            record = controls[0]
            rest = [r for r in values if r is not record]
        else:
            record, rest = values[0], values[1:]
        if (
            record.get("from") != "user"
            or record.get("type") != "user_input"
            or record.get("metadata", {}).get("origin") != "host_user"
        ):
            self.runtime_store.mailbox(
                "ack",
                self.inputs.recipient,
                owner=self.inputs.consumer,
                records=[record],
            )
            if rest:
                self.inputs.release(rest)
            raise ValueError("Untrusted inbox data cannot become a user request.")
        if rest:
            self.inputs.release(rest)
        literal = record.get("metadata", {}).get("literal", False)
        command = (
            HostCommand(record["content"])
            if literal
            else HostCommand.parse(record["content"])
        )
        if self._recovery_blocked and not command.allowed_during_recovery:
            self.inputs.release([record])
            return False
        receipt = self.runtime_store.get("input_receipts", record["id"], {})
        if receipt.get("next_retry", 0) and receipt["next_retry"] > time.time():
            self.inputs.release([record])
            return False
        self._pending_user = [record]
        self._api_turn_id = record.get("metadata", {}).get("sdk_turn")
        try:
            result = self.run_user(
                record["content"],
                literal=literal,
                goal_spec=record.get("metadata", {}).get("sdk_goal"),
            )
            if self._pending_user:
                with cleanup_scope():
                    self._checkpoint_writer.save(
                        "idle",
                        ack=[
                            (
                                self.inputs.recipient,
                                self.inputs.consumer,
                                [record["id"]],
                            )
                        ],
                        boundary={
                            "receipts": [
                                {
                                    "input_id": record["id"],
                                    "checkpoint": self.checkpoint_key,
                                    "state": "applied",
                                    "revision": self.inputs.revision,
                                    "target": "lead",
                                    "record": record,
                                }
                            ]
                        },
                    )
                self._pending_user = []
            if self._api_turn_id:
                if self._api_pending_work():
                    self._api_pending_turn_id = self._api_turn_id
                else:
                    self._finish_api_turn(
                        self._api_turn_id,
                        "blocked" if self.goal.pending else "completed",
                        result,
                    )
            print("\n[runtime] user " + record["id"] + "\n" + result, flush=True)
        except Exception as error:
            if isinstance(error, RecoveryRequired) and not self._recovery_blocked:
                self._recovery_blocked.append("user_input_recovery_required")
            if self._pending_user:
                try:
                    self.inputs.fail(self._pending_user, error)
                except Exception:
                    self._recovery_blocked.append("input_persistence_unknown")
                    raise
                finally:
                    self._pending_user = []
            self._automatic_failed = True
            if self._api_turn_id:
                self._finish_api_turn(
                    self._api_turn_id,
                    "blocked" if self._recovery_blocked else "failed",
                    None,
                    str(error),
                )
            print(
                "[runtime] User work stopped (" + type(error).__name__ + ").",
                file=sys.stderr,
            )
        finally:
            self._api_turn_id = None
        self._drain_changed.set()
        return True

    def _finish_api_turn(self, identity, state, output, error=None):
        row = self.runtime_store.get("api_turns", identity)
        if not row or row["state"] in {"completed", "cancelled", "failed"}:
            return
        value = {
            "id": identity,
            "session_id": self.session_id,
            "turn_id": row["turn_id"],
            "state": state,
            "output": output,
            "error": error,
            "input_revision": self.inputs.revision,
        }
        with cleanup_scope():
            self._checkpoint_writer.save("idle", boundary={"api_turn": value})

    def _finish_api_pending(self):
        identity = getattr(self, "_api_pending_turn_id", None)
        if identity and not self._api_pending_work():
            self._finish_api_turn(
                identity,
                "blocked"
                if self.goal.pending or self._automatic_failed
                else "failed"
                if self._async_failed
                else "completed",
                self.last_goal_result,
            )
            self._api_pending_turn_id = None

    def _api_pending_work(self):
        # Later user queries do not belong to the current query's execution.
        return set(self.pending_work()) - {"goal", "user_input"}

    def _bind_task_goal(self, query, definition):
        from homebrew_harness.context.task_intent import TaskIntent
        from homebrew_harness.storage.control_boundary import ProposalSuperseded

        intent = TaskIntent()
        intent.restore(self.intent.to_dict())
        intent.update(query)
        prepared = self.goal.prepare_goal(definition)
        prepared.state["task_intent"] = intent.to_dict()
        identity = uuid.uuid4().hex
        try:
            try:
                self._checkpoint_writer.save(
                    "idle",
                    boundary={
                        "goal_binding": prepared.entries,
                        "guard": self.inputs.token(),
                    },
                    overrides={
                        "intent": intent.to_dict(),
                        "context_intent": intent.to_dict(),
                        "goal_id": prepared.state["goal_id"],
                        "boundary_id": identity,
                    },
                )
            except Exception:
                if (
                    self.runtime_store.get("checkpoints", self.checkpoint_key, {}).get(
                        "boundary_id"
                    )
                    != identity
                ):
                    raise
            self.intent.restore(intent.to_dict())
            self.user_request = self.intent.original
            self.goal.adopt_prepared(prepared)
        except ProposalSuperseded as error:
            self.goal.discard_prepared(prepared)
            raise RecoveryRequired(
                "Task/Goal binding changed; inspect the current input before retrying."
            ) from error
        except BaseException:
            self.goal.discard_prepared(prepared)
            raise

    def run_user(self, query: str, *, goal_spec=None, literal=False) -> str:
        if goal_spec is not None:
            from homebrew_harness.orchestration.hard_conditions import validate_spec

            goal_spec = validate_spec(goal_spec)
            if not isinstance(query, str) or not query.strip():
                raise ValueError("A Goal-bound task needs nonempty text.")
        command = (
            HostCommand(query)
            if goal_spec is not None or literal
            else HostCommand.parse(query)
        )
        direct = self._independent_control(command)
        if direct is not None:
            return direct
        if self._execution_paused and not command.local:
            raise ValueError("Execution is paused; queue your changes and use /resume.")
        if self.run_control is not None and not command.local:
            raise ValueError(
                "Managed task input uses submit_user_input; execution uses run_episode."
            )
        if command.name == "inputs":
            with control_scope(Deadline.after(2)):
                if self._closed:
                    raise ValueError("Harness session is closed.")
                self._ensure_runtime()
                result = (
                    self.inputs.status()
                    if command.action == "status"
                    else self.inputs.manage(command.action, command.argument)
                )
                return json.dumps(result, ensure_ascii=False)
        deadline = (
            operation_deadline(self.turn_timeout)
            if command.mode == "context"
            else Deadline.after(2)
            if command.local
            else operation_deadline(self.turn_timeout)
        )
        with (
            control_scope(deadline) if command.local else nullcontext(),
            self.gateway.scope(cancel_event=None if command.local else self._execution_cancel),
            acquired(self._runtime_init_lock, deadline, inherit=not command.local),
        ):
            return (
                self._run_user(command, deadline)
                if goal_spec is None
                else self._run_user(command, deadline, goal_spec=goal_spec)
            )

    def _independent_control(self, command):
        if command.name == "quit":
            self.close()
            return json.dumps(self.shutdown_status())
        if command.name == "stop":
            return json.dumps(self.request_stop(command.argument or None))
        if command.name == "resume" and command.action != "list":
            if command.argument:
                raise ValueError("Historical /resume ID belongs to the terminal entry; use /resume list.")
            if self._execution_paused or self.goal.pending and not self.goal.enabled:
                return json.dumps(self.resume_execution())
        if command.mode == "read" and command.name in {"plan", "init"}:
            return json.dumps(self.conversation if command.name == "plan" else {"file": self.project_guidance.snapshot(), "draft": self.conversation["init"]}, ensure_ascii=False, indent=2)
        from homebrew_harness.runtime.local_commands import QUERY_NAMES, run as local_run
        if command.mode == "read" and command.name in QUERY_NAMES:
            with control_scope(Deadline.after(2)):
                if not self._runtime_started:
                    self._ensure_runtime()
                return local_run(self, command)
        return None

    def control(self, command):
        """Execute a typed local host control without interpreting task text."""
        if not isinstance(command, HostCommand) or not command.local:
            raise ValueError("Host control requires a local HostCommand.")
        direct = self._independent_control(command)
        if direct is not None:
            return direct
        deadline = Deadline.after(2)
        with (
            control_scope(deadline),
            acquired(self._runtime_init_lock, deadline, inherit=False),
        ):
            return self._run_user(command, deadline)

    def _run_user(
        self, command: HostCommand, turn_deadline: Deadline, *, goal_spec=None
    ) -> str:
        if self._closed:
            raise ValueError("Harness session is closed.")
        from homebrew_harness.runtime.activities import activity_scope
        query = command.query
        with self.gateway.scope(cancel_event=self.stop_event, deadline=turn_deadline):
            self._ensure_runtime(turn_deadline)
        self._user_waiting.set()
        work_started = False
        try:
            with (
                capability_scope(self.capabilities),
                self.gateway.scope(
                    role="lead",
                    context_id="user",
                    cancel_event=self.stop_event,
                    deadline=turn_deadline,
                ),
                acquired(self.lock, turn_deadline, inherit=not command.local),
                checkpoint_scope(self._checkpoint_writer),
                self.trace.scope("user", "lead"),
                activity_scope(self.activities),
                requester_scope(self.lead_binding),
                owner_scope(self),
            ):
                if command.name == "recovery" and command.action == "status":
                    return json.dumps(
                        self.recovery_status(), ensure_ascii=False, indent=2
                    )
                if command.name == "recovery" and command.action == "inspect":
                    identity, revision = command.values
                    if identity.startswith("bg_"):
                        self.background.inspect_unknown(identity, revision)
                        self._recovery_blocked = [
                            x for x in self._recovery_blocked if x != identity
                        ]
                        self._save_checkpoint()
                        return "Host inspected " + identity
                    if identity.startswith("cron_"):
                        self.cron.inspect_run(identity, revision)
                        self._recovery_blocked = [
                            x for x in self._recovery_blocked if x != identity
                        ]
                        self._save_checkpoint()
                        return "Host inspected " + identity
                    return self.inspect_operation(identity, revision)
                if command.name == "recovery" and command.action == "continue":
                    if (
                        self.recovery_operations()
                        or (self.cron.unresolved() if self.cron else [])
                        or any(
                            item["status"] == "unknown" and not item.get("inspected")
                            for item in (
                                self.background.snapshot() if self.background else []
                            )
                        )
                    ):
                        raise RecoveryRequired(
                            "Inspect the listed operation IDs before continuing; active work cannot be acknowledged."
                        )
                    self._recovery_blocked.clear()
                    for message in self.messages:
                        if message.get("role") == "tool":
                            message["content"] = inspected_output(
                                message.get("content", "")
                            )
                    self._automatic_failed = self._control_blocked = (
                        self._control_limit
                    ) = False
                    self._save_checkpoint()
                    return "Host acknowledged the interrupted boundary; tool records were retained."
                self._refresh_recovery()
                if self._recovery_blocked and not command.allowed_during_recovery:
                    raise RecoveryRequired(
                        "Recovery requires host inspection. Use /recovery for pending outcomes, then /recovery continue after checking them."
                    )
                if self.conversation["mode"] == "plan" and command.name in {"goal", "task"} and not command.local:
                    raise ValueError("Leave planning mode or apply its plan before activating executable Goal/task work.")
                if command.name in {"plan", "init"}:
                    from homebrew_harness.runtime import conversation_commands as conversation
                    if command.action == "status":
                        return json.dumps(self.conversation if command.name == "plan" else {"file": self.project_guidance.snapshot(), "draft": self.conversation["init"]}, ensure_ascii=False, indent=2)
                    if command.name == "init":
                        value = conversation.publish_init(self, command.values[0]) if command.action == "apply" else conversation.init_draft(self, preview=command.action == "preview")
                        return json.dumps(value, ensure_ascii=False, indent=2)
                    if command.action in {"on", "off"}:
                        return json.dumps(conversation.set_mode(self, "plan" if command.action == "on" else "normal"), ensure_ascii=False)
                    if command.action == "discuss":
                        conversation.set_mode(self, "plan")
                        query = command.argument
                    elif command.action == "apply":
                        query = conversation.accept_plan(self, command.values[0])
                        command = HostCommand("/goal resume", "goal", "resume") if self.goal.pending else HostCommand(query)
                if self.conversation["mode"] == "plan" and (not command.name or command.name == "plan" and command.action == "discuss"):
                    from homebrew_harness.runtime.conversation_commands import plan_turn
                    if goal_spec is not None:
                        self._bind_task_goal(query, goal_spec)
                        self.goal.suspend("suspended", "Goal awaits accepted planning execution.")
                    work_started = True
                    return plan_turn(self, query)
                if command.name in {"pwd", "ps", "hooks", "agents", "worktree", "memory", "resume"}:
                    from homebrew_harness.runtime.local_commands import run as local_run
                    return local_run(self, command)
                if command.extended and command.name not in {"plan", "init"}:
                    from homebrew_harness.runtime.session_commands import run
                    result = run(self, command)
                    if result is not None:
                        return result
                    query = command.values[0]
                    if self.conversation["mode"] == "plan":
                        from homebrew_harness.runtime.conversation_commands import plan_turn
                        work_started = True
                        return plan_turn(self, query)
                if command.name == "mcp":
                    if self.mcp is None:
                        return json.dumps({"connections": [], "enabled": False})
                    result = (
                        self.mcp.connection_status()
                        if command.action == "status"
                        else self.mcp.close_connection(command.argument)
                    )
                    return json.dumps(result, ensure_ascii=False, indent=2)
                if command.name == "goal" and command.action == "status":
                    return json.dumps(self.goal.status(), ensure_ascii=False, indent=2)
                if command.name == "goal" and command.action == "cancel":
                    execution_check(turn_deadline, gateway=False)
                    self._goal_ready = False
                    result = self.goal.clear()
                    with cleanup_scope():
                        self._save_checkpoint()
                    return result
                if command.name == "usage":
                    return json.dumps(
                        self.gateway.summary(), ensure_ascii=False, indent=2
                    )
                if command.name == "storage":
                    from homebrew_harness.storage.storage_metrics import metrics_for

                    return json.dumps(
                        {
                            "backend": self.runtime_store.backend_name,
                            "trace": self.trace.status(),
                            "metrics": metrics_for(
                                self.runtime_store.workdir
                            ).snapshot(),
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                if command.name == "tests":
                    if command.action == "status":
                        return json.dumps(
                            {"tests": self.tests.catalog()},
                            ensure_ascii=False,
                            indent=2,
                        )
                    action, name = command.action, command.argument
                    if action == "approve":
                        assignment = (
                            self.workspace.store.owned()
                            if self.workspace.store
                            else None
                        )
                        self.tests.approve(
                            name,
                            cwd=self.workspace.lead_cwd(),
                            role="lead",
                            task_id=assignment.id if assignment else None,
                        )
                        return "Host test authorization recorded for " + name + "."
                    if action == "revoke":
                        self.tests.revoke(name)
                        return "Host test authorization revoked for " + name + "."
                    raise TestRunnerError(
                        "Use /tests, /tests approve NAME or /tests revoke NAME."
                    )
                if command.name == "task":
                    query = command.argument
                    if (
                        self.pending_work(include_dispatch=False)
                        or (
                            self.workspace.store.owned()
                            if self.workspace.store
                            else None
                        )
                        is not None
                        or any(
                            member.status not in {"stopped", "failed"}
                            for member in self.members.values()
                        )
                    ):
                        raise ValueError(
                            "Finish or stop pending asynchronous work before starting a new task."
                        )
                    self.intent.update(query, new_task=True)
                    self.todo = homebrew_harness.runtime.assembly.prepare_context(
                        self.client,
                        self.model,
                        self.workdir,
                        replace(self.loop_components(), todo_manager=None),
                    ).todo_manager
                    self.messages[:] = [
                        {
                            "role": "system",
                            "content": homebrew_harness.runtime.prompts.build_system_prompt(
                                self.workdir,
                                skill_loader=self.skills,
                                capabilities=self.capabilities,
                            ),
                        }
                    ]
                    self.goal.clear()
                if goal_spec is not None:
                    self._bind_task_goal(query, goal_spec)
                elif command.name == "goal":
                    if command.action == "resume":
                        goal_id = command.argument or (
                            self.goal.state["goal_id"] if self.goal.state else ""
                        )
                        self.goal.restore(goal_id)
                        query = self.goal.resume_prompt()
                        if self.goal.state.get("task_intent") is not None:
                            self.intent.restore(self.goal.state["task_intent"])
                        else:
                            self.intent.update(query, new_task=True)
                    else:
                        definition = (
                            command.values[0]
                            if command.action == "spec"
                            else command.argument
                        )
                        self.goal.set_goal(definition)
                        query = (
                            definition["condition"]
                            if isinstance(definition, dict)
                            else definition
                        )
                        self.intent.update(query)
                else:
                    self.goal.begin_query()
                self._goal_ready = False
                self.intent.update(query)
                self.user_request = self.intent.original
                if self.goal.pending:
                    self.goal.state["task_intent"] = self.intent.to_dict()
                    self.goal._persist()
                if self.workspace.store:
                    self.workspace.store.save_intent()
                self.gateway.reset_failures()
                self._control_blocked = False
                self._control_limit = False
                work_started = True
                with self.inputs.turn():
                    result = homebrew_harness.runtime.turns.run_user_turn(
                        self.client,
                        self.model,
                        self.messages,
                        query,
                        self.workdir,
                        memory_runtime=self.memory,
                        max_turns=self.max_turns,
                        collect_background=False,
                        goal_controller=self.goal,
                        stop_block_cap=self.stop_block_cap,
                        stop_event=self.stop_event,
                        components=self.loop_components(),
                    )
                self.user_request = self.intent.original
                if self.workspace.store:
                    self.workspace.store.save_intent()
                if self.goal.pending:
                    self.goal.state["task_intent"] = self.intent.to_dict()
                    self.goal._persist()
                self.last_goal_result = result
                self._quota_blocked = False
                self._automatic_failed = False
                return result
        except Exception as error:
            if work_started and self.goal.enabled:
                controlled = gateway_error(error)
                self.goal.suspend(
                    "limit"
                    if isinstance(controlled, ModelDeadlineExceeded)
                    else "suspended"
                    if isinstance(controlled, ModelCancelled)
                    else "error",
                    str(controlled)
                    if controlled
                    else "Worker turn failed ("
                    + type(error).__name__
                    + "); goal retained.",
                )
            if quota_exhausted(error):
                self._quota_blocked = True
                self._emit_trace("provider_blocked", reason="insufficient_quota")
            raise
        finally:
            try:
                if work_started:
                    with self.lock:
                        self._save_checkpoint("idle")
            finally:
                self._user_waiting.clear()
                if (
                    not self._closed
                    and self.inputs.pending()
                    and not self._poller.started
                ):
                    self.start(include_clock=False)

    def run_episode(self):
        """One host-controlled segment without adding a synthetic user prompt."""
        control = self.run_control
        if control is None:
            raise ValueError("run_episode requires a managed RunController.")
        from homebrew_harness.runs.controller import run_scope

        with (
            capability_scope(self.capabilities),
            self.gateway.scope(
                role="lead",
                context_id="managed",
                cancel_event=control.cancel,
                deadline=control.episode_deadline,
            ),
            acquired(self.lock),
            checkpoint_scope(self._checkpoint_writer),
            self.trace.scope("managed", control.id),
            run_scope(control),
            self.inputs.turn(),
        ):
            if self._recovery_blocked:
                raise RecoveryRequired("Managed execution requires host inspection.")
            self.goal.state and self.goal.state.update(episode_workers=0)
            if not control.state["submitted"]:
                self.intent.update(control.spec.prompt, new_task=True)
                self.user_request = self.intent.original
                if control.spec.goal:
                    identity = "goal_" + control.id[4:]
                    if self.runtime_store.get("goals", identity):
                        self.goal.restore(identity)
                    else:
                        self.goal.set_goal(control.spec.goal, goal_id=identity)
                    self.goal.state["task_intent"] = self.intent.to_dict()
                    self.goal._persist()
                components = replace(
                    self.loop_components(), on_user_submit=control.submitted
                )
                return homebrew_harness.runtime.turns.run_user_turn(
                    self.client,
                    self.model,
                    self.messages,
                    control.spec.prompt,
                    self.workdir,
                    components=components,
                    max_turns=control.spec.episode_model_calls,
                    goal_controller=self.goal,
                    collect_background=True,
                    stop_event=self.stop_event,
                    stop_block_cap=self.stop_block_cap,
                )
            with homebrew_harness.runtime.assembly._prepare_loop(
                self.client, self.model, self.workdir, self.loop_components()
            ) as prepared:
                return prepared.run(
                    self.messages,
                    control.spec.episode_model_calls,
                    active_request=self.intent.original,
                    goal_controller=self.goal,
                    stop_block_cap=self.stop_block_cap,
                )

    def process_once(self) -> bool:
        from homebrew_harness.runtime.activities import activity_scope
        with activity_scope(self.activities), self.gateway.scope(cancel_event=None if self._execution_paused else self._execution_cancel), capability_scope(self.capabilities):
            return self._process_once()

    def _process_once(self) -> bool:
        if self._closed or self.stop_event.is_set():
            return False
        self._ensure_runtime()
        if self._consume_user_input():
            return True
        self._refresh_recovery()
        if self._execution_paused or self._recovery_blocked or self.conversation["mode"] == "plan":
            return False
        if (
            self.stop_event.is_set()
            or self.mode == "one_shot"
            and self._automatic_failed
            or self._control_blocked
            or self.gateway.blocked
            or self._quota_blocked
            or self._user_waiting.is_set()
            or not self.lock.acquire(blocking=False)
        ):
            return False
        changed = False
        try:
            if self._user_waiting.is_set() or self.stop_event.is_set():
                return False
            test_ready = (
                self.goal.enabled
                and self.goal.state["status"] == "waiting"
                and "tests" in self.goal.state.get("waiting_on", [])
                and not self.tests.active.is_set()
            )
            if not (
                self._pending_team
                or self._pending_background
                or self._goal_ready
                and self.goal.enabled
                or test_ready
                or self.workspace.bus.available("lead")
                or (self.background is not None and self.background.has_ready())
                or (self.cron is not None and self.cron.has_pending())
            ):
                return False
            changed = True
            self._pending_team.extend(self.workspace.consume_lead_inbox())
            self._pending_background.extend(
                (self.background.collect() if self.background else [])
            )
            now = time.monotonic()
            if (
                self.goal.enabled
                and self.goal.state["status"] == "waiting"
                and "tests" in self.goal.state.get("waiting_on", [])
                and not self.tests.active.is_set()
            ):
                self._goal_ready = True
            observations = (
                bool(
                    self._pending_team
                    or self._pending_background
                    or self._goal_ready
                    and self.goal.enabled
                )
                and now >= self._observation_retry
            )
            # An unfinished manual task is still a Lead assignment. Scheduled
            # work waits, rather than inheriting that task's worktree implicitly.
            cron_ready = (self.cron is not None and self.cron.has_pending()) and (
                self.workspace.store.owned() if self.workspace.store else None
            ) is None
            if observations and (not cron_ready or self._last_source == "cron"):
                self._last_source = "observations"
                team = copy.deepcopy(self._pending_team)
                background = copy.deepcopy(self._pending_background)
                self._async_failed |= any(
                    item.get("status") in {"failed", "cancelled"} for item in background
                )
                source = (
                    "events"
                    if team and background
                    else "team"
                    if team
                    else "background"
                    if background
                    else "goal"
                )
                self._emit_trace(
                    "observations_collected",
                    team_events=len(team),
                    background_ids=[result["bg_id"] for result in background],
                )
                self._checkpoint_writer.completion_ack = [
                    ("lead", self.workspace.bus.consumer, [m["id"] for m in team]),
                    ("background", None, [b["bg_id"] for b in background]),
                ]
                try:
                    self._observing = True
                    with (
                        self.gateway.scope(
                            role="lead",
                            context_id=source,
                            cancel_event=self.stop_event,
                            deadline=Deadline.after(self.turn_timeout),
                        ),
                        checkpoint_scope(self._checkpoint_writer),
                        self.trace.scope(source, "lead"),
                    ):
                        result = homebrew_harness.runtime.turns.run_team_turn(
                            team,
                            client=self.client,
                            model=self.model,
                            messages=self.messages,
                            workdir=self.workdir,
                            background_results=background,
                            active_request=self.user_request,
                            memory_runtime=self.memory,
                            max_turns=self.max_turns,
                            stop_event=self.stop_event,
                            collect_background=False,
                            goal_controller=self.goal,
                            stop_block_cap=self.stop_block_cap,
                            components=self.loop_components(),
                        )
                    self._goal_ready = False
                    self.last_goal_result = result
                    self._pending_team.clear()
                    self._pending_background.clear()
                    self._save_checkpoint(
                        ack=[
                            (
                                "lead",
                                self.workspace.bus.consumer,
                                [m["id"] for m in team],
                            ),
                            ("background", None, [b["bg_id"] for b in background]),
                        ]
                    )
                    print(f"\n[runtime] {source}\n{result}", flush=True)
                except Exception as error:
                    self._automatic_failed = True
                    controlled = gateway_error(error)
                    self._control_blocked = controlled is not None
                    self._control_limit = isinstance(controlled, ModelDeadlineExceeded)
                    if self.goal.enabled:
                        self.goal.suspend(
                            "limit"
                            if self._control_limit
                            else "suspended"
                            if isinstance(controlled, ModelCancelled)
                            else "error",
                            str(controlled)
                            if controlled
                            else "Automatic worker turn failed ("
                            + type(error).__name__
                            + "); goal retained.",
                        )
                    if quota_exhausted(error):
                        self._quota_blocked = True
                        self._emit_trace(
                            "provider_blocked", reason="insufficient_quota"
                        )
                    if not getattr(error, "retryable", True):
                        delivered = list(self._pending_team)
                        self._pending_team.clear()
                        self._pending_background.clear()
                        self._save_checkpoint(
                            ack=[
                                (
                                    "lead",
                                    self.workspace.bus.consumer,
                                    [m["id"] for m in delivered],
                                ),
                                ("background", None, [b["bg_id"] for b in background]),
                            ]
                        )
                    self._observation_retry = time.monotonic() + self.retry_delay
                    print(
                        f"[runtime] Observation turn stopped ({type(error).__name__}).",
                        file=sys.stderr,
                    )
                return True
            if cron_ready:
                job = self.cron.take_pending()
                if job is None:
                    return False
                if now < self._cron_retry.get(job.id, 0):
                    self.cron.restore(job)
                    return False
                self._last_source = "cron"
                try:
                    with (
                        self.gateway.scope(
                            role="lead",
                            context_id=job.id,
                            cancel_event=self.stop_event,
                            deadline=Deadline.after(self.turn_timeout),
                        ),
                        checkpoint_scope(self._checkpoint_writer),
                        self.trace.scope("cron", job.id),
                    ):
                        result = homebrew_harness.runtime.turns.run_scheduled_turn(
                            job,
                            client=self.client,
                            model=self.model,
                            messages=self.messages,
                            workdir=self.workdir,
                            scheduler=self.cron,
                            memory_runtime=self.memory,
                            stop_event=self.stop_event,
                            max_turns=self.max_turns,
                            collect_background=False,
                            stop_block_cap=self.stop_block_cap,
                            goal_observer=self.goal,
                            components=self.loop_components(),
                        )
                    if (
                        self.goal.enabled
                        and self.goal.state["status"] == "waiting"
                        and "cron" in self.goal.state.get("waiting_on", [])
                    ):
                        self._goal_ready = True
                    self._cron_retry.pop(job.id, None)
                    print(f"\n[runtime] cron {job.id}\n{result}", flush=True)
                except Exception as error:
                    self._automatic_failed = True
                    controlled = gateway_error(error)
                    self._control_blocked = controlled is not None
                    self._control_limit = isinstance(controlled, ModelDeadlineExceeded)
                    if quota_exhausted(error):
                        self._quota_blocked = True
                        self._emit_trace(
                            "provider_blocked", reason="insufficient_quota"
                        )
                    self.cron.restore(job)
                    self._cron_retry[job.id] = time.monotonic() + self.retry_delay
                    print(
                        f"[runtime] Scheduled turn stopped ({type(error).__name__}).",
                        file=sys.stderr,
                    )
                return True
            return False
        finally:
            self._observing = False
            try:
                self._checkpoint_writer.completion_ack = ()
                if changed:
                    self._save_checkpoint()
            finally:
                self.lock.release()
                self._drain_changed.set()

    def wait_user_boundary(self, timeout=2):
        """Allow a visible final reply to finish its critical input commit."""
        if self.gateway.inflight or self.tests.active.is_set() or self.goal.pending or any(row["stoppable"] for row in self.activities.snapshot()):
            return False
        expires = time.monotonic() + timeout
        while self._input_dispatching.is_set() or self._user_waiting.is_set() or self.inputs.active_id:
            if self.gateway.inflight or self.tests.active.is_set() or self.goal.pending or any(row["stoppable"] for row in self.activities.snapshot()):
                return False
            remaining = expires - time.monotonic()
            if remaining <= 0:
                return False
            self._drain_changed.wait(min(.05, remaining))
            self._drain_changed.clear()
        return True

    def _publish_pause(self, paused):
        db = self.runtime_store.backend()
        candidate = None
        try:
            with cleanup_scope(), db.write() as conn:
                meta = db._get(conn, "session_meta", self.session_id)
                if not meta or meta.get("instance") != self.workspace.leases.instance:
                    raise ValueError("Session pause belongs to its live owner.")
                candidate = {**meta, "execution_paused": paused, "revision": meta["revision"] + 1}
                db._put(conn, "session_meta", self.session_id, candidate)
        except Exception:
            saved = self.runtime_store.get("session_meta", self.session_id, {})
            if not candidate or saved.get("instance") != candidate["instance"] or saved.get("execution_paused") is not paused or saved.get("revision", 0) < candidate["revision"]:
                raise
            self._emit_trace("control_confirmation_lost", paused=paused)

    def request_stop(self, identity=None):
        if self._closed:
            return self.shutdown_status()
        with control_scope(Deadline.after(2)):
            if not self._runtime_started:
                self._ensure_runtime()
            if self.run_control:
                if identity is not None:
                    raise ValueError("Managed resources use their native Run controls.")
                return self.run_control.store.control(self.run_control.id, "pause")
            if identity and identity != "agent:lead":
                from homebrew_harness.runtime.local_commands import stop_target
                return stop_target(self, identity)
            with self.inputs.lock:
                self._publish_pause(True)
                self._execution_paused = True
                self._execution_cancel.set()
                self.inputs.cancel.set()
            for row in self.gateway.active_requests():
                try:
                    self.gateway.stop_request(row["id"])
                except ValueError:
                    pass
            for row in self.activities.snapshot():
                if row["stoppable"]:
                    try:
                        self.activities.stop(row["id"])
                    except ValueError:
                        pass
            self._goal_changed.set()
            return {"state": "stop_requested", "session_id": self.session_id}

    def resume_execution(self):
        if self._closed:
            raise ValueError("Restore the closed Session by ID before continuing.")
        with control_scope(Deadline.after(2)):
            if not self._runtime_started:
                self._ensure_runtime()
            if self.run_control:
                return self.run_control.store.control(self.run_control.id, "run")
            if self.inputs.active_id or self._user_waiting.is_set() or self.gateway.inflight or self.tests.active.is_set() or self.workflow and self.workflow.pending() or any(r["stoppable"] for r in self.activities.snapshot()):
                raise ValueError("Stopped work still needs cleanup; inspect /ps before /resume.")
            self._refresh_recovery()
            if self._recovery_blocked or self.recovery_operations():
                raise ValueError("Inspect /recovery before continuing the stopped task.")
            self._publish_pause(False)
            self._execution_paused = False
            self._execution_cancel = threading.Event()
            self._automatic_failed = False
            pending = self.inputs.pending()
            if self.conversation["mode"] == "plan":
                if not pending and self.intent.original:
                    self.submit_user_input("Continue the planning discussion without executing the project.", mode="queued")
            elif self.goal.pending and not any(row["content"].strip() == "/goal resume" for row in pending):
                self.submit_user_input("/goal resume", mode="queued")
            elif not pending and self.intent.original:
                self.submit_user_input("Continue the interrupted task using saved requirements and results; inspect current state before repeating work.", mode="queued")
            self._goal_changed.set()
            return {"state": "accepted", "session_id": self.session_id}

    def close(self):
        self._closed = True
        self.stop_event.set()
        self.tests.closed.set()
        for member in list(self.members.values()):
            member.stop_event.set()
        self.gateway.close()
        self._shutdown.request(
            lambda: self._close_components(time.monotonic() + self.shutdown_timeout),
            self.shutdown_timeout,
        )

    def _emit_trace(self, kind, **fields):
        self._observations.run("trace", lambda: self.trace.emit(kind, **fields))

    def _critical_close(self, label, action, *, repeat=False):
        result = self._close_progress.step(label, action, repeat=repeat)
        if not result:
            self._emit_trace("cleanup_error", component=label)
        return result

    def _close_components(self, deadline):
        self._goal_changed.set()
        self._drain_changed.set()
        remaining = lambda: max(0, deadline - time.monotonic())
        actions = [
            ("approval_reviewer", self.approval_reviewer.close),
            ("tests", lambda: self.tests.close(timeout=remaining())),
            ("shell", lambda: self.shell.close(timeout=remaining())),
        ]
        if self.workflow:
            actions.append(("workflow", self.workflow.close))
        if self.mcp:
            actions.append(("mcp", lambda: self.mcp.close(timeout=remaining())))
        if self.background:
            actions.append(
                ("background", lambda: self.background.close(timeout=remaining()))
            )
        actions.append(("poller", lambda: self._poller.close(remaining())))
        for label, action in actions:
            self._critical_close(label, action)
        self._close_progress.require(label for label, _ in actions)
        self._finish_close()

    def shutdown_status(self):
        progress = self._close_progress.snapshot()
        errors = self._shutdown_errors()
        held = self.workspace.leases.held("session")
        complete = self._close_finished.is_set() and not held
        return {
            "execution_paused": self._execution_paused,
            "state": "open"
            if not self._closed
            else "pending"
            if not complete
            else "error"
            if errors
            else "closed",
            "resources_closed": complete,
            "persistence_ok": complete and not errors,
            "errors": errors,
            "start_error": self._shutdown.error,
            "session_lease_held": held,
            "cleanup_history": progress["history"],
            "retryable": self._closed and not complete and not self._shutdown.started,
            "completed_steps": progress["completed_steps"],
            "failed_steps": progress["failed_steps"],
            "observations": self._observations.snapshot(),
        }

    def _shutdown_errors(self):
        return [
            *self._close_progress.snapshot()["errors"],
            *getattr(self.background, "cleanup_errors", []),
            *getattr(self.team, "cleanup_errors", []),
            *getattr(self.workflow, "cleanup_errors", []),
        ]

    def wait_closed(self, timeout=5):
        """Observe deferred cleanup without exceeding the caller wait limit."""
        return self._close_finished.wait(timeout)

    def _finish_close(self, deadline=None):
        def wait(action):
            if action() is not True:
                raise ClosePending(
                    "Resource wait returned before actual execution ended."
                )

        waits = [
            ("wait_poller", lambda: self._poller.workers.wait(None)),
            ("wait_gateway", lambda: self.gateway.wait_idle(None)),
        ]
        for label, component in (
            ("tests", self.tests),
            ("shell", self.shell),
            ("background", self.background),
            ("workflow", self.workflow),
            ("mcp", self.mcp),
            ("workspace", self.workspace),
        ):
            if component is not None and hasattr(component, "wait_closed"):
                waits.append(
                    (
                        "wait_" + label,
                        lambda component=component: component.wait_closed(None),
                    )
                )
        for label, action in waits:
            self._critical_close(label, lambda action=action: wait(action))
        self._close_progress.require(label for label, _ in waits)
        with self._runtime_init_lock, self.lock, cleanup_scope():
            can_report_checkpoint_failure = (
                not self._pending_team
                and not self._pending_background
                and not self._persistence_error
            )
            steps = [
                ("models", self.models.close),
                (
                    "user_delivery",
                    lambda: (
                        self.inputs.release(self._pending_user)
                        if self._pending_user
                        else None
                    ),
                ),
                (
                    "background_delivery",
                    lambda: (
                        self.background.restore(self._pending_background)
                        if self.background
                        else None
                    ),
                ),
                ("checkpoint", lambda: self._save_checkpoint("closed")),
                (
                    "team_delivery",
                    lambda: self.workspace.bus.release("lead", self._pending_team),
                ),
                (
                    "team",
                    lambda: self.workspace.close(
                        timeout=0
                        if deadline is None
                        else max(0, deadline - time.monotonic())
                    ),
                ),
                ("goal", self.goal.close),
            ]
            for label, action in steps:
                if not self._critical_close(label, action):
                    if label == "checkpoint" and can_report_checkpoint_failure:
                        self._close_progress.terminal_failure(label)
                    if (
                        label == "goal"
                        and self.goal.lease is None
                        and not self.goal.enabled
                    ):
                        self._close_progress.terminal_failure(label)
                    self._close_progress.require([label])
            self._pending_team.clear()
            self._pending_background.clear()
            if self._runtime_started:
                errors = [
                    item
                    for item in self._shutdown_errors()
                    if item.get("component")
                    not in {"session_lease", "session_lifecycle"}
                ]
                self._critical_close(
                    "session_lifecycle",
                    lambda: self.runtime_store.finish_session(
                        self.session_id,
                        self.workspace.leases.instance,
                        complete=not errors,
                    ),
                    repeat=True,
                )
                self._close_progress.require(["session_lifecycle"])

            def release():
                self.workspace.release_session()
                if self.workspace.leases.held("session"):
                    raise ClosePending(
                        "Session lease remains held; inspect pending resources and retry close."
                    )

            released = self._critical_close("session_lease", release)
            if not released and self.workspace.leases.held("session"):
                if self._runtime_started:
                    self._critical_close(
                        "session_lifecycle",
                        lambda: self.runtime_store.finish_session(
                            self.session_id,
                            self.workspace.leases.instance,
                            complete=False,
                        ),
                        repeat=True,
                    )
                raise ClosePending(
                    "Critical session release is pending; close can be retried."
                )
            self._emit_trace("session_closed")
