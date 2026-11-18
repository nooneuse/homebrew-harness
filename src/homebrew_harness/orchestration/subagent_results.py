"""Bounded child facts and parent-owned result lookup on existing checkpoints."""

import json
import re
import threading
import time

from homebrew_harness.runtime.errors import error_message
from homebrew_harness.security.data_boundary import redact
from homebrew_harness.storage.durable_runtime import active_writer, operation_active
from homebrew_harness.tools.tool_outcome import needs_inspection, subagent_result


def resources_pending(store, effects, owner_operation=None):
    caller = threading.current_thread()
    keys = [e["operation_id"] for e in effects]
    if owner_operation:
        keys.append(owner_operation)
    from homebrew_harness.execution.operations import ACCEPTING_THREAD
    excluded = (caller, ACCEPTING_THREAD.get())
    return any(operation_active(store.workdir, key, excluding=excluded) for key in keys)


def model_pending(gateway, identity):
    if gateway is None:
        return False
    with gateway._state.condition:
        return any(r.metadata.get("context_id") == identity and not r.done.is_set() for r in gateway._state.inflight.values())


def result_for(writer, identity, status, *, result="", error=None, owner_operation=None, force_uncertain=False, gateway=None):
    rows = []
    for effect in writer.effects:
        row = writer.store.get("operations", effect["operation_id"], {})
        rows.append({**effect, **{k: row[k] for k in ("state", "read_only", "output") if k in row}})
    mutations = [e for e in rows if not e.get("read_only")]
    uncertain = force_uncertain or any(e["state"] in {"started", "unknown", "partial"} or needs_inspection(e.get("output", {})) for e in mutations)
    pending = resources_pending(writer.store, rows, owner_operation) or model_pending(gateway, identity)
    state = "uncertain" if uncertain else "pending" if pending else "settled" if mutations else "none"
    if pending and status == "completed":
        status = "incomplete"
    compact = [{k: e.get(k) for k in ("operation_id", "call_id", "name", "state", "read_only")} for e in mutations]
    value = subagent_result(identity, status, result=result, error=error_message(error) if error else "", effects=compact, effect_state=state, cleanup_complete=not pending, tools_executed=len(rows), evidence_ref=writer.key)
    value["owner_operation"] = owner_operation
    value["tool_results"] = [{k: e.get(k) for k in ("operation_id", "name", "state", "read_only")} for e in rows]
    return value


def read_result(*, workdir, run_id, operation_id=None, offset=0, limit=4000):
    if not re.fullmatch(r"sub-[0-9a-f]{32}", run_id) or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 16000:
        raise ValueError("Use an owned subagent ID and character offset/limit within 16000.")
    parent = active_writer()
    if parent is None:
        raise ValueError("Subagent evidence requires its owning parent checkpoint.")
    sid = parent.snapshot().get("session_id")
    key = "sub:" + str(sid) + ":" + run_id
    child = parent.store.get("checkpoints", key, {})
    if child.get("parent_checkpoint") != parent.key or child.get("run_id") != run_id:
        raise ValueError("Subagent does not belong to this parent context.")
    summary = child.get("subagent_result")
    if summary is None:
        raise ValueError("The child has not saved a structured result yet.")
    value = dict(summary)
    if value.get("effect_state") == "pending":
        from homebrew_harness.execution.model_gateway import current_scope
        idle = not resources_pending(parent.store, child.get("effects", []), value.get("owner_operation")) and not model_pending(current_scope().gateway, run_id)
    else:
        idle = False
    if idle:
        value.update(effect_state="settled" if value.get("effects") else "none", cleanup_complete=True, outcome="completed")
    if operation_id is not None:
        if operation_id not in {e["operation_id"] for e in summary.get("tool_results", [])}:
            raise ValueError("Operation is not registered to this child.")
        record = parent.store.get("operations", operation_id, {})
        effect = next(e for e in child["effects"] if e["operation_id"] == operation_id)
        if record and record.get("checkpoint") != key:
            raise ValueError("Tool result belongs to another context.")
        output = record.get("output")
        execution_id = effect.get('execution_id')
        if output is None and execution_id is not None:
            execution = parent.store.get('executions', execution_id, {})
            if execution.get('checkpoint') != key:
                raise ValueError('Execution result belongs to another context.')
            if not execution.get('execution_pending') and execution.get('result') is not None:
                output = json.dumps(execution['result'], ensure_ascii=False)
        if output is None:
            from homebrew_harness.context.tracked_history import encoded_message
            from homebrew_harness.security.safe_files import SafeFile
            matches = set()
            def collect(rows):
                batch = None
                for message in rows:
                    if message.get("role") == "assistant":
                        batch = encoded_message(message)[1]
                    elif message.get("role") == "tool" and message.get("tool_call_id") == effect["call_id"] and batch == effect.get("batch_id"):
                        matches.add(message["content"])
            collect(child["messages"])
            budget = 32 * 1024 * 1024
            for reference in child.get("archives", {}).get("saved", []):
                if not reference.endswith(".jsonl"):
                    continue
                with SafeFile(parent.store.workdir, reference) as saved:
                    data, _, _ = saved.snapshot(max_bytes=budget)
                if data is None:
                    raise ValueError("A child evidence archive is unavailable.")
                budget -= len(data)
                collect([json.loads(line) for line in data.decode().splitlines() if line.strip()])
            if len(matches) != 1:
                raise ValueError("The requested child result is unavailable or ambiguous.")
            output = matches.pop()
        value = {"run_id": run_id, "operation_id": operation_id, "name": effect["name"], "state": effect["state"], "output": output}
    raw = json.dumps(redact(value), ensure_ascii=False)
    return json.dumps({"run_id": run_id, "content": raw[offset:offset+limit], "total_characters": len(raw), "next_offset": offset+limit if offset+limit < len(raw) else None, "complete": offset+limit >= len(raw)}, ensure_ascii=False)
