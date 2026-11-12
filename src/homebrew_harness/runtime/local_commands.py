"""Workspace queries and narrow controls over existing component owners."""

import json
from pathlib import Path

from homebrew_harness.context.memory import MemoryStore, MemoryError, SECRET_PATTERN
from homebrew_harness.execution.execution_control import control_scope
from homebrew_harness.execution.model_gateway import Deadline
from homebrew_harness.orchestration.worktrees import WorktreeManager
from homebrew_harness.security.data_boundary import redact

QUERY_NAMES = {"pwd", "ps", "hooks", "agents", "worktree", "memory", "resume"}


def output(value):
    return redact(value) if isinstance(value, str) else json.dumps(redact(value), ensure_ascii=False, indent=2)


def sessions(store, *, current=None):
    rows = []
    metas = sorted(store.items("session_meta").items(), key=lambda item: item[1].get("last_activity_at", 0), reverse=True)
    for sid, meta in metas:
        if len(rows) >= 20:
            break
        if meta.get("lifecycle") in {"retired", "purged"}:
            continue
        saved = store.get("checkpoints", "lead:" + sid)
        if not saved or saved.get("managed_run"):
            continue
        rows.append({"id": "session:" + sid, "kind": "session", "current": sid == current,
            "summary": saved.get("intent", {}).get("original", "")[:300],
            "last_activity_at": meta.get("last_activity_at"), "paused": meta.get("execution_paused", False),
            "state": "inspection_required" if saved.get("recovery_blocked") else "cleanup_pending" if not meta.get("close_complete") and sid != current else "retained",
            "model": saved.get("model_binding", {}).get("model")})
    return rows


def resolve_session(store, identity):
    from homebrew_harness.storage.checkpoint_lifecycle import session_id
    try:
        sid = session_id(identity.removeprefix("session:"))
    except Exception:
        raise ValueError("Invalid Session ID; use /resume list.") from None
    meta = store.get("session_meta", sid, {})
    saved = store.get("checkpoints", "lead:" + sid)
    if not saved or saved.get("managed_run") or meta.get("lifecycle") in {"retired", "purged"}:
        raise ValueError("Retained Lead Session not found; use /resume list.")
    if not meta.get("close_complete"):
        raise ValueError("Saved Session cleanup is incomplete; inspect it before restoring.")
    return sid


def validate_restore(session, sid):
    """Validate immutable bindings before the terminal closes its current context."""
    saved = session.runtime_store.get("checkpoints", "lead:" + sid, {})
    base = saved.get("model_selection", {}).get("base_binding", saved.get("model_binding"))
    checks = ((saved.get("contract_version") == 4, "protocol contract"),
        (saved.get("bash_policy") == session.bash_policy.identity(), "Bash policy"),
        (saved.get("model_registry") == session.models.manifest(), "model catalog"),
        (base == session._selection_base_identity, "original model binding"),
        (saved.get("capabilities") == session.capabilities.manifest(), "capability configuration"))
    for matches, label in checks:
        if not matches:
            raise ValueError("Restore requires the saved " + label + ".")


def pwd(workdir, session=None):
    cwd = session.workspace.lead_cwd() if session else Path(workdir)
    task = session.workspace.lead_assignment if session else None
    manager = WorktreeManager(Path(workdir), None)
    git_present = any((directory / ".git").exists() or (directory / ".git").is_symlink() for directory in (cwd, *cwd.parents))
    branch = (manager._git(["symbolic-ref", "--short", "-q", "HEAD"], cwd=cwd, check=False).stdout.strip() or None) if git_present else None
    return {"workspace": str(workdir), "cwd": str(cwd), "branch": branch,
        "session_id": session.session_id if session else None,
        "run_id": session.run_control.id if session and session.run_control else None,
        "task_id": task.id if task else None, "worktree": task.worktree if task else None}


def managed_pwd(workdir, store, run):
    value = pwd(workdir)
    saved = store.get("checkpoints", "lead:" + str(run.get("session_id")), {})
    identity = saved.get("lead_task")
    task = store.get("tasks", identity, {}) if identity else {}
    value.update(run_id=run["id"], task_id=identity, worktree=task.get("worktree"), source="checkpoint snapshot")
    if task.get("worktree"):
        manager = WorktreeManager(Path(workdir), None)
        value["cwd"] = str(manager.resolve(task["worktree"]))
        value["branch"] = manager.branch(task["worktree"])
    return value


def worktrees(workdir, store, session=None, name=None):
    manager = WorktreeManager(Path(workdir), None)
    if name is not None:
        manager.path(name)
    tasks = list(store.items("tasks").values())
    rows = []
    registry = manager._registry()
    selected_path = manager.resolve(name) if name is not None else None
    entries = [(selected_path, registry[selected_path])] if selected_path else list(registry.items())[:64]
    for path, entry in entries:
        managed = path.parent == manager.root and entry.get("branch") == "refs/heads/" + manager.branch(path.name)
        if name is not None and (not managed or path.name != name):
            continue
        bound = [r for r in tasks if r.get("worktree") == path.name] if managed else []
        rows.append({"name": path.name if managed else None, "cwd": str(path), "branch": entry.get("branch"),
            "head": entry.get("HEAD"), "managed": managed, "current": (session.workspace.lead_cwd() if session else Path(workdir)) == path,
            "tasks": [{key: r.get(key) for key in ("id", "status", "owner")} for r in bound],
            "dirty": bool(manager._git(["status", "--porcelain=v1", "--untracked-files=normal"], cwd=path).stdout.strip())})
    if name is not None and not rows:
        raise ValueError("Managed worktree not found.")
    return {"worktrees": rows, "truncated": len(registry) > 64}


def memory(workdir, command, runtime=None):
    store = runtime.store if runtime else MemoryStore(Path(workdir))
    snapshot = store.snapshot()
    records = {record.name: record for record in snapshot.records}
    if command.action in {"status", "list"}:
        rows = [{"name": r.name, "description": r.description, "evidence": r.evidence} for r in records.values()]
        return rows if command.action == "list" else {"mode": runtime.mode if runtime else "off", "path": str(store.root), "records": rows}
    if command.argument not in records:
        raise MemoryError("Unknown memory record.")
    if command.action == "show":
        return records[command.argument].__dict__
    text = None if command.action == "delete" else command.values[0]
    if text is not None and (SECRET_PATTERN.search(text) or redact(text) != text or runtime and runtime._sensitive(text)):
        raise MemoryError("Memory correction requires nonprivate text.")
    return store.update_record(command.argument, text)


def agents(session):
    if session is None:
        return []
    rows = [{"id": "agent:lead", "name": "lead", "kind": "lead", "role": "lead", "model": session.model,
        "cwd": str(session.workspace.lead_cwd()), "task_id": session.workspace.lead_assignment.id if session.workspace.lead_assignment else None,
        "state": "paused" if session._execution_paused else "running" if session.inputs.active_id else "idle", "stoppable": bool(session.inputs.active_id), "steering": True}]
    if session.team:
        with session.team.lock:
            members = list(session.members.values())
        for member in members:
            running = bool(member.thread and member.thread.is_alive())
            rows.append({"id": "agent:" + member.name, "name": member.name, "kind": "teammate", "role": member.role,
                "model": member.model_binding.model if member.model_binding else None,
                "cwd": str(member.assignment_path) if member.assignment_path else str(session.workdir),
                "task_id": member.assignment.id if member.assignment else None,
                "state": "stopping" if running and member.stop_event.is_set() else member.status,
                "stoppable": running, "steering": running})
    rows.extend({**row, "steering": False} for row in session.activities.snapshot() if row["kind"] == "subagent")
    return rows


def processes(session):
    if session is None:
        return []
    rows = session.gateway.active_requests() + session.activities.snapshot()
    if session.inputs.active_id:
        rows.insert(0, {"id": session.inputs.active_id, "kind": "foreground", "role": "lead", "cwd": str(session.workspace.lead_cwd()),
            "state": "stopping" if session._execution_paused else session.inputs.phase, "stoppable": True})
    if session.background:
        rows.extend({"id": row["bg_id"], "kind": "background", "state": "stopping" if row.get("stop_requested") and row["status"] == "running" else row["status"],
            "cwd": row["cwd"], "stoppable": row["status"] == "running", "result": row.get("result"),
            "task_id": (row.get("assignment") or {}).get("task_id")} for row in session.background.snapshot())
    test = session.tests.activity() if hasattr(session.tests, "activity") else None
    if test:
        rows.append(test)
    rows.extend(row for row in agents(session) if row["kind"] == "teammate")
    if session.workflow:
        rows.extend(session.workflow.activities() if hasattr(session.workflow, "activities") else [])
    if session.mcp:
        status = session.mcp.connection_status()
        rows.append({"id": "mcp:connections", "kind": "mcp", "state": "resource", "stoppable": False, "details": status})
    rows.extend({"id": "operation:" + row["call_id"], "kind": "recovery", "state": "cleanup_pending" if row["active"] else "inspection_required",
        "stoppable": False, "details": row} for row in session.recovery_operations())
    return rows


def managed_view(store, run, *, agent_only=False, worker=None):
    """Foreign workers expose durable observations, labelled as checkpoint snapshots."""
    saved = store.get("checkpoints", "lead:" + str(run.get("session_id")), {})
    binding = saved.get("model_binding") or run.get("expected_binding") or {}
    rows = [{"id": "agent:lead" if agent_only else run["id"], "kind": "lead" if agent_only else "run",
        "name": "lead", "state": run["status"], "model": binding.get("model"), "profile": run.get("selected_model"),
        "stoppable": not agent_only, "source": "run control"}]
    members = saved.get("team", {}).get("members", {})
    for declared in run["spec"].get("members", []):
        member = members.get(declared["name"], {})
        task = store.get("tasks", member.get("task_id"), {}) if member.get("task_id") else {}
        rows.append({"id": "agent:" + declared["name"], "kind": "teammate", "name": declared["name"],
            "role": declared["role"], "model": (member.get("model_binding") or {}).get("model"), "profile": declared.get("model_profile"),
            "state": member.get("status", "declared"), "task_id": member.get("task_id"), "worktree": task.get("worktree"),
            "stoppable": False, "source": "checkpoint snapshot", "native_control": "/stop pauses the owning Run"})
    if not agent_only:
        if worker and worker.poll() is None:
            rows.append({"id": "worker:" + run["id"], "kind": "worker", "pid": worker.pid, "state": "running", "stoppable": False, "native_control": "/stop"})
        for item in saved.get("pending_background", []):
            rows.append({"id": item["bg_id"], "kind": "background", "state": item["status"], "stoppable": False, "source": "checkpoint snapshot"})
        for item in store.items("mcp_resources").values():
            if item.get("run_id") == run["id"]:
                rows.append({"id": item["id"], "kind": "mcp", "state": item.get("state"), "stoppable": False, "source": "durable resource"})
    return rows


def stop_target(session, identity):
    row = next((r for r in processes(session) if r["id"] == identity), None)
    if not row or not row.get("stoppable"):
        raise ValueError("Unknown or inactive stoppable operation; use /ps.")
    if row["kind"] == "foreground":
        return session.request_stop()
    if row["kind"] == "model":
        return session.gateway.stop_request(identity)
    if row["kind"] in {"subagent", "tool"}:
        return session.activities.stop(identity)
    if row["kind"] == "background":
        return session.background.stop(identity)
    if row["kind"] == "test":
        return session.tests.stop(identity)
    if row["kind"] == "teammate":
        return session.team.stop_member(row["name"])
    if row["kind"] == "workflow":
        return session.workflow.stop_run(identity)
    raise ValueError("This resource uses its native lifecycle command.")


def run(session, command):
    with control_scope(Deadline.after(2)):
        if command.name == "pwd":
            return output(pwd(session.workdir, session))
        if command.name in {"ps", "agents"}:
            rows = processes(session) if command.name == "ps" else agents(session)
            if command.action == "show":
                rows = [r for r in rows if command.argument in {r["id"], r.get("name")}]
                if not rows:
                    raise ValueError("Unknown operation or agent.")
            if command.name == "ps" and command.action != "show":
                rows = [{k: v for k, v in r.items() if k not in {"result", "details"}} for r in rows]
            text = output({command.name: rows})
            if len(text) > 16000:
                return output({"truncated": True, "preview": text[:12000]})
            return text
        if command.name == "hooks":
            return output(session.hooks.snapshot(command.argument if command.action == "show" else None))
        if command.name == "memory":
            value = memory(session.workdir, command, session.memory)
            if command.action in {"correct", "delete"}:
                from homebrew_harness.runtime.turns import set_memory_context
                set_memory_context(session.messages, "")
                session._save_checkpoint()
            return output(value)
        if command.name == "worktree":
            if command.mode == "read":
                return output(worktrees(session.workdir, session.runtime_store, session, command.argument or None))
            if session.run_control:
                raise ValueError("Managed worktrees follow the fixed Run stage contract.")
            manager = session.workspace.worktrees
            if manager is None:
                raise ValueError("Enable tasks/worktrees before managing a task worktree.")
            if command.action == "create":
                return output(manager.create(command.argument, command.values[0]))
            paths = session.background.running_paths() if session.background else []
            paths.extend(m.assignment_path for m in session.members.values() if m.assignment_path)
            if session.workspace.lead_assignment:
                paths.append(session.workspace.lead_cwd())
            return output(manager.remove(command.argument, leased_paths=paths))
        if command.name == "resume":
            return output({"sessions": sessions(session.runtime_store, current=session.session_id)})
    raise ValueError("Unsupported workspace command.")
