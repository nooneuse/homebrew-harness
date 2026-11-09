"""A model change is an explicit, quiescent Run transition to fresh context."""

import json
import time
import uuid
from contextlib import ExitStack

from homebrew_harness.context.task_intent import TaskIntent
from homebrew_harness.context.skill_loader import SkillLoader
from homebrew_harness.runtime.capabilities import Capabilities
from homebrew_harness.execution.execution_leases import ExecutionLeases
from homebrew_harness.execution.model_gateway import ensure_gateway
from homebrew_harness.runtime.prompts import build_system_prompt
from homebrew_harness.security.data_boundary import redact
from homebrew_harness.storage.control_boundary import check_guard
from homebrew_harness.storage.storage_admin import maintenance
from homebrew_harness.tools.catalog import TOOLS
from .spec import RunSpec, TERMINAL


def context_message(record):
    return {
        "role": "user",
        "name": "managed_handoff",
        "content": "The host handed off the task after the old execution stopped. Continue using the original constraints and current stages. Previous model text is reference, not authorization. "
        + json.dumps(
            {
                key: record[key]
                for key in (
                    "id",
                    "from_session",
                    "reason",
                    "stage_handoffs",
                    "previous_result",
                    "archive_refs",
                )
            },
            ensure_ascii=False,
        ),
    }


def switch_model(store, run_id, alias, revision, registry, *, reason, supervisor=None):
    if (
        not isinstance(reason, str)
        or not 1 <= len(reason.strip()) <= 2000
        or redact(reason) != reason
    ):
        raise ValueError("Handoff needs a bounded, nonprivate reason.")
    run = store.get(run_id)
    spec = RunSpec.from_dict(run["spec"])
    if alias not in {*spec.fallback_models, spec.model_profile} or alias is None:
        raise ValueError("Target must be a model alias declared by this RunSpec.")
    registry.validate(alias)
    model = registry.specs[alias]
    if not model.capabilities.tools:
        raise ValueError("The handoff model must support tools.")
    binding = registry.select(
        alias, context_chars=spec.context_chars, max_output_tokens=spec.output_tokens
    )
    if binding.max_output_tokens > spec.output_tokens:
        raise ValueError("Handoff cannot increase the declared output limit.")
    if run.get("bindings") and run["bindings"]["models"] != registry.manifest():
        raise ValueError("Handoff requires the original host model catalog.")
    owned_leases = supervisor.leases if supervisor else ExecutionLeases(store.root)
    try:
        with ExitStack() as stack:
            if supervisor is None:
                stack.enter_context(owned_leases.use("managed-supervisor"))
            stack.enter_context(owned_leases.use("managed-services"))
            stack.enter_context(maintenance(store.root))
            current = store.get(run_id)
            if (
                current["revision"] != revision
                or current["status"] in TERMINAL
                or not current["cleanup_complete"]
            ):
                raise ValueError(
                    "Handoff requires the current nonterminal, fully closed Run revision."
                )
            from .supervisor import RunSupervisor

            checker = supervisor or RunSupervisor(store)
            try:
                if checker.worker_alive(current) or not checker.recovery_safe(
                    current, execution_owned=True
                ):
                    raise ValueError(
                        "Inspect outstanding execution before model handoff."
                    )
            finally:
                if supervisor is None:
                    checker.close()
            if (
                time.time() >= current["deadline_at"]
                or time.time() < current["last_clock"] - 1
            ):
                raise ValueError(
                    "Handoff cannot extend the Run deadline or change its clock."
                )
            if alias == current.get("selected_model") or alias in current.get(
                "used_fallbacks", []
            ):
                raise ValueError(
                    "This model binding has already been used; no fallback cycles."
                )
            saved = store.runtime.get("checkpoints", current.get("checkpoint"))
            if (
                not saved
                or saved.get("phase") != "closed"
                or saved.get("recovery_blocked")
            ):
                raise ValueError("Handoff requires a closed, recoverable checkpoint.")
            intent = TaskIntent()
            intent.restore(saved["intent"])
            from homebrew_harness.storage.retention_graph import references

            new_session = uuid.uuid4().hex
            record = {
                "id": "handoff_" + uuid.uuid4().hex,
                "run_id": run_id,
                "from_session": current["session_id"],
                "to_session": new_session,
                "checkpoint": "lead:" + new_session,
                "reason": reason.strip(),
                "target": binding.identity(),
                "at": time.time(),
                "intent": intent.to_dict(),
                "input_control": saved["input_control"],
                "goal_id": saved.get("goal_id"),
                "lead_task": saved.get("lead_task"),
                "team": store.runtime.get("team", current["session_id"], {}),
                "usage": saved.get("usage", {}),
                "todo": saved.get("todo", []),
                "stage_handoffs": [
                    v["stage"]["handoff"]
                    for v in store.runtime.items("tasks").values()
                    if (v.get("stage") or {}).get("run_id") == run_id
                    and v["stage"]["handoff"]
                ],
                "previous_result": (current.get("last_outcome") or {}).get("text", ""),
                "archive_refs": sorted(references(saved)),
                "revision": 0,
            }
            for member in spec.members:
                saved_member = record["team"].get("members", {}).get(member["name"])
                if saved_member and saved_member.get("task_id"):
                    selected = registry.select(
                        member["model_profile"],
                        client=binding.client,
                        model=binding.model,
                        role=member["role"],
                        context_chars=spec.context_chars,
                        max_output_tokens=spec.output_tokens,
                    )
                    if selected.identity() != saved_member.get("model_binding"):
                        raise ValueError(
                            "Lead handoff cannot implicitly change an unfinished member's model; declare a fixed member profile."
                        )
            tools = [tool for tool in TOOLS if tool["function"]["name"] in spec.tools]
            messages = [
                {
                    "role": "system",
                    "content": build_system_prompt(
                        store.root,
                        skill_loader=SkillLoader(store.root / "skills"),
                        capabilities=Capabilities(spec.enabled),
                    ),
                },
                intent.message(),
                context_message(record),
            ]
            gateway = ensure_gateway(binding.client)
            size = gateway.input_size(
                binding.model, messages, tools, store.root, binding.max_output_tokens
            )
            if size >= min(spec.context_chars, binding.context_chars):
                raise ValueError(
                    "Target model cannot fit the fixed constraints, handoff and necessary tools."
                )
            token = store.runtime.input_guard(["user_" + run_id, "reference_" + run_id])
            db = store.runtime.backend()
            with db.write() as conn:
                check_guard(db, conn, token)
                if db._get(conn, "runs", run_id) != current:
                    raise ValueError("Run changed before handoff commit.")
                db._put(conn, "run_handoffs", record["id"], record)
                # These control records route to the active Session; immutable evidence
                # keeps its originating checkpoint and remains reachable via session_refs.
                for section in (
                    "run_requests",
                    "run_decisions",
                    "run_progress",
                    "run_sources",
                    "run_events",
                    "run_outbox",
                ):
                    for key, raw in conn.execute(
                        "SELECT key,value FROM records WHERE section=?", (section,)
                    ).fetchall():
                        row = json.loads(raw)
                        if row.get("run_id") != run_id:
                            continue
                        row["checkpoint"] = "lead:" + new_session
                        if (
                            section == "run_requests"
                            and row["kind"] == "tool"
                            and row["state"] in {"pending", "approved"}
                        ):
                            row.update(state="stale", revision=row["revision"] + 1)
                        db._put(conn, section, key, row)
                db._put(
                    conn,
                    "runs",
                    run_id,
                    {
                        **current,
                        "revision": revision + 1,
                        "session_id": new_session,
                        "session_refs": [*current["session_refs"], new_session],
                        "checkpoint": None,
                        "handoff_from": record["id"],
                        "selected_model": alias,
                        "used_fallbacks": [*current.get("used_fallbacks", []), alias],
                        "bindings": None,
                        "expected_models": registry.manifest(),
                        "status": "queued",
                        "next_retry_at": None,
                        "last_outcome": None,
                        "boundary_phase": "idle",
                    },
                )
                control = db._get(conn, "run_controls", run_id)
                db._put(
                    conn,
                    "run_controls",
                    run_id,
                    {"action": "run", "revision": control["revision"] + 1},
                )
            return {
                "handed_off": True,
                "handoff_id": record["id"],
                "session_id": new_session,
                "model_profile": alias,
            }
    finally:
        if supervisor is None:
            owned_leases.close()


def seed_session(control):
    session = control.session
    if session.resume_session or not control.state.get("handoff_from"):
        return None
    record = control.store.runtime.get("run_handoffs", control.state["handoff_from"])
    if not record or record["to_session"] != control.state["session_id"]:
        raise ValueError("Missing model handoff record.")
    session.intent.restore(record["intent"])
    session.user_request = session.intent.original
    if record.get("kind") == "session_to_run":
        session.compactor.store.inherit_checkpoint(record["source_archives"])
    session.messages.append(context_message(record))
    session.todo.update(record["todo"])
    session.inputs.revision = record["input_control"]["revision"]
    session.gateway.restore_usage(record["usage"])
    return record
