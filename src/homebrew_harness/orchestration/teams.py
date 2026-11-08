"""s13: persistent independent workers, task leases and typed coordination."""

from __future__ import annotations

import copy
import json
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from homebrew_harness.context.task_intent import TaskIntent
from homebrew_harness.context.tracked_history import (
    ConversationHistory,
    encoded_message,
    set_context,
)
from homebrew_harness.execution.execution_control import acquired
from homebrew_harness.orchestration.task_store import Task, TaskStore
from homebrew_harness.storage.durable_runtime import (
    CheckpointWriter,
    checkpoint_scope,
    repair_history,
)
from homebrew_harness.storage.message_bus import validate_agent_name
from homebrew_harness.runtime.runtime_state import RecoveryRequired

TEAM_TOOL_NAMES = (
    "spawn_teammate",
    "list_teammates",
    "send_message",
    "request_shutdown",
    "request_plan",
    "review_plan",
    "create_worktree",
)
WORKSPACE_TOOLS = {
    "list_artifacts",
    "get_artifact",
    "publish_artifacts",
    "run_tests",
    "bash",
    "read_file",
    "write_file",
    "edit_file",
    "glob",
    "search_text",
    "git_status",
    "git_diff",
}
MODIFY_TOOLS = {
    "publish_artifacts",
    "run_tests",
    "bash",
    "write_file",
    "edit_file",
    "complete_task",
}


@dataclass
class ProtocolState:
    request_id: str
    type: str
    sender: str
    target: str
    status: str
    payload: str = ""
    work_version: int | None = None
    task_id: str | None = None


class TeamTurnError(RuntimeError):
    def __init__(self, error: Exception, *, retryable: bool):
        super().__init__(f"Team event turn failed ({type(error).__name__}).")
        self.retryable = retryable


class TeammateRuntime:
    def __init__(self, manager, name: str, role: str, prompt: str, require_plan: bool):
        self.manager, self.name, self.role = manager, name, role
        self.store = TaskStore(manager.workdir, owner=name, leases=manager.leases)
        if getattr(manager, "managed", None):
            from homebrew_harness.runs.stages import MemberStageAccess

            self.store.stage_control = MemberStageAccess(
                manager.managed.control.stages, name, self.store
            )
        self.failure_kind = None
        self.last_outcome = None
        self.permission_blocked = None
        self.store.intent = TaskIntent()
        if manager.task_intent is not None:
            self.store.intent.restore(manager.task_intent.to_dict())
        self.messages = ConversationHistory([{"role": "user", "content": prompt}])
        if (
            manager.task_intent is not None
            and manager.task_intent.message() is not None
        ):
            import copy

            inherited = copy.deepcopy(manager.task_intent.message())
            inherited["name"] = "inherited_task_constraints"
            self.messages.insert(0, inherited)
        self.compactor = None
        self.model_binding = None
        self.user_revision = 0
        self.user_input_ids = []
        self.todo_manager = None
        self.require_plan = require_plan
        self.allowed_tasks = frozenset()
        self.gate = "required" if require_plan else "not_required"
        self.plan_request: str | None = None
        self.version = 0
        self.assignment: Task | None = None
        self.assignment_path: Path | None = None
        self.status = "working"
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.pending_context: list[dict] = []
        self.inflight_messages = []
        self.step_lock = threading.RLock()
        self.active_tool = False
        self.snapshot_revision = 0
        self._snapshot_signatures = {}
        self._team_signature = (json.dumps([], sort_keys=True), ())

    @contextmanager
    def tool_step(self, name=""):
        with acquired(self.step_lock):
            with self.manager.lock:
                self.active_tool = True
                self.active_tool_name = name
                self.active_thread = threading.get_ident()
            try:
                yield
            finally:
                with self.manager.lock.lock:
                    self.active_tool = False

    def bind(self, task: Task, *, automatic: bool = False, defer: bool = False) -> None:
        path = self.store.workspace(task)
        self.assignment, self.assignment_path = task, path
        if task.task_intent is not None:
            inherited = TaskIntent()
            inherited.restore(task.task_intent)
            message = inherited.message()
            if message is not None:
                message["name"] = "inherited_task_constraints"
                set_context(
                    self.messages, "inherited_task_constraints", message, position=0
                )
        self.version += 1
        self.plan_request = None
        self.gate = "required" if self.require_plan else "not_required"
        context = {
            "role": "user",
            "content": f"[{'Auto-claimed' if automatic else 'Assigned'} task {task.id}] {task.subject}\n"
            f"{task.description}\nTask is already in_progress; do not claim it again.\n"
            f"Work directory: {path}\nPlan status: {self.gate}",
        }
        (self.pending_context if defer else self.messages).append(context)

    def cwd(self) -> Path:
        with self.manager.lock:
            if self.assignment is None:
                raise ValueError("Claim a task before using workspace tools.")
            task = self.store.load(self.assignment.id)
            if task.status == "in_progress":
                self.store.assert_execution(task.id)
            if task.owner != self.name or task.status not in {
                "in_progress",
                "completed",
            }:
                raise ValueError("Task assignment is no longer owned by this teammate.")
            path = self.store.workspace(task)
            if path != self.assignment_path:
                raise ValueError("Task working directory changed during the work turn.")
            return path

    def guard(
        self, name: str, arguments: dict, *, external_mutating: bool = False
    ) -> str | None:
        with self.manager.lock:
            if name in WORKSPACE_TOOLS or external_mutating:
                self.cwd()
            if name == "bash" and arguments.get("run_in_background"):
                return json.dumps({"error": "Teammates use synchronous Bash only."})
            if (name in MODIFY_TOOLS or external_mutating) and self.gate not in {
                "not_required",
                "approved",
            }:
                return json.dumps(
                    {
                        "error": f"Plan status is {self.gate}; submit a plan and wait for Lead approval."
                    }
                )
        return None

    def claim(self, task_id: str, *, workdir: Path) -> str:
        with self.manager.lock:
            if task_id not in self.allowed_tasks:
                raise ValueError(
                    "Task is outside this member's host-approved task scope."
                )
            if self.assignment is not None:
                raise ValueError(
                    "Finish the current work turn before claiming another task."
                )
            if self.manager.lead_busy:
                raise ValueError(
                    "Lead is updating the task board; claim after its turn finishes."
                )
            task = self.store.claim(task_id)
            self.bind(task, defer=True)
            return json.dumps(task.to_dict(), ensure_ascii=False)

    def complete(
        self, task_id: str, *, workdir: Path, summary="", next_steps=""
    ) -> str:
        with self.manager.lock:
            if self.assignment is None or self.assignment.id != task_id:
                raise ValueError("Only the current assigned task can be completed.")
            blocked = self.guard("complete_task", {})
            if blocked:
                return blocked
            task, unblocked = self.store.complete(
                task_id, summary=summary, next_steps=next_steps
            )
            return json.dumps(
                {
                    "task": task.to_dict(),
                    "unblocked": [item.to_dict() for item in unblocked],
                },
                ensure_ascii=False,
            )

    def submit_plan(self, plan: str, *, workdir: Path) -> str:
        with self.manager.lock:
            if self.assignment is None or not isinstance(plan, str) or not plan.strip():
                raise ValueError("A nonempty plan requires an assigned task.")
            if self.store.load(self.assignment.id).status != "in_progress":
                raise ValueError(
                    "Plans can only be submitted for work still in progress."
                )
            if self.gate == "pending":
                raise ValueError("A plan is already pending review.")
            request = self.manager.new_request(
                "plan_approval",
                self.name,
                "lead",
                payload=plan,
                work_version=self.version,
                task_id=self.assignment.id,
            )
            try:
                self.manager.bus.send(
                    self.name,
                    "lead",
                    plan,
                    "plan_approval_request",
                    {"request_id": request.request_id},
                )
            except Exception:
                self.manager.protocols.pop(request.request_id)
                raise
            self.plan_request, self.gate = request.request_id, "pending"
            self.status = "waiting_approval"
            return json.dumps(asdict(request), ensure_ascii=False)

    def handlers(self) -> dict:
        return {
            "claim_task": self.claim,
            "complete_task": self.complete,
            "submit_plan": self.submit_plan,
            "list_tasks": lambda **kwargs: self.store.run_tool("list_tasks", **kwargs),
            "get_task": lambda **kwargs: self.store.run_tool("get_task", **kwargs),
            "send_message": lambda to, content, **kwargs: self.manager.send(
                self.name, to, content
            ),
        }

    def handle_messages(self, inbox: list[dict]) -> bool:
        work = []
        with self.manager.lock:
            self.inflight_messages.extend(inbox)
            for message in inbox:
                kind, metadata = message["type"], message["metadata"]
                request_id = metadata.get("request_id")
                request = (
                    self.manager.protocols.get(request_id)
                    if isinstance(request_id, str)
                    else None
                )
                if kind == "test_grant_decision" and getattr(
                    self.manager, "managed", None
                ):
                    managed = self.manager.managed
                    saved = managed.control.store.runtime.get(
                        "run_requests", metadata.get("request_id", ""), {}
                    )
                    if (
                        message["from"] == "lead"
                        and metadata.get("origin") == "managed_test_authorization"
                        and saved.get("run_id") == managed.control.id
                        and saved.get("kind") == "test_grant"
                        and self.assignment
                        and saved["context"]["task_id"] == self.assignment.id
                        and saved["context"]["member"] == self.name
                        and saved["state"] == "consumed"
                    ):
                        self.test_request = None
                        work.append("[Host fixed-test grant] " + message["content"])
                    continue
                if kind == "input_applied" and getattr(self.manager, "managed", None):
                    applied = self.manager.managed.control.store.runtime.get(
                        "run_input_applied", self.manager.managed.control.id, {}
                    )
                    if (
                        message["from"] == "lead"
                        and metadata.get("origin") == "managed_input"
                        and metadata.get("run_id") == self.manager.managed.control.id
                        and metadata.get("checkpoint") == applied.get("checkpoint")
                        and metadata.get("revision") == applied.get("revision")
                        and self.manager.managed.input_ready(self)
                    ):
                        self.test_request = None
                        self.status = "working"
                    continue
                if kind == "shutdown_request":
                    if (
                        request
                        and request.type == "shutdown"
                        and request.status == "pending"
                        and request.sender == "lead"
                        and request.target == self.name
                        and message["from"] == "lead"
                        and message["to"] == self.name
                    ):
                        self.status = "stopping"
                        self.manager.bus.send(
                            self.name,
                            "lead",
                            "Shutdown acknowledged.",
                            "shutdown_response",
                            {"request_id": request.request_id, "approve": True},
                        )
                        self.stop_event.set()
                        self.manager.persist_checkpoint(
                            self.name, self.inflight_messages
                        )
                        self.inflight_messages.clear()
                        return False
                    continue
                if kind == "plan_approval_response":
                    if (
                        request
                        and request.type == "plan_approval"
                        and request.sender == self.name
                        and request.target == "lead"
                        and request.request_id == self.plan_request
                        and request.work_version == self.version
                        and self.assignment is not None
                        and request.task_id == self.assignment.id
                        and request.status in {"approved", "rejected"}
                        and message["from"] == "lead"
                        and message["to"] == self.name
                        and type(metadata.get("approve")) is bool
                        and metadata["approve"] == (request.status == "approved")
                    ):
                        self.gate, self.plan_request = request.status, None
                        work.append(f"[Plan {self.gate}] {message['content']}")
                    continue
                if kind == "plan_request":
                    if message["from"] == "lead":
                        work.append("[Plan required] " + message["content"])
                    continue
                if (
                    kind == "user_steer"
                    and message["from"] == "lead"
                    and metadata.get("origin") == "host_user"
                ):
                    identity = metadata.get("input_id")
                    if not isinstance(identity, str) or identity in self.user_input_ids:
                        continue
                    self.user_input_ids.append(identity)
                    self.user_input_ids[:] = self.user_input_ids[-256:]
                    self.user_revision += 1
                    self.store.intent.update(message["content"])
                    if self.compactor is not None:
                        self.compactor.intent.update(message["content"])
                    work.append("[Host user steering] " + message["content"])
                    continue
                if kind == "message":
                    work.append(
                        f"[Message from {message['from']}] {message['content']}"
                    )
            if work:
                self.messages.append({"role": "user", "content": "\n".join(work)})
                self.status = "working"
        self.manager.persist_checkpoint(self.name, self.inflight_messages)
        self.inflight_messages.clear()
        return bool(work)

    def before_model(self) -> None:
        managed = getattr(self.manager, "managed", None)
        if managed:
            managed.before_model(self)
        sid = getattr(self.manager, "runtime_session_id", None)
        if sid is not None:
            # Accepted targeted input already invalidated this member's proposal;
            # wait for the Lead's atomic history + target delivery commit.
            while any(
                r.get("metadata", {}).get("target") == self.name
                and r["metadata"].get("mode") != "queued"
                for r in self.manager.bus.store.mailbox("inspect", "user_" + sid)
            ):
                from homebrew_harness.execution.execution_control import check

                check(cancel_events=(self.stop_event,))
                self.stop_event.wait(0.05)
        # Only human task intent is copied here; inbox observations cannot amend it.
        if self.manager.task_intent is not None:
            before = self.store.intent.to_dict()
            current = self.manager.task_intent
            for text in [current.original, *current.amendments]:
                if (
                    text
                    and text != self.store.intent.original
                    and text not in self.store.intent.amendments
                ):
                    self.store.intent.update(text)
            if self.store.intent.to_dict() != before:
                self.store.save_intent()
            inherited = self.store.intent.message()
            if inherited is not None:
                inherited["name"] = "inherited_task_constraints"
                set_context(
                    self.messages, "inherited_task_constraints", inherited, position=0
                )
        self.messages.extend(self.pending_context)
        self.pending_context.clear()
        self.handle_messages(self.manager.bus.claim(self.name))
        if getattr(self.manager, "managed", None):
            self.manager.managed.verify_before_model(self)

    def finish_work_turn(self, result: str) -> None:
        if getattr(self.manager, "managed", None):
            return self.manager.managed.complete_turn(self, result)
        with self.manager.lock:
            if self.gate == "pending":
                self.status = "waiting_approval"
                return
            task = self.store.load(self.assignment.id) if self.assignment else None
            if result.strip():
                self.manager.bus.send(
                    self.name,
                    "lead",
                    result,
                    "result",
                    {
                        "task_id": task.id if task else None,
                        "status": task.status if task else None,
                        "member_status": "idle",
                        "work_version": self.version,
                    },
                )
            if task is not None and task.status == "completed":
                self.assignment, self.assignment_path = None, None
                self.version += 1
                self.plan_request = None
                self.gate = "required" if self.require_plan else "not_required"
            if not result.strip():
                self.manager.bus.send(
                    self.name,
                    "lead",
                    "Waiting for more work.",
                    "idle_notification",
                    {
                        "task_id": self.assignment.id if self.assignment else None,
                        "work_version": self.version,
                    },
                )
            self.status = "idle"

    def run(self) -> None:
        try:
            while not self.stop_event.is_set():
                if self.status == "waiting_input" and getattr(
                    self.manager, "managed", None
                ):
                    if self.manager.managed.input_ready(self):
                        self.status = "working"
                    else:
                        from homebrew_harness.execution.execution_control import check

                        check(cancel_events=(self.stop_event,))
                        self.stop_event.wait(self.manager.scan_interval)
                        continue
                if self.status in {"idle", "waiting_approval"}:
                    inbox = self.manager.bus.wait_for_messages(
                        self.name, self.manager.scan_interval, durable=True
                    )
                    if inbox:
                        if not self.handle_messages(inbox):
                            continue
                    elif self.status == "idle" and self.assignment is None:
                        try:
                            with self.manager.lock:
                                # Check control messages once more before board discovery.
                                if (
                                    self.manager.bus.peek(self.name)
                                    or self.stop_event.is_set()
                                    or self.manager.lead_busy
                                ):
                                    continue
                                task = self.store.claim_next(self.allowed_tasks)
                                if task:
                                    self.bind(task, automatic=True)
                                    self.status = "working"
                        except ValueError:
                            continue  # A competing claimant or broken binding cannot change cwd.
                        if task is None:
                            continue
                    else:
                        continue
                sid = getattr(self.manager, "runtime_session_id", "standalone")
                writer = CheckpointWriter(
                    self.manager.bus.store,
                    "member:" + sid + ":" + self.name,
                    lambda: {
                        "session_id": sid,
                        "messages": self.messages,
                        "intent": self.store.intent.to_dict(),
                        "team": self.manager.snapshot_for_storage(),
                    },
                )
                if getattr(self.manager, "managed", None):
                    writer.run_control = self.manager.managed.checkpoint_control(self)
                with checkpoint_scope(writer):
                    try:
                        result = self.manager.run_member(self)
                    except Exception as error:
                        from homebrew_harness.storage.control_boundary import (
                            ProposalSuperseded,
                        )

                        if isinstance(error, ProposalSuperseded) and getattr(
                            self.manager, "managed", None
                        ):
                            if self.manager.managed.unknown(self.name):
                                self.manager.managed.record_outcome(
                                    self, "blocked", "inspection_required"
                                )
                            else:
                                self.manager.managed.record_outcome(
                                    self,
                                    "waiting_input",
                                    "input_changed",
                                    wait_on={
                                        "type": "run_input",
                                        "run_id": self.manager.managed.control.id,
                                    },
                                )
                            continue
                        raise
                    if not self.stop_event.is_set():
                        self.finish_work_turn(result)
        except Exception as error:
            if not self.stop_event.is_set():
                from homebrew_harness.storage.control_boundary import ProposalSuperseded

                self.failure_kind = (
                    "superseded"
                    if isinstance(error, ProposalSuperseded)
                    else getattr(error, "kind", type(error).__name__)
                )
                self.status = "failed"
                try:
                    self.manager.bus.send(
                        self.name,
                        "lead",
                        f"Teammate stopped ({type(error).__name__}); verify existing files before resuming.",
                        "error",
                    )
                except Exception:
                    pass
        finally:
            with self.manager.lock.lock:
                try:
                    task = self.store.owned()
                    if task:
                        self.store.release(task.id)
                except Exception as error:
                    try:
                        self.manager.bus.send(
                            self.name,
                            "lead",
                            f"Assignment cleanup failed ({type(error).__name__}).",
                            "error",
                        )
                    except Exception:
                        pass
                if getattr(self.manager, "managed", None) and self.status != "failed":
                    self.status = "stopped"
                try:
                    self.manager.persist_checkpoint()
                except Exception as error:
                    self.manager.cleanup_errors.append(
                        {
                            "component": "member_checkpoint",
                            "error_type": type(error).__name__,
                        }
                    )
                finally:
                    self.store.close()
                self.assignment, self.assignment_path, self.plan_request = (
                    None,
                    None,
                    None,
                )
                self.version += 1
                if self.status != "failed":
                    self.status = "stopped"
                if (
                    self.manager.session_owner is None
                    and self.manager.closed
                    and self.manager.lead_busy == 0
                    and not any(
                        item.thread
                        and item.thread is not threading.current_thread()
                        and item.thread.is_alive()
                        for item in self.manager.members.values()
                    )
                ):
                    self.manager.release_session()


from homebrew_harness.orchestration.workspace_runtime import WorkspaceRuntime
from homebrew_harness.runtime.runtime_control import WorkAccepted, Workers


class TeamManager(WorkspaceRuntime):
    def __init__(
        self,
        workdir: Path,
        run_member,
        *,
        max_members: int = 8,
        scan_interval: float = 0.2,
        background_manager=None,
        bash_policy=None,
        worktrees=True,
    ):
        super().__init__(
            workdir,
            tasks=True,
            worktrees=worktrees,
            background_manager=background_manager,
            bash_policy=bash_policy,
        )
        self.capabilities = self.capabilities.include("teams", "subagents")
        self.run_member, self.max_members, self.scan_interval = (
            run_member,
            max_members,
            scan_interval,
        )
        self.model_selector = None
        self._actors = Workers()
        self._spawn_approvals = []
        self.approval_history = []
        self.members: dict[str, TeammateRuntime] = {}
        self.protocols: dict[str, ProtocolState] = {}
        self.lead_assignment: Task | None = None
        self.task_intent = None
        self._session_leased = False
        self.model_pending = lambda: False
        self.test_runner = None
        self.closed = False
        self.lead_busy = 0
        self.background_manager = background_manager
        self.runtime_session_id = "standalone"
        self.recovered_members = {}
        self.cleanup_errors = []
        self.snapshot_revision = 0

    def checkpoint(self, *, copy_messages=True):
        with self.lock.lock:
            if not hasattr(self, "_snapshot_signatures"):
                self._snapshot_signatures = {}
                self._team_signature = None
            members = (
                copy.deepcopy(self.recovered_members)
                if copy_messages
                else dict(self.recovered_members)
            )
            for m in self.members.values():
                if not isinstance(m, TeammateRuntime):
                    continue
                item = {
                    "name": m.name,
                    "role": m.role,
                    "user_revision": m.user_revision,
                    "user_input_ids": list(m.user_input_ids),
                    "model_binding": m.model_binding.identity()
                    if m.model_binding
                    else None,
                    "messages": m.messages,
                    "pending_context": copy.deepcopy(m.pending_context),
                    "status": m.status,
                    "failure_kind": m.failure_kind,
                    "last_outcome": m.last_outcome,
                    "gate": m.gate,
                    "require_plan": m.require_plan,
                    "plan_request": m.plan_request,
                    "version": m.version,
                    "allowed_tasks": sorted(m.allowed_tasks),
                    "task_id": m.assignment.id if m.assignment else None,
                    "intent": m.store.intent.to_dict(),
                    "archives": m.compactor.store.checkpoint() if m.compactor else None,
                    "todo": [
                        {"content": i.content, "status": i.status}
                        for i in m.todo_manager.items
                    ]
                    if m.todo_manager
                    else [],
                }
                signature = (
                    json.dumps(
                        {
                            key: value
                            for key, value in item.items()
                            if key != "messages"
                        },
                        sort_keys=True,
                    ),
                    tuple(encoded_message(message)[1] for message in m.messages),
                )
                if signature != self._snapshot_signatures.get(m.name):
                    m.snapshot_revision += 1
                    self._snapshot_signatures[m.name] = signature
                item["revision"] = m.snapshot_revision
                members[m.name] = copy.deepcopy(item) if copy_messages else item
            protocols = [asdict(p) for p in self.protocols.values()]
            signature = (
                json.dumps(protocols, sort_keys=True),
                tuple(
                    sorted(
                        (name, value.get("revision", 0))
                        for name, value in members.items()
                    )
                ),
            )
            if signature != self._team_signature:
                self.snapshot_revision += 1
                self._team_signature = signature
            return {
                "revision": self.snapshot_revision,
                "protocols": protocols,
                "members": members,
            }

    def snapshot_for_storage(self):
        return self.checkpoint(copy_messages=False)

    def persist_checkpoint(self, ack_name=None, records=()):
        snapshot = self.checkpoint()
        self.bus.store.team_checkpoint(
            self.runtime_session_id,
            snapshot,
            ack_name=ack_name,
            owner=self.bus.consumer,
            records=records,
        )

    def restore_checkpoint(self, session_id):
        from homebrew_harness.storage.checkpoint_lifecycle import require_retained

        require_retained(self.bus.store.get("session_meta", session_id))
        saved = self.bus.store.get("team", session_id, {})
        self.snapshot_revision = saved.get("revision", 0)
        self._team_signature = (
            json.dumps(saved.get("protocols", []), sort_keys=True),
            tuple(
                sorted(
                    (name, value.get("revision", 0))
                    for name, value in saved.get("members", {}).items()
                )
            ),
        )
        self.protocols = {
            p["request_id"]: ProtocolState(**p) for p in saved.get("protocols", [])
        }
        # Restored protocol data is reference. New member execution needs fresh host spawn approval.
        for protocol in self.protocols.values():
            if protocol.status == "pending":
                protocol.status = "stale"
        self.recovered_members = saved.get("members", {})
        for member in self.recovered_members.values():
            member["messages"] = ConversationHistory(member.get("messages", []))
        self.runtime_session_id = session_id

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def new_request(
        self, kind: str, sender: str, target: str, **kwargs
    ) -> ProtocolState:
        while True:
            key = "req_" + secrets.token_hex(6)
            if key not in self.protocols:
                state = ProtocolState(key, kind, sender, target, "pending", **kwargs)
                self.protocols[key] = state
                self.persist_checkpoint()
                return state

    def authorize_spawn(self, arguments, *, task_ids=()):
        """Host-only approval of one exact invocation, consumed once."""
        approved = {
            "task_id": None,
            "require_plan": False,
            "model_profile": None,
            **arguments,
        }
        with self.lock:
            if isinstance(task_ids, str):
                raise ValueError("Host task scope requires a collection of task IDs.")
            allowed = frozenset(TaskStore._id(identity) for identity in task_ids)
            if approved["task_id"] is not None:
                allowed |= {TaskStore._id(approved["task_id"])}
            for identity in allowed:
                self.store.load(identity)
            binding = self._select_model(approved["role"], approved["model_profile"])
            self._spawn_approvals.append(
                (approved, allowed, binding.identity() if binding else None)
            )

    def _select_model(self, role, alias):
        if self.model_selector is None:
            if alias is not None:
                raise ValueError("Model selection requires a host model registry.")
            return None
        return self.model_selector(role, alias)

    def spawn(
        self,
        name: str,
        role: str,
        prompt: str,
        task_id: str | None = None,
        require_plan: bool = False,
        model_profile: str | None = None,
    ) -> dict:
        validate_agent_name(name, teammate=True)
        if (
            not all(isinstance(text, str) and text.strip() for text in (role, prompt))
            or type(require_plan) is not bool
        ):
            raise ValueError(
                "Teammate role/prompt must be nonempty and require_plan must be boolean."
            )
        if getattr(self, "managed", None):
            self.managed.prepare_member(name)
        with self.lock:
            self.acquire_session()
            requested = dict(
                name=name,
                role=role,
                prompt=prompt,
                task_id=task_id,
                require_plan=require_plan,
                model_profile=model_profile,
            )
            approval = next(
                (entry for entry in self._spawn_approvals if entry[0] == requested),
                None,
            )
            if approval is None:
                raise ValueError(
                    "approval_required: the host must approve this exact teammate invocation."
                )
            binding = self._select_model(role, model_profile)
            if approval[2] != (binding.identity() if binding else None):
                raise ValueError(
                    "Model binding changed after spawn approval; obtain fresh host approval."
                )
            saved = self.recovered_members.get(name)
            if saved and saved.get("model_binding") != (
                binding.identity() if binding else None
            ):
                raise RecoveryRequired(
                    "Member resume requires its saved model binding; use a new member identity for another model."
                )
            self._spawn_approvals.remove(approval)
            self.approval_history.append(
                {
                    "name": name,
                    "task_id": task_id,
                    "task_scope": sorted(approval[1]),
                    "instance": self.leases.instance,
                    "model_binding": binding.identity() if binding else None,
                }
            )
            if self.closed or name in self.members:
                raise ValueError(
                    "Team is closed or this identity has already been used in the session."
                )
            if (
                sum(
                    item.status not in {"stopped", "failed"}
                    for item in self.members.values()
                )
                >= self.max_members
            ):
                raise ValueError("Team member limit reached.")
            if getattr(self, "managed", None):
                self.managed.before_spawn(requested)
            member = TeammateRuntime(self, name, role, prompt, require_plan)
            member.model_binding = binding
            member.allowed_tasks = approval[1]
            resumed = member.store.owned()
            if resumed is not None and resumed.id not in member.allowed_tasks:
                raise ValueError(
                    "Resumed assignment requires fresh host approval of its task ID."
                )
            claimed = False
            if task_id is not None:
                if resumed is not None and resumed.id != task_id:
                    raise ValueError("Teammate already owns another unfinished task.")
                record = member.store.load(task_id)
                task = resumed or (
                    member.store.resume(task_id)
                    if record.status == "in_progress" and record.owner == name
                    else member.store.claim(task_id)
                )
                claimed = resumed is None
            else:
                task = resumed
            try:
                if task:
                    member.bind(task)
                saved = self.recovered_members.get(name)
                if saved and saved["role"] == role and saved.get("task_id") == task_id:
                    key = "member:" + self.runtime_session_id + ":" + name
                    history = self.bus.store.get("checkpoints", key, saved).get(
                        "messages", []
                    )
                    ops = list(self.bus.store.items("operations", owner=key).values())
                    member.messages = ConversationHistory(
                        repair_history(history, ops)[0]
                    )
                    member.messages.append(
                        {"role": "user", "content": "[Host resumed member] " + prompt}
                    )
                    member.last_outcome = saved.get("last_outcome")
                    if member.last_outcome and saved.get("status") in {
                        "idle",
                        "waiting_input",
                    }:
                        member.status = saved["status"]
                    member.snapshot_revision = saved.get("revision", 0)
                    member.user_revision = saved.get("user_revision", 0)
                    member.user_input_ids = saved.get("user_input_ids", [])
                    member.store.intent.restore(
                        saved.get("intent", member.store.intent.to_dict())
                    )
                    member.restored_archives = saved.get("archives")
                    member.pending_context = saved.get("pending_context", [])
                    member.version = max(member.version, saved.get("version", 0)) + 1
                    member.gate = (
                        "required"
                        if require_plan or saved.get("require_plan")
                        else "not_required"
                    )
                    member.plan_request = None
                    from homebrew_harness.context.todo import TodoManager

                    member.todo_manager = TodoManager()
                    member.todo_manager.update(saved.get("todo", []))
                self.bus.recover(name)
                self.members[name] = member
                self.persist_checkpoint()
                self._actors.start(
                    member.run,
                    name="teammate-" + name,
                    on_created=lambda thread: setattr(member, "thread", thread),
                )
            except WorkAccepted:
                raise
            except Exception:
                self.members.pop(name, None)
                if claimed:
                    member.store.release(task.id)
                raise
            return {
                "name": name,
                "status": "working",
                "task_id": task.id if task else None,
                "message": "Teammate started. End this turn; runtime delivers result and idle events.",
            }

    def _member(self, name: str) -> TeammateRuntime:
        member = self.members.get(name)
        if member is None or member.status in {"stopped", "failed"}:
            raise ValueError("Teammate is not active.")
        return member

    def send(self, sender: str, to: str, content: str) -> str:
        with self.lock:
            if sender != "lead":
                self._member(sender)
            if to != "lead":
                self._member(to)
            if not isinstance(content, str) or not content.strip():
                raise ValueError("Message content must be nonempty.")
            self.bus.send(sender, to, content)
        return json.dumps({"sent": True, "from": sender, "to": to})

    def request_shutdown(self, teammate: str) -> dict:
        with self.lock:
            self._member(teammate)
            prior = next(
                (
                    item
                    for item in self.protocols.values()
                    if item.type == "shutdown"
                    and item.target == teammate
                    and item.status == "pending"
                ),
                None,
            )
            if prior:
                return asdict(prior)
            request = self.new_request("shutdown", "lead", teammate)
            try:
                self.bus.send(
                    "lead",
                    teammate,
                    "Finish the current step and shut down.",
                    "shutdown_request",
                    {"request_id": request.request_id},
                )
            except Exception:
                self.protocols.pop(request.request_id)
                raise
            return asdict(request)

    def request_plan(self, teammate: str, task: str) -> dict:
        with self.lock:
            member = self._member(teammate)
            if (
                member.active_tool
                and getattr(member, "active_thread", None) != threading.get_ident()
            ):
                raise ValueError(
                    "Teammate is executing a tool; retry plan changes after the step completes."
                )
            self.bus.send("lead", teammate, task, "plan_request")
            if member.plan_request:
                self.protocols[member.plan_request].status = "stale"
            member.plan_request = None
            member.require_plan, member.gate = True, "required"
            return {"teammate": teammate, "plan_status": "required"}

    def review_plan(self, request_id: str, approve: bool, feedback: str = "") -> dict:
        if type(approve) is not bool:
            raise ValueError("approve must be boolean.")
        with self.lock:
            request = self.protocols.get(request_id)
            if (
                request is None
                or request.type != "plan_approval"
                or request.status != "pending"
            ):
                raise ValueError("Request is not a pending plan approval.")
            member = self._member(request.sender)
            if (
                member.active_tool
                and getattr(member, "active_tool_name", None) != "submit_plan"
                and getattr(member, "active_thread", None) != threading.get_ident()
            ):
                raise ValueError(
                    "Teammate is executing a tool; retry plan review after the step completes."
                )
            if (
                member.assignment is None
                or member.assignment.id != request.task_id
                or member.version != request.work_version
                or member.plan_request != request_id
            ):
                raise ValueError("Plan belongs to an earlier task or work version.")
            outcome = "approved" if approve else "rejected"
            content = feedback or (
                "Plan approved." if approve else "Revise and submit the plan again."
            )
            self.bus.send(
                "lead",
                member.name,
                content,
                "plan_approval_response",
                {"request_id": request_id, "approve": approve},
            )
            request.status = outcome
            return asdict(request)

    def consume_lead_inbox(self) -> list[dict]:
        inbox = self.bus.claim("lead")
        with self.lock:
            for message in inbox:
                if message["type"] != "shutdown_response":
                    continue
                meta = message["metadata"]
                request_id = meta.get("request_id")
                request = (
                    self.protocols.get(request_id)
                    if isinstance(request_id, str)
                    else None
                )
                if (
                    request
                    and request.type == "shutdown"
                    and request.status == "pending"
                    and message["from"] == request.target
                    and message["to"] == request.sender
                    and type(meta.get("approve")) is bool
                ):
                    request.status = "approved" if meta["approve"] else "rejected"
        return inbox

    def handlers(self) -> dict:
        return {
            **{
                name: (
                    lambda _name=name, **arguments: self.run_tool(_name, **arguments)
                )
                for name in TEAM_TOOL_NAMES
                if name != "create_worktree"
            },
            **super().handlers(),
        }

    def run_tool(self, tool_name: str, *, workdir: Path, **arguments) -> str:
        if workdir.resolve() != self.workdir:
            raise ValueError("Team tools are bound to the base workspace.")
        if tool_name == "spawn_teammate":
            result = self.spawn(**arguments)
        elif tool_name == "list_teammates":
            with self.lock:
                result = {
                    "teammates": [
                        {
                            "name": item.name,
                            "role": item.role,
                            "model_binding": item.model_binding.identity()
                            if item.model_binding
                            else None,
                            "status": item.status,
                            "task_id": item.assignment.id if item.assignment else None,
                            "plan_status": item.gate,
                        }
                        for item in self.members.values()
                    ]
                }
        elif tool_name == "send_message":
            return self.send("lead", **arguments)
        elif tool_name == "request_shutdown":
            result = self.request_shutdown(**arguments)
        elif tool_name == "request_plan":
            result = self.request_plan(**arguments)
        elif tool_name == "review_plan":
            result = self.review_plan(**arguments)
        elif tool_name == "create_worktree":
            return self.create_worktree(workdir=workdir, **arguments)
        else:
            raise ValueError("Unknown team tool.")
        return json.dumps(result, ensure_ascii=False)

    def remove_worktree(self, name: str, *, discard_changes: bool = False) -> dict:
        with self.lock:
            leased = [
                item.assignment_path
                for item in self.members.values()
                if item.assignment_path
            ]
            if self.lead_assignment:
                leased.append(self.lead_cwd())
            if self.background_manager:
                leased.extend(self.background_manager.running_paths())
            return self.worktrees.remove(
                name, leased_paths=leased, discard_changes=discard_changes
            )

    def wait_closed(self, timeout=5):
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        for member in list(self.members.values()):
            if member.thread and member.thread is not threading.current_thread():
                member.thread.join(
                    None if deadline is None else max(0, deadline - time.monotonic())
                )
                if member.thread.is_alive():
                    return False
        return True

    def close(self, timeout: float = 2) -> None:
        deadline = time.monotonic() + max(0, timeout)
        self.shell_runner.close(timeout=max(0, deadline - time.monotonic()))
        acquired = self.lock.acquire(timeout=max(0, deadline - time.monotonic()))
        try:
            self.closed = True
            members = list(self.members.values())
            for member in members:
                member.stop_event.set()
            self.bus.wake()
        finally:
            if acquired:
                self.lock.release()
        for member in members:
            if member.thread and member.thread is not threading.current_thread():
                member.thread.join(timeout=max(0, deadline - time.monotonic()))
            if member.thread is None or not member.thread.is_alive():
                member.store.close()
        if self.lead_busy == 0 and not self.model_pending():
            self.store.close()
        # Live workers retain their task/session locks until their finally block.
        if (
            self.session_owner is None
            and self.lead_busy == 0
            and not any(
                member.thread and member.thread.is_alive() for member in members
            )
        ):
            self.release_session()
