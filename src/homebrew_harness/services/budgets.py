"""Stopped-service owner configuration; permissions and private references stay fixed."""

import copy
from pathlib import Path
from .spec import ServiceSpec
from .runtime import saved_config
from .protocol import fingerprint
from homebrew_harness.runs.budgets import FIELDS as RUN_FIELDS

FIELDS = {
    "run_defaults": RUN_FIELDS,
    "maintenance": {
        "authority_seconds",
        "job_seconds",
        "job_turns",
        "job_attempts",
        "job_warning_seconds",
        "job_call_warning",
        "job_attempt_warning",
    },
    "delivery": {
        "gate_seconds",
        "authorization_seconds",
        "ci_wait_seconds",
        "ci_wait_warning_seconds",
        "ci_fix_limit",
        "ci_fix_warning",
        "auto_ci_fix",
    },
    "communication": {"authorization_seconds", "prepare_seconds", "request_seconds"},
    "config": {
        "total_seconds",
        "turn_seconds",
        "max_turns",
        "model_call_warning",
        "turn_warning_seconds",
        "approval_review",
        "runtime_limits",
    },
}


def configure(
    store, identity, changes, revision, request_id, *, work_id=None, run_changes=None
):
    if not isinstance(changes, dict) or not changes or set(changes) - set(FIELDS):
        raise ValueError("Unknown Service budget group.")
    for group, values in changes.items():
        if not isinstance(values, dict) or set(values) - set(FIELDS[group]):
            raise ValueError(
                "Only progress budgets and explicit approval delegation can be reconfigured."
            )
    meta = store.get(identity)
    spec, config = copy.deepcopy(meta["spec"]), copy.deepcopy(meta["config"])
    for group, values in changes.items():
        if group == "config":
            config.update(values)
        else:
            spec[group] = {**spec.get(group, {}), **values}
    ServiceSpec(**spec)
    saved_config(config)
    key = "budgets:" + identity + ":" + request_id
    payload = fingerprint([changes, work_id, run_changes])
    prior = store.runtime.get("service_commands", key)
    if prior and prior.get("result") is not None:
        if prior["payload"] != payload:
            raise ValueError("Budget request ID has another payload.")
        return prior["result"]
    if prior and prior["payload"] != payload:
        raise ValueError("Budget request ID has another payload.")
    if meta["state"] != "stopped" or not meta["resources_closed"] or meta.get("owner"):
        raise ValueError(
            "Service budget configuration requires its stopped owner state."
        )
    if not prior and meta["revision"] != revision:
        raise ValueError("Service configuration revision changed.")
    work = store.work(work_id, identity) if work_id else None
    if run_changes and (not work or not work.get("pause_requested")):
        raise ValueError("The bound Work must remain paused.")
    if run_changes is not None:
        if (
            not isinstance(run_changes, dict)
            or not run_changes
            or set(run_changes) - RUN_FIELDS
        ):
            raise ValueError(
                "Run budget migration requires known nonempty budget fields."
            )
        from homebrew_harness.runs.spec import RunSpec

        RunSpec.from_dict({**work["run_spec"], **run_changes})
        if not prior:
            from homebrew_harness.runs.store import RunStore

            check_store = RunStore(Path(work.get("run_workspace") or spec["workspace"]))
            try:
                bound = check_store.get(work["run_id"])
                if (
                    bound["status"] not in {"paused", "blocked"}
                    or not bound.get("cleanup_complete")
                    or bound.get("finalized_at")
                ):
                    raise ValueError(
                        "Budget migration requires its original quiescent Run before admission."
                    )
            finally:
                check_store.close()
    prepared = prior or {
        "payload": payload,
        "phase": "prepared",
        "result": None,
        "changes": changes,
        "run_changes": run_changes,
        "work_id": work_id,
        "previous_spec": meta["spec"],
        "previous_config": meta["config"],
        "previous_work_revision": work["revision"] if work else None,
        "previous_run_spec": work.get("run_spec") if work else None,
        "run_id": work.get("run_id") if work else None,
        "run_workspace": work.get("run_workspace") if work else None,
        "checkpoint": "service:" + identity,
    }
    if not prior:
        with store.db.write() as conn:
            current = store.db._get(conn, "service_meta", identity)
            if current["revision"] != revision or current["state"] != "stopped":
                raise ValueError("Service changed before budget admission.")
            pending = conn.execute(
                "SELECT key FROM records WHERE section='service_commands' AND owner=? AND key<>? AND json_extract(value,'$.phase')='prepared' LIMIT 1",
                ("service:" + identity, key),
            ).fetchone()
            if pending:
                raise ValueError(
                    "Finish the accepted budget migration before another configuration."
                )
            store.db._put(conn, "service_commands", key, prepared)
    native = None
    try:
        # Hold the Service transaction through the idempotent Run step. The
        # previously committed admission survives a crash between databases.
        with store.db.write() as conn:
            current = store.db._get(conn, "service_meta", identity)
            if (
                current["state"] != "stopped"
                or not current["resources_closed"]
                or current.get("owner")
            ):
                raise ValueError("Service changed during budget preparation.")
            # A same-request replay reapplies only the admitted budget fields to
            # current configuration. Unrelated instructions and settings survive.
            spec, config = copy.deepcopy(current["spec"]), copy.deepcopy(
                current["config"]
            )
            for group, values in changes.items():
                if group == "config":
                    config.update(values)
                else:
                    spec[group] = {**spec.get(group, {}), **values}
            ServiceSpec(**spec)
            saved_config(config)
            actual = store.db._get(conn, "service_work", work_id) if work else None
            if actual and (
                not actual.get("pause_requested")
                or actual.get("run_id") != prepared.get("run_id", work.get("run_id"))
                or actual.get("run_workspace")
                != prepared.get("run_workspace", work.get("run_workspace"))
            ):
                raise ValueError(
                    "The accepted budget migration belongs to the original paused Run and workspace; restore that binding before replaying it."
                )
            if actual and run_changes:
                previous_spec = prepared["previous_run_spec"] or {}
                other_keys = (set(actual["run_spec"]) | set(previous_spec)) - set(
                    run_changes
                )
                if any(
                    actual["run_spec"].get(k) != previous_spec.get(k)
                    for k in other_keys
                ):
                    raise ValueError(
                        "The original Run contract changed outside the accepted budget fields; inspect its migration record before replay."
                    )
            if run_changes:
                from homebrew_harness.runs.store import RunStore

                native = RunStore(
                    Path(actual.get("run_workspace") or spec["workspace"])
                )
                run = native.get(actual["run_id"])
                native.configure_budgets(
                    run["id"], run_changes, run["revision"], "service-" + request_id
                )
            refreshed = store.db._get(conn, "service_meta", identity)
            if (
                refreshed["revision"] != current["revision"]
                or refreshed["state"] != "stopped"
            ):
                raise ValueError(
                    "Service changed while committing the Run budget step."
                )
            current.update(spec=spec, config=config, revision=current["revision"] + 1)
            store.db._put(conn, "service_meta", identity, current)
            if actual and run_changes:
                actual.update(
                    run_spec={**actual["run_spec"], **run_changes},
                    revision=actual["revision"] + 1,
                )
                store.db._put(conn, "service_work", work_id, actual)
            result = {
                "id": identity,
                "revision": current["revision"],
                "configured_groups": sorted(changes),
                "work_id": work_id,
                "execution_resumed": False,
                "binding_refresh_required": True,
            }
            store.db._put(
                conn,
                "service_commands",
                key,
                {
                    **prepared,
                    "phase": "completed",
                    "result": result,
                    "previous_configured": {
                        g: meta["config"] if g == "config" else meta["spec"].get(g, {})
                        for g in changes
                    },
                    "configured": changes,
                },
            )
            return result
    except Exception as error:
        from homebrew_harness.runtime.errors import boundary_error

        raise boundary_error(
            ValueError,
            "budget_migration_pending",
            "Accepted budget migration did not finish. Its Run step may already be committed; replay the same request after checking the stopped Service/Work. Admission record: "
            + key,
            migration_key=key,
            recovery_required=True,
        ) from error
    finally:
        if native:
            native.close()
