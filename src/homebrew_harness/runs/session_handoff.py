"""A closed single-agent Session continues as a new Run without replaying input."""

import time
import uuid

from homebrew_harness.execution.execution_leases import ExecutionLeases


def accept_session_handoff(store, interaction, task, spec, config):
    from homebrew_harness.storage.retention_graph import references

    source = task["target"]["id"]
    saved = store.runtime.get("checkpoints", "lead:" + source)
    if (
        not saved
        or saved["phase"] != "closed"
        or saved.get("recovery_blocked")
        or saved.get("lead_task")
        or saved.get("team", {}).get("members")
    ):
        raise ValueError("Handoff needs a closed, inspectable single-agent checkpoint.")
    if any(
        v.get("state") in {"started", "unknown", "partial"}
        for v in store.runtime.items("operations", owner="lead:" + source).values()
    ):
        raise ValueError("Inspect uncertain source operations before handoff.")
    if any(
        v.get("state") in {"started", "unknown", "partial"}
        for v in store.runtime.items(
            "operations", owner_prefix="sub:" + source + ":"
        ).values()
    ):
        raise ValueError("Subagent execution is not settled.")
    if store.runtime.mailbox("inspect", "user_" + source):
        raise ValueError("Source inbox must be fully consumed before handoff.")
    tests = saved.get("test_handoff", {})
    if not tests.get("supported"):
        raise ValueError("Source test authorization provider cannot be handed off.")
    leases = ExecutionLeases(store.root)
    try:
        with leases.use("session"):
            record_id = "handoff_" + uuid.uuid4().hex

            def accept(db, conn, run):
                current = db._get(conn, "interaction_tasks", task["id"])
                if current != task:
                    raise ValueError("Handoff generation changed before acceptance.")
                if db._get(conn, "checkpoints", "lead:" + source) != saved:
                    raise ValueError(
                        "Source checkpoint changed before handoff acceptance."
                    )
                record = {
                    "id": record_id,
                    "kind": "session_to_run",
                    "run_id": run["id"],
                    "from_session": source,
                    "to_session": run["session_id"],
                    "checkpoint": "lead:" + run["session_id"],
                    "source_checkpoint": "lead:" + source,
                    "reason": "User requested managed continuation at a closed single-agent boundary.",
                    "target": saved["model_binding"],
                    "at": time.time(),
                    "intent": saved["intent"],
                    "input_control": saved["input_control"],
                    "goal_id": None,
                    "goal_spec": spec.goal,
                    "source_goal": saved.get("goal_id"),
                    "lead_task": None,
                    "team": {},
                    "usage": saved.get("usage", {}),
                    "todo": saved.get("todo", []),
                    "stage_handoffs": [],
                    "previous_result": saved.get("last_result", ""),
                    "archive_refs": sorted(references(saved)),
                    "source_archives": saved.get("archives", {}),
                    "revision": 0,
                }
                db._put(conn, "run_handoffs", record_id, record)
                if record["source_goal"]:
                    source_goal = db._get(conn, "goals", record["source_goal"])
                    if source_goal:
                        db._put(
                            conn,
                            "goals",
                            record["source_goal"],
                            {
                                **source_goal,
                                "status": "replaced",
                                "revision": source_goal["revision"] + 1,
                                "superseded_by_run": run["id"],
                                "last_reason": "Continued with a fresh Goal in the target Run; source evidence remains historical.",
                            },
                        )
                source_grants = {}
                for grant in tests["grants"]:
                    if (
                        "lead" in grant["roles"]
                        and grant["task_id"] is None
                        and grant["cwd"] in {None, str(store.root)}
                    ):
                        source_grants[grant["recipe"]] = {
                            "id": grant["id"],
                            "identity": grant["identity"],
                            "context": None,
                            "used": 0,
                            "max_runs": grant["remaining"],
                            "expires_at": min(run["deadline_at"], grant["expires_at"]),
                        }
                for name in spec.allow_tests:
                    if name not in source_grants:
                        raise ValueError(
                            "Source preauthorization is unavailable; cannot issue a fresh grant during handoff."
                        )
                source_meta = db._get(conn, "session_meta", source)
                if not source_meta or not source_meta.get("close_complete"):
                    raise ValueError(
                        "Source cleanup is not confirmed; finish its recovery before handoff."
                    )
                db._put(
                    conn,
                    "session_meta",
                    source,
                    {
                        **source_meta,
                        "handoff_to": run["id"],
                        "revision": source_meta["revision"] + 1,
                    },
                )
                run.update(
                    test_grants=source_grants,
                    test_runs=tests["runs"],
                    test_seconds=tests["seconds"],
                    test_attempts={"source_session": tests["runs"]},
                    test_limits={
                        "max_runs": tests["max_runs"],
                        "seconds": tests["total_seconds"],
                    },
                    submitted=True,
                    handoff_from=record_id,
                    session_refs=[source, run["session_id"]],
                    expected_binding=saved["model_binding"],
                    expected_models=saved["model_registry"],
                )
                db._put(
                    conn,
                    "interaction_tasks",
                    task["id"],
                    {
                        **task,
                        "state": "accepted",
                        "target": {"kind": "run", "id": run["id"]},
                        "handoff_id": record_id,
                        "route": {**task["route"], "execution_mode": "managed"},
                        "config": config,
                    },
                )

            try:
                return store.create(spec, _accept=accept)
            except Exception:
                current = interaction.get(task["id"])
                if (
                    current.get("handoff_id")
                    and current.get("target", {}).get("kind") == "run"
                ):
                    return store.get(current["target"]["id"])
                raise
    finally:
        leases.close()
