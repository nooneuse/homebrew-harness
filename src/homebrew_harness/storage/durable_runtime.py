"""Atomic private runtime state: inbox, checkpoints and write-ahead tool outcomes.

One SQLite transaction commits a checkpoint and its inbox ACKs.
An interrupted started action is unknown, never automatically replayed.
"""

import copy
import json
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from homebrew_harness.execution.execution_control import persistence_scope
from homebrew_harness.security.safe_files import canonical_root
from homebrew_harness.storage.storage_gate import storage_operation
from homebrew_harness.tools.tool_outcome import needs_inspection

_ACTIVE = ContextVar("durable_checkpoint", default=None)
_LIVE_LOCK = threading.RLock()
_LIVE = {}
_OPERATION = ContextVar("current_tool_operation", default=None)
_BOUNDARY_OBSERVER = ContextVar("checkpoint_boundary_observer", default=None)


@contextmanager
def boundary_observer(callback):
    token = _BOUNDARY_OBSERVER.set(callback)
    try:
        yield
    finally:
        _BOUNDARY_OBSERVER.reset(token)


def current_operation():
    return _OPERATION.get()


def operation_active(root, identity, *, excluding=None):
    with _LIVE_LOCK:
        key = (str(root), identity)
        return bool(_LIVE.get(key, set()) - (set(excluding) if isinstance(excluding, (tuple, list, set, frozenset)) else {excluding} if excluding is not None else set()))


def track_activity(resource, key=None):
    key = key or _OPERATION.get()
    if key:
        with _LIVE_LOCK:
            _LIVE.setdefault(key, set()).add(resource)
    return key


def settle_activity(key, resource):
    if key:
        with _LIVE_LOCK:
            group = _LIVE.get(key, set())
            group.discard(resource)
            if not group:
                _LIVE.pop(key, None)


from homebrew_harness.runtime.runtime_state import RecoveryRequired, settled_operation


class DurableStore:
    """One current incremental SQLite runtime; no prerelease format adapters."""

    def __init__(self, workdir):
        self.workdir = canonical_root(Path(workdir))
        from homebrew_harness.storage.incremental_store import IncrementalStore

        self._store = IncrementalStore(self.workdir)
        self.path = self._store.path

    def backend(self):
        return self._store

    @property
    def backend_name(self):
        return "sqlite"

    def read(self):
        return self._store.read()

    def load_session(self, requested):
        return self._store.load_session(requested)

    def get_meta(self, key, default=None):
        return self._store.get_meta(key, default)

    @storage_operation
    def transaction(self, update):
        return self._store.transaction(update)

    @persistence_scope(operation="checkpoint_prepare")
    def checkpoint(
        self, key, snapshot, *, ack=(), expected_revision=None, boundary=None
    ):
        from homebrew_harness.storage.persistence import persist
        from homebrew_harness.storage.storage_gate import storage_gate
        ack = tuple((name, owner, tuple(ids)) for name, owner, ids in ack)
        def commit():
            with storage_gate(self.workdir):
                return self._store.checkpoint(key, snapshot, ack=ack,
                    expected_revision=expected_revision, boundary=boundary)
        return persist("checkpoint", commit, context_id=key)

    def checkpoint_revision(self, key):
        return self._store.revision(key)

    def record_result(self, key, operation_id, output, snapshot):
        from homebrew_harness.storage.storage_gate import storage_gate
        from homebrew_harness.storage.persistence import persist
        def commit():
            with storage_gate(self.workdir):
                return self._store.record_result(key, operation_id, output, snapshot)
        return persist("tool_result", commit, context_id=key)

    @storage_operation
    def start_session(self, sid, instance):
        return self._store.start_session(sid, instance)

    @storage_operation
    def finish_session(self, sid, instance, *, complete):
        return self._store.finish_session(sid, instance, complete=complete)

    def get(self, section, key, default=None):
        return self._store.get(section, key, default)

    def items(self, section, *, owner=None, prefix=None, owner_prefix=None):
        return self._store.items(
            section, owner=owner, prefix=prefix, owner_prefix=owner_prefix
        )

    def artifact_page(self, scope=None, *, offset=0, limit=20):
        return self._store.artifact_page(scope, offset=offset, limit=limit)

    @storage_operation
    def put(self, section, key, value):
        from homebrew_harness.storage.checkpoint_lifecycle import (
            checkpoint_session,
            require_retained,
        )

        if section == "checkpoints":
            return self._store.checkpoint(key, value)
        checkpoint = (
            value.get("checkpoint")
            if section in {"operations", "approval_reviews", "executions"} and isinstance(value, dict)
            else None
        )
        if checkpoint is not None:
            sid = checkpoint_session(checkpoint, {})
            if sid is not None:
                require_retained(self.get("session_meta", sid))
        if section == "team":
            require_retained(self.get("session_meta", key))
        return self._store.put(section, key, value)

    @storage_operation
    def compare_put(self, section, key, value, expected_revision):
        return self._store.compare_put(section, key, value, expected_revision)

    @storage_operation
    def remove(self, section, key):
        return self._store.remove(section, key)

    @storage_operation
    def update_fields(self, section, key, fields):
        return self._store.update_fields(section, key, fields)

    @storage_operation
    def set_meta(self, key, value):
        return self._store.set_meta(key, value)

    @storage_operation
    def clear_operations(self, keys):
        return self._store.clear_operations(keys)

    @storage_operation
    def mailbox(self, action, name, *, owner=None, records=()):
        return self._store.mailbox(action, name, owner=owner, records=records)

    @storage_operation
    def submit_input(self, record, expected_turn=None):
        return self._store.submit_input(record, expected_turn)

    def input_guard(self, names):
        return self._store.input_guard(names)

    @storage_operation
    def accept_operation(self, key, record, guard):
        return self._store.accept_operation(key, record, guard)

    @storage_operation
    def team_checkpoint(self, sid, snapshot, *, ack_name=None, owner=None, records=()):
        from homebrew_harness.storage.checkpoint_lifecycle import require_retained

        require_retained(self.get("session_meta", sid))
        return self._store.team_checkpoint(
            sid, snapshot, ack_name=ack_name, owner=owner, records=records
        )


class CheckpointWriter:
    def __init__(self, store, key, snapshot):
        self.store, self.key, self.snapshot = store, key, snapshot
        self.messages = None
        self.todo = None
        self.intent = None
        self.turn_id = uuid.uuid4().hex
        self.completion_ack = ()
        self.effects = []
        self.tool_messages = {}
        self._operation_tokens = {}
        self.effect_parent = None

    def value(self, phase="running"):
        value = self.snapshot()
        if self.messages is not None:
            value["messages"] = self.messages
        if self.todo is not None:
            value["context_todo"] = [
                {"content": i.content, "status": i.status} for i in self.todo.items
            ]
            value["context_todo_revision"] = self.todo.revision
        if self.intent is not None:
            # Runtime prompts (Cron, team observations) cannot amend the host's
            # original user task. Persist the loop's intent as separate context.
            value["context_intent"] = self.intent.to_dict()
        value["effects"] = copy.deepcopy(self.effects)
        value["phase"] = phase
        return value

    @persistence_scope(operation="checkpoint_prepare")
    def save(self, phase="running", *, ack=(), boundary=None, overrides=None):
        expected = self.store.checkpoint_revision(self.key)
        value = self.value(phase)
        value.update(overrides or {})
        control = getattr(self, "run_control", None)
        if control is not None:
            boundary = control.boundary(value, phase, boundary)
        if phase == "executed" and self.completion_ack:
            value["pending_team"] = []
            value["pending_background"] = []
            ack = [*ack, *self.completion_ack]
        with persistence_scope(operation="checkpoint", context_id=self.key):
            self.store.checkpoint(
                self.key, value, ack=ack, expected_revision=expected, boundary=boundary
            )

    def tool_started(self, call, workdir, *, track=False, guard=None, read_only=False):
        operation_id = uuid.uuid4().hex + ":" + call.id
        self.effects.append(
            {
                "operation_id": operation_id,
                "call_id": call.id,
                "name": call.name,
                "state": "started",
                **({"read_only": True} if read_only else {}),
            }
        )
        if self.effect_parent is not None:
            self.effect_parent.effects.append(self.effects[-1])
        record = {
            "checkpoint": self.key,
            "state": "started",
            "revision": 0,
            "name": call.name,
            "call_id": call.id,
            "cwd": str(workdir.resolve().relative_to(self.store.workdir)),
            "time": time.time(),
            **({"read_only": True} if read_only else {}),
        }
        if getattr(self, "run_control", None):
            record["managed_run"] = self.run_control.header
            if hasattr(self.run_control, "operation_guard"):
                guard = {**(guard or {}), **self.run_control.operation_guard()}
            from homebrew_harness.runs.requests import current_approval, digest

            approval = current_approval()
            if approval is not None:
                record.update(
                    approval_id=approval["id"],
                    approval_revision=approval["revision"],
                    approval_context=digest(approval["context"]),
                    approval_arguments=digest(json.loads(call.arguments)),
                    operation_id=operation_id,
                )
            if call.name.startswith("mcp__") and hasattr(
                self.run_control, "mcp_operation_fields"
            ):
                record.update(self.run_control.mcp_operation_fields(call.name, workdir))
                record["operation_id"] = operation_id
            if call.name == "run_tests":
                record["test_recipe"] = json.loads(call.arguments)["name"]
                record["test_grant_key"] = self.run_control.test_grant_key(
                    record["test_recipe"], workdir
                )
                record.update(
                    self.run_control.test_authorization_fields(
                        record["test_recipe"], workdir
                    )
                )
        from homebrew_harness.context.tracked_history import encoded_message

        assistant = next(
            (m for m in reversed(self.messages or []) if m.get("role") == "assistant"),
            None,
        )
        record["batch_id"] = encoded_message(assistant)[1] if assistant else None
        self.effects[-1]["batch_id"] = record["batch_id"]
        # Assistant batch was already committed before reaching this point.
        if track:
            track_activity(
                threading.current_thread(), (str(self.store.workdir), operation_id)
            )
            self._operation_tokens[operation_id] = _OPERATION.set(
                (str(self.store.workdir), operation_id)
            )
        try:
            self.store.accept_operation(operation_id, record, guard)
        except BaseException:
            effect = self.effects.pop()
            if self.effect_parent is not None and effect in self.effect_parent.effects:
                self.effect_parent.effects.remove(effect)
            self.end_operation(operation_id)
            raise
        return operation_id

    def end_operation(self, operation_id):
        token = self._operation_tokens.pop(operation_id, None)
        if token is not None:
            _OPERATION.reset(token)
        settle_activity(
            (str(self.store.workdir), operation_id), threading.current_thread()
        )

    @persistence_scope(operation="tool_result")
    def tool_finished(self, operation_id, output):
        call_id = operation_id.split(":", 1)[1]
        for effect in reversed(self.effects):
            if effect["call_id"] == call_id:
                effect["state"] = "unknown" if needs_inspection(output) else "done"
                break
        value = self.value()
        messages = value.get("messages", [])
        last = next(
            (
                index
                for index in range(len(messages) - 1, -1, -1)
                if messages[index].get("role") == "assistant"
            ),
            None,
        )
        declared = last is not None and any(
            call.get("id") == call_id for call in messages[last].get("tool_calls", [])
        )
        if declared:
            from homebrew_harness.context.tracked_history import (
                ConversationHistory,
                TrackedDict,
            )

            virtual = ConversationHistory(messages)
            message = TrackedDict(
                {"role": "tool", "tool_call_id": call_id, "content": output}
            )
            extra = []
            if not any(
                m.get("role") == "tool" and m.get("tool_call_id") == call_id
                for m in messages[last + 1 :]
            ):
                virtual.append(message)
                extra = [message]
            virtual.origin = messages
            virtual.origin_revision = getattr(messages, "version", None)
            virtual.extra = extra
            value["messages"] = virtual
            self.store.record_result(self.key, operation_id, output, value)
            self.tool_messages[call_id] = message
            return
        self.store.update_fields(
            "operations",
            operation_id,
            {
                "state": "unknown" if needs_inspection(output) else "done",
                "output": output,
            },
        )


class ExecutionWriter(CheckpointWriter):
    """Accepted worker effects, without writing the model's live conversation."""
    def __init__(self, parent, execution_id):
        snapshot = parent.snapshot()
        super().__init__(parent.store, parent.key,
                         lambda: {"session_id": snapshot.get("session_id")})
        self.run_control = getattr(parent, 'run_control', None)
        self.messages = copy.deepcopy(list(parent.messages or []))
        self.effect_parent = parent
        self.execution_id = execution_id

    def tool_started(self, *args, **kwargs):
        identity = super().tool_started(*args, **kwargs)
        self.effects[-1]['execution_id'] = self.execution_id
        self.store.update_fields('operations', identity, {'execution_id': self.execution_id})
        return identity

    def tool_finished(self, operation_id, output):
        messages = self.messages
        self.messages = []
        try:
            # Preserve the existing result-commit instrumentation; an execution
            # writer publishes only effects and never replaces its parent's chat.
            return super().tool_finished(operation_id, output)
        finally:
            self.messages = messages


@contextmanager
def checkpoint_scope(writer):
    token = _ACTIVE.set(writer)
    try:
        yield writer
    finally:
        _ACTIVE.reset(token)


def active_writer():
    return _ACTIVE.get()


def save_boundary(messages, todo=None, intent=None, phase="running", *, ack=()):
    writer = active_writer()
    if writer:
        writer.messages, writer.todo, writer.intent = messages, todo, intent
        writer.save(phase, ack=ack)
    observer = _BOUNDARY_OBSERVER.get()
    if observer is not None:
        observer(messages, todo, intent, phase)


def repair_history(messages, operations):
    """Pair persisted batches without executing them; recover known output or uncertainty."""
    if not isinstance(messages, list) or any(
        not isinstance(m, dict)
        or m.get("role") not in {"system", "developer", "user", "assistant", "tool"}
        for m in messages
    ):
        raise RecoveryRequired("Invalid saved conversation; no actions were resumed.")
    result = []
    pending = {}
    outcomes = {
        (v.get("batch_id"), v["call_id"]): v for v in operations if settled_operation(v)
    }
    uncertain = []

    def close_batch():
        for cid in list(pending):
            outcome = outcomes.get((pending[cid], cid)) or outcomes.get((None, cid))
            output = recovered_output(outcome)
            result.append({"role": "tool", "tool_call_id": cid, "content": output})
            if not outcome:
                uncertain.append(cid)
        pending.clear()

    for message in copy.deepcopy(messages):
        if message.get("role") == "tool":
            cid = message.get("tool_call_id")
            if cid in pending:
                outcome = outcomes.get((pending[cid], cid)) or outcomes.get((None, cid))
                if outcome and outcome["state"] == "inspected":
                    message["content"] = inspected_output(message.get("content", ""))
                result.append(message)
                pending.pop(cid)
        else:
            close_batch()
            result.append(message)
            if message.get("role") == "assistant":
                from homebrew_harness.context.tracked_history import encoded_message

                pending.update(
                    {
                        call["id"]: encoded_message(message)[1]
                        for call in message.get("tool_calls", [])
                    }
                )
    close_batch()
    return result, uncertain


def recovered_output(operation):
    unknown = json.dumps(
        {
            "error": "Interrupted tool batch; execution outcome unknown, original result unavailable and side effects uncertain. "
            "Host inspection is required and cannot establish successful execution.",
            "outcome": "unknown",
        }
    )
    if operation is None:
        return unknown
    if operation["state"] == "inspected":
        return inspected_output(operation.get("output", unknown))
    if "output" not in operation:
        raise RecoveryRequired(
            "Completed operation has no saved result; inspect the stored execution facts."
        )
    return operation["output"]


def inspected_output(output):
    try:
        value = json.loads(output)
        if isinstance(value, dict) and needs_inspection(value):
            return json.dumps(
                {**value, "outcome": "inspected", "host_inspected": True},
                ensure_ascii=False,
            )
    except (ValueError, TypeError):
        pass
    return output


def track_process(process):
    return track_activity(process)


def settle_process(key, process):
    settle_activity(key, process)
