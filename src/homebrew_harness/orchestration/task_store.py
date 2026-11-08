"""s10: persistent tasks, dependency validation, and host-owned transitions."""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from homebrew_harness.context.task_intent import TaskIntent
from homebrew_harness.execution.execution_leases import ExecutionLeases
from homebrew_harness.security.data_boundary import redact
from homebrew_harness.security.safe_files import SafeFile

TASK_TERMINAL = {"completed", "abandoned"}

TASK_ID_PATTERN = re.compile(r"task_[0-9a-f]{8}")
TASK_TOOL_NAMES = (
    "create_task",
    "update_task",
    "list_tasks",
    "get_task",
    "claim_task",
    "complete_task",
    "resume_task",
)
_STORE_LOCKS: dict[Path, threading.RLock] = {}
_STORE_LOCK_GUARD = threading.Lock()
_STORE_DEPTHS = threading.local()


@dataclass(frozen=True)
class Task:
    id: str
    subject: str
    description: str
    status: str
    owner: str | None
    blockedBy: tuple[str, ...]
    worktree: str | None = None
    task_intent: dict | None = None
    stage: dict | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["blockedBy"] = list(self.blockedBy)
        return data


class TaskStore:
    """SQLite task records; no model-supplied paths or owner identities."""

    def __init__(self, workdir: Path, *, owner: str = "agent", leases=None):
        self.workdir = workdir.resolve()
        self.directory = self.workdir / ".harness/runtime"
        from homebrew_harness.storage.durable_runtime import DurableStore

        self.runtime = DurableStore(self.workdir)
        self.stage_control = None
        self.owner = self._text(owner, "owner")
        self.leases = leases or ExecutionLeases(self.workdir)
        self.instance = self.leases.instance
        self._executions = {}
        self.intent = None
        with _STORE_LOCK_GUARD:
            self._thread_lock = _STORE_LOCKS.setdefault(self.workdir, threading.RLock())

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    @staticmethod
    def _text(value, name: str, *, empty: bool = False) -> str:
        if not isinstance(value, str) or (not empty and not value.strip()):
            raise ValueError(
                f"Task {name} must be a {'string' if empty else 'nonempty string'}."
            )
        return value

    @staticmethod
    def _id(task_id: str) -> str:
        if not isinstance(task_id, str) or not TASK_ID_PATTERN.fullmatch(task_id):
            raise ValueError("Invalid task ID; use an ID returned by create_task.")
        return task_id

    def _root(self, *, create: bool = False) -> Path:
        # Check before mkdir so an existing symlink cannot create outside files.
        if self.directory.is_symlink() or self.directory.resolve() != self.directory:
            raise ValueError(
                "Task directory must not be a symlink or escape the workspace."
            )
        if create:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.exists() and not self.directory.is_dir():
            raise ValueError("Task directory must be a directory.")
        return self.directory

    @contextmanager
    def _lock(self):
        from homebrew_harness.execution.execution_control import acquired, check
        from homebrew_harness.storage.storage_gate import storage_gate

        self._root()
        with storage_gate(self.workdir), acquired(self._thread_lock):
            depths = getattr(_STORE_DEPTHS, "values", {})
            _STORE_DEPTHS.values = depths
            if depths.get(self.workdir, 0):
                check()
                yield
                return
            with self._file_lock():
                depths[self.workdir] = 1
                try:
                    check()
                    yield
                finally:
                    depths.pop(self.workdir, None)

    @contextmanager
    def _file_lock(self):
        self._root(create=True)
        if (self.directory / "task-board.lock").is_symlink():
            raise ValueError("Task lock must not be a symlink.")
        with SafeFile(
            self.workdir, ".harness/runtime/task-board.lock", create_parents=True
        ) as target:
            fd = os.open(
                "task-board.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
                dir_fd=target.fds[-1],
            )
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ValueError(
                        "Task store is busy; retry this tool call."
                    ) from None
                target.verify_binding()
                yield
            finally:
                os.close(fd)

    def _load(self, task_id: str) -> Task:
        data = self.runtime.get("tasks", self._id(task_id))
        if data is None:
            raise ValueError(f"Task not found: {task_id}")
        fields = {
            "id",
            "subject",
            "description",
            "status",
            "owner",
            "blockedBy",
            "worktree",
            "task_intent",
            "stage",
        }
        if not isinstance(data, dict) or set(data) != fields:
            raise ValueError(f"Invalid task fields: {task_id}")
        if data.get("stage") is not None:
            from homebrew_harness.runs.stages import validate_task_stage

            validate_task_stage(data["stage"])
        if data.get("task_intent") is not None:
            TaskIntent().restore(data["task_intent"])
        worktree = data.get("worktree")
        if worktree is not None:
            from homebrew_harness.orchestration.worktrees import validate_worktree_name

            validate_worktree_name(worktree)
        if data["id"] != task_id:
            raise ValueError(f"Task file ID does not match {task_id}.")
        self._text(data["subject"], "subject")
        self._text(data["description"], "description", empty=True)
        if data["status"] not in {"pending", "in_progress", *TASK_TERMINAL}:
            raise ValueError(f"Invalid task status: {task_id}")
        if data["status"] in {"pending", "abandoned"}:
            if data["owner"] is not None:
                raise ValueError(f"Pending task must be unowned: {task_id}")
        else:
            self._text(data["owner"], "owner")
        edges = data["blockedBy"]
        if not isinstance(edges, list):
            raise ValueError(f"Task dependencies must be an array: {task_id}")
        for dependency in edges:
            self._id(dependency)
            if dependency == task_id:
                raise ValueError(f"Task cannot depend on itself: {task_id}")
        if len(set(edges)) != len(edges):
            raise ValueError(f"Task has duplicate dependencies: {task_id}")
        return Task(**{**data, "blockedBy": tuple(edges)})

    def _list(self) -> list[Task]:
        return [
            self._load(identity) for identity in sorted(self.runtime.items("tasks"))
        ]

    def _write(self, task: Task, *, create: bool = False) -> None:
        db = self.runtime.backend()
        with db.write() as conn:
            if create and db._get(conn, "tasks", task.id) is not None:
                raise FileExistsError("Task ID already exists")
            db._put(conn, "tasks", task.id, task.to_dict())

    def _stage_guard(self, task, *, dependencies=True):
        if task.stage is not None:
            if self.stage_control is None:
                raise ValueError("Managed stage requires its Run execution controller.")
            self.stage_control.guard(task, dependencies=dependencies)

    def create(self, subject: str, description: str = "") -> Task:
        subject = self._text(subject, "subject").strip()
        description = self._text(description, "description", empty=True)
        with self._lock():
            for _ in range(100):
                task = Task(
                    f"task_{secrets.token_hex(4)}",
                    subject,
                    description,
                    "pending",
                    None,
                    (),
                )
                try:
                    self._write(task, create=True)
                    return task
                except FileExistsError:
                    continue
        raise ValueError("Could not allocate a unique task ID.")

    def load(self, task_id: str) -> Task:
        self._id(task_id)
        if not self._root().exists():
            raise ValueError(f"Task not found: {task_id}")
        with self._lock():
            return self._load(task_id)

    def list(self) -> list[Task]:
        if not self._root().exists():
            return []
        with self._lock():
            return self._list()

    def _depends_on(self, task_id: str, target_id: str) -> bool:
        pending, visited = [task_id], set()
        while pending:
            current = pending.pop()
            if current == target_id:
                return True
            if current in visited:
                continue
            visited.add(current)
            pending.extend(self._load(current).blockedBy)
        return False

    def update_dependencies(self, task_id: str, add_blocked_by: list[str]) -> Task:
        self._id(task_id)
        if not isinstance(add_blocked_by, list):
            raise ValueError("addBlockedBy must be an array of task IDs.")
        for dependency in add_blocked_by:
            self._id(dependency)
        with self._lock():
            task = self._load(task_id)
            if task.stage is not None:
                raise ValueError("Managed stage dependencies are host-selected.")
            if task.status != "pending" or task.owner is not None:
                raise ValueError(
                    "Dependencies can only be updated while pending and unowned."
                )
            edges = list(task.blockedBy)
            # Validate the entire proposal before publishing any of its edges.
            for dependency in dict.fromkeys(add_blocked_by):
                if dependency == task_id:
                    raise ValueError("Task cannot depend on itself.")
                self._load(dependency)
                if self._depends_on(dependency, task_id):
                    raise ValueError("Dependency cycle detected.")
                if dependency not in edges:
                    edges.append(dependency)
            updated = replace(task, blockedBy=tuple(edges))
            self._write(updated)
            return updated

    def _incomplete(
        self, task: Task, records: dict[str, Task] | None = None
    ) -> list[str]:
        incomplete = []
        for dependency in task.blockedBy:
            try:
                record = (
                    records.get(dependency)
                    if records is not None
                    else self._load(dependency)
                )
                if record is None or record.status != "completed":
                    incomplete.append(dependency)
            except (OSError, ValueError):
                # Missing or invalid predecessors never count as completed.
                incomplete.append(dependency)
        return incomplete

    def claim(self, task_id: str) -> Task:
        self._id(task_id)
        if self.stage_control is not None:
            self.stage_control.prepare_claim(task_id)
        with self._lock():
            return self._claim(self._load(task_id))

    def _claim(self, task: Task) -> Task:
        self._stage_guard(task)
        if task.status != "pending":
            raise ValueError(f"Task {task.id} is {task.status}; cannot claim.")
        blockers = self._incomplete(task)
        if blockers:
            raise ValueError("Task is blocked by: " + ", ".join(blockers))
        if any(
            item.status == "in_progress" and item.owner == self.owner
            for item in self._list()
        ):
            raise ValueError(
                "Owner must complete its current task before claiming another."
            )
        self._acquire_execution(task)
        intent = (
            redact(self.intent.to_dict())
            if self.intent is not None and self.intent.original
            else task.task_intent
        )
        claimed = replace(
            task, status="in_progress", owner=self.owner, task_intent=intent
        )
        try:
            self._write(claimed)
        except BaseException:
            self._release_execution(task.id)
            raise
        return claimed

    def workspace(self, task: Task) -> Path:
        if task.worktree is None:
            return self.workdir
        from homebrew_harness.orchestration.worktrees import WorktreeManager

        return WorktreeManager(self.workdir, self).resolve(task.worktree)

    def _acquire_execution(self, task):
        resources = (
            "owner:" + self.owner,
            "task:" + task.id,
            "workspace:" + str(self.workspace(task)),
        )
        acquired = []
        try:
            for resource in resources:
                self.leases.acquire(resource)
                acquired.append(resource)
        except BaseException:
            for resource in reversed(acquired):
                self.leases.release(resource)
            raise
        self._executions[task.id] = resources

    def _release_execution(self, task_id):
        for resource in reversed(self._executions.pop(task_id, ())):
            self.leases.release(resource)

    def assert_execution(self, task_id):
        if task_id not in self._executions:
            raise ValueError(
                "This instance has no execution lease; explicitly resume after the old host stops."
            )

    def resume(self, task_id: str) -> Task:
        """Explicit takeover; OS locks, not an expired timestamp, prove inactivity."""
        with self._lock():
            task = self._load(task_id)
            self._stage_guard(task, dependencies=False)
            if task.status != "in_progress" or task.owner != self.owner:
                raise ValueError(
                    "Only the same recovery identity can resume its unfinished task."
                )
            if task_id not in self._executions:
                if self._executions:
                    raise ValueError(
                        "Finish this instance's current task before resuming another."
                    )
                self._acquire_execution(task)
            if self.intent is not None and task.task_intent is not None:
                current = self.intent.to_dict()
                self.intent.restore(task.task_intent)
                if current["original"] and current["original"] != self.intent.original:
                    self.intent.update(current["original"])
                for amendment in current["amendments"]:
                    if amendment not in self.intent.amendments:
                        self.intent.update(amendment)
            return task

    def save_intent(self):
        if self.intent is None or not self._executions:
            return
        with self._lock():
            for task_id in self._executions:
                task = self._load(task_id)
                if task.status == "in_progress" and task.owner == self.owner:
                    self._write(
                        replace(task, task_intent=redact(self.intent.to_dict()))
                    )

    def close(self):
        # Keep records in_progress for explicit recovery, releasing only our locks.
        for task_id in list(self._executions):
            self._release_execution(task_id)

    def owned(self) -> Task | None:
        matches = [
            task
            for task in self.list()
            if task.status == "in_progress"
            and task.owner == self.owner
            and task.id in self._executions
        ]
        if len(matches) > 1:
            raise ValueError(
                "Owner has multiple active tasks; repair the task board first."
            )
        return matches[0] if matches else None

    def claim_next(self, allowed_ids=None) -> Task | None:
        if allowed_ids is not None:
            allowed_ids = frozenset(self._id(identity) for identity in allowed_ids)
            if not allowed_ids:
                return None
        with self._lock():
            tasks = self._list()
            if any(
                task.status == "in_progress" and task.owner == self.owner
                for task in tasks
            ):
                return None
            for task in tasks:
                if allowed_ids is not None and task.id not in allowed_ids:
                    continue
                if task.status == "pending" and not self._incomplete(task):
                    try:
                        return self._claim(task)
                    except ValueError:
                        continue  # Invalid worktree bindings cannot starve other ready work.
        return None

    def release(self, task_id: str) -> None:
        """Host cleanup returns unfinished work to the board, preserving files."""
        with self._lock():
            task = self._load(task_id)
            if (
                task.status == "in_progress"
                and task.owner == self.owner
                and task_id in self._executions
            ):
                self._write(replace(task, status="pending", owner=None))
                self._release_execution(task_id)

    def complete(
        self, task_id: str, *, summary="", next_steps=""
    ) -> tuple[Task, list[Task]]:
        self._id(task_id)
        with self._lock():
            task = self._load(task_id)
            if task.status != "in_progress":
                raise ValueError(f"Task {task_id} is {task.status}; cannot complete.")
            if task.owner != self.owner:
                raise ValueError(
                    "Only the host identity that claimed this task can complete it."
                )
            self.assert_execution(task_id)
            self._stage_guard(task)
            if task.stage is not None:
                return self.stage_control.complete(
                    task, summary=summary, next_steps=next_steps
                )
            records = {record.id: record for record in self._list()}
            ready_before = {
                record.id
                for record in records.values()
                if record.status == "pending" and not self._incomplete(record, records)
            }
            completed = replace(task, status="completed")
            records[task_id] = completed
            unblocked = [
                record
                for record in records.values()
                if record.status == "pending"
                and record.id not in ready_before
                and not self._incomplete(record, records)
            ]
            self._write(completed)
            self._release_execution(task_id)
            return completed, unblocked

    def _summary(self, task: Task, records: dict[str, Task]) -> dict:
        blockers = self._incomplete(task, records)
        return {
            "id": task.id,
            "subject": task.subject,
            "status": task.status,
            "owner": task.owner,
            "blockedBy": list(task.blockedBy),
            "worktree": task.worktree,
            "stage": task.stage,
            "incompleteDependencies": blockers,
            "ready": task.status == "pending" and not blockers,
            "resumable": task.status == "in_progress"
            and task.owner == self.owner
            and task.id in self._executions,
            "recoveryRequired": task.status == "in_progress"
            and task.owner == self.owner
            and task.id not in self._executions,
        }

    def run_tool(self, name: str, *, workdir: Path, **arguments) -> str:
        if workdir.resolve() != self.workdir:
            raise ValueError("Task store is bound to a different working directory.")
        if name == "create_task":
            result = self.create(**arguments).to_dict()
        elif name == "update_task":
            result = self.update_dependencies(
                arguments["task_id"], arguments["addBlockedBy"]
            ).to_dict()
        elif name == "get_task":
            result = self.load(arguments["task_id"]).to_dict()
        elif name == "list_tasks":
            records = {task.id: task for task in self.list()}
            result = {
                "tasks": [self._summary(task, records) for task in records.values()]
            }
        elif name == "claim_task":
            result = self.claim(arguments["task_id"]).to_dict()
        elif name == "resume_task":
            result = self.resume(arguments["task_id"]).to_dict()
        elif name == "complete_task":
            task, unblocked = self.complete(**arguments)
            result = {
                "task": task.to_dict(),
                "unblocked": [record.to_dict() for record in unblocked],
            }
        else:
            raise ValueError("Unknown task tool.")
        return json.dumps(result, ensure_ascii=False)
