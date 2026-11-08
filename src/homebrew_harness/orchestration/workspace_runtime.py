"""Shared workspace ownership and optional task routing, without team actors."""

import json
from pathlib import Path

from homebrew_harness.execution.execution_control import (
    ControlledRLock,
    acquired,
    operation_deadline,
)
from homebrew_harness.execution.execution_leases import ExecutionLeases
from homebrew_harness.execution.model_gateway import Deadline
from homebrew_harness.orchestration.task_store import TaskStore
from homebrew_harness.runtime.capabilities import Capabilities
from homebrew_harness.storage.message_bus import MessageBus


class WorkspaceRuntime:
    def __init__(
        self,
        workdir,
        *,
        tasks=False,
        worktrees=False,
        background_manager=None,
        bash_policy=None,
    ):
        self.workdir = Path(workdir).resolve()
        self.lock = ControlledRLock()
        self.bus = MessageBus(self.workdir)
        self.leases = ExecutionLeases(self.workdir)
        self.store = (
            TaskStore(self.workdir, leases=self.leases) if tasks or worktrees else None
        )
        from homebrew_harness.orchestration.worktrees import WorktreeManager

        self.worktrees = (
            WorktreeManager(self.workdir, self.store) if worktrees else None
        )
        self.lead_assignment = None
        self.lead_busy = 0
        self._session_leased = False
        self.closed = False
        self.cleanup_errors = []
        from homebrew_harness.execution.bash_policy import current_bash_policy
        from homebrew_harness.execution.bash_runner import make_shell_runner

        self.bash_policy = bash_policy or current_bash_policy()
        self.shell_runner = make_shell_runner(self.bash_policy)
        self.capabilities = Capabilities(
            ({"tasks"} if tasks else set()) | ({"worktrees"} if worktrees else set())
        )
        self.model_pending = lambda: not self.shell_runner.wait_closed(0)
        self.background_manager = background_manager
        self.runtime_session_id = "standalone"
        self.session_owner = None

    def acquire_session(self, deadline=None):
        with acquired(
            self.lock,
            Deadline(deadline) if deadline is not None else operation_deadline(120),
        ):
            if self.closed:
                raise ValueError("Workspace runtime is closed.")
            if self._session_leased:
                return
            self.leases.acquire("session")
            self._session_leased = True

    def release_session(self):
        with self.lock.lock:
            if self._session_leased and not self.model_pending():
                self.leases.release("session")
                self._session_leased = False

    def begin_lead_turn(self):
        self.acquire_session()
        with self.lock:
            self.lead_busy += 1

    def end_lead_turn(self):
        with self.lock.lock:
            self.lead_busy = max(0, self.lead_busy - 1)
            if (
                self.store
                and self.lead_assignment
                and self.store.load(self.lead_assignment.id).status == "completed"
            ):
                self.lead_assignment = None
            if self.closed and self.lead_busy == 0 and not self.model_pending():
                if self.store:
                    self.store.close()
                if self.session_owner is None and self.wait_closed(0):
                    self.release_session()

    def lead_cwd(self):
        with self.lock:
            if self.store and self.lead_assignment is None:
                self.lead_assignment = self.store.owned()
            if self.lead_assignment is None:
                return self.workdir
            task = self.store.load(self.lead_assignment.id)
            if task.status == "in_progress":
                self.store.assert_execution(task.id)
            if task.owner != self.store.owner or task.status not in {
                "in_progress",
                "completed",
            }:
                raise ValueError("Lead assignment no longer valid.")
            return self.store.workspace(task)

    def background_assignment(self):
        self.lead_cwd()
        if self.lead_assignment is None:
            return None
        task = self.store.load(self.lead_assignment.id)
        if task.status != "in_progress":
            raise ValueError(
                "Do not start background work after completing the assigned task."
            )
        return {"task_id": task.id, "owner": task.owner, "worktree": task.worktree}

    def lead_claim(self, task_id, *, workdir):
        if self.store.stage_control:
            self.store.stage_control.prepare_claim(task_id)
        with self.lock:
            if self.lead_assignment:
                raise ValueError("Finish the current Lead task first.")
            self.lead_assignment = self.store.claim(task_id)
            return json.dumps(self.lead_assignment.to_dict())

    def lead_resume(self, task_id, *, workdir):
        with self.lock:
            if self.lead_assignment and self.lead_assignment.id != task_id:
                raise ValueError("Finish the current Lead task first.")
            self.lead_assignment = self.store.resume(task_id)
            return json.dumps(self.lead_assignment.to_dict())

    def lead_complete(self, task_id, *, workdir, summary="", next_steps=""):
        with self.lock:
            if self.background_manager and self.background_manager.task_running(
                task_id
            ):
                raise ValueError("Assigned background work is still running.")
            result = self.store.run_tool(
                "complete_task",
                workdir=workdir,
                task_id=task_id,
                summary=summary,
                next_steps=next_steps,
            )
            if self.lead_assignment and self.lead_assignment.id == task_id:
                self.lead_assignment = None
            return result

    def handlers(self):
        handlers = (
            {
                "claim_task": self.lead_claim,
                "resume_task": self.lead_resume,
                "complete_task": self.lead_complete,
            }
            if self.store
            else {}
        )
        if self.worktrees is not None:
            handlers["create_worktree"] = self.create_worktree
        return handlers

    def create_worktree(self, name, task_id, *, workdir):
        if self.worktrees is None or workdir.resolve() != self.workdir:
            raise ValueError("Worktree creation requires its enabled base workspace.")
        return self.worktrees.run_tool(name=name, task_id=task_id, workdir=workdir)

    def consume_lead_inbox(self):
        return self.bus.claim("lead")

    def wait_closed(self, timeout=5):
        return True

    def close(self, timeout=2):
        self.closed = True
        self.shell_runner.close(timeout=timeout)
        if self.store:
            self.store.close()
        if self.session_owner is None:
            self.release_session()
