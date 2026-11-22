"""Native Work controls; waiting policy is independent of execution lifetime."""

import uuid
from .work_sources import WorkSources
from .protocol import fingerprint


def integration_control(runtime, identity, operation, revision, caller=None):
    from pathlib import Path

    work = runtime.store.work(identity, runtime.id)
    if work["revision"] != revision or (
        operation != "inspect" and work.get("cancel_requested")
    ):
        raise ValueError("Work revision or cancellation changed.")
    if operation not in {"continue", "abort", "inspect"}:
        raise ValueError("Choose continue, abort or inspect.")
    if not work.get("continuation", {}).get("integration"):
        raise ValueError("This Work has no selected integration.")
    if operation != "inspect" and not runtime.maintenance.store.valid_authority(work):
        raise ValueError("Current Work authority is required.")
    if operation != "inspect" and work.get("run_id"):
        handle = runtime.child_for(work).runs.get(work["run_id"], control=True)
        for row in handle.executions.list():
            if (
                row.execution_pending
                and row.ref.id != caller
                and not row.details.get("read_only")
            ):
                raise ValueError(
                    "An execution still writes or tests this source; settle it before integration."
                )
    git = runtime.maintenance.delivery.git
    workspace = work["run_workspace"]
    repo = git.run(["rev-parse", "--absolute-git-dir"], cwd=workspace).decode().strip()
    rebasing = any(
        Path(
            git.run(["--git-dir=" + repo, "rev-parse", "--git-path", name])
            .decode()
            .strip()
        ).exists()
        for name in ("rebase-merge", "rebase-apply")
    )
    merging = Path(
        git.run(["--git-dir=" + repo, "rev-parse", "--git-path", "MERGE_HEAD"])
        .decode()
        .strip()
    ).exists()
    env = {
        "GIT_EDITOR": "true",
        "GIT_AUTHOR_NAME": git.values["author_name"],
        "GIT_COMMITTER_NAME": git.values["author_name"],
        "GIT_AUTHOR_EMAIL": git.values["author_email"],
        "GIT_COMMITTER_EMAIL": git.values["author_email"],
    }
    if operation == "continue" and (rebasing or merging):
        paths = (
            git.run(
                ["--git-dir=" + repo, "diff", "--name-only", "--diff-filter=U"],
                cwd=workspace,
            )
            .decode()
            .splitlines()
        )
        from .maintenance_spec import within

        if any(not within(p, work["run_spec"]["write_paths"]) for p in paths):
            raise ValueError("Conflict resolution exceeds the Work write scope.")
        if paths:
            git.run(["--git-dir=" + repo, "add", "--", *paths], cwd=workspace)
        command = ["rebase", "--continue"] if rebasing else ["commit", "--no-edit"]
        git.run(
            ["--git-dir=" + repo, *command],
            cwd=workspace,
            env_extra=env,
            accepted_codes=(0, 1),
        )
    elif operation == "abort" and (rebasing or merging):
        git.run(
            ["--git-dir=" + repo, "rebase" if rebasing else "merge", "--abort"],
            cwd=workspace,
            env_extra=env,
        )
    conflicts = (
        git.run(
            ["--git-dir=" + repo, "diff", "--name-only", "--diff-filter=U"],
            cwd=workspace,
        )
        .decode()
        .splitlines()
    )
    active = any(
        Path(
            git.run(["--git-dir=" + repo, "rev-parse", "--git-path", name])
            .decode()
            .strip()
        ).exists()
        for name in ("rebase-merge", "rebase-apply", "MERGE_HEAD")
    )
    result = {
        "id": identity,
        "state": "conflicted" if conflicts else "active" if active else "settled",
        "conflicts": conflicts,
        "head": git.head(workspace),
        "operation": operation,
    }
    if operation != "inspect":
        with runtime.store.db.write() as conn:
            runtime.store.guard(conn, runtime.header)
            current = runtime.store.db._get(conn, "service_work", identity)
            if current["revision"] != revision:
                raise ValueError(
                    "Work changed during Git control; inspect its current source."
                )
            if operation == "abort":
                original_target = current["continuation"].get("pre_integration_target")
                if original_target:
                    current["continuation"]["target_commit"] = original_target
                from homebrew_harness.execution.source_snapshot import snapshot

                current["baseline"] = {
                    "head": result["head"],
                    "files": snapshot(
                        Path(workspace), **current["run_spec"]["snapshot_limits"]
                    ),
                }
                result["state"] = "aborted"
            current["continuation"].update(
                conflicts=conflicts, integration_state=result["state"]
            )
            current["revision"] += 1
            runtime.store.db._put(conn, "service_work", identity, current)
    return result


def command(runtime, method, payload, request_id):
    if not runtime.maintenance.delivery:
        raise ValueError("Work source operations require repository Delivery.")
    request_id = request_id or uuid.uuid4().hex
    identity = payload["id"]
    runtime.store.work(identity, runtime.id)
    if method == "work_observe_target":

        def run():
            source = WorkSources(runtime).target(identity, request_id)
            return {
                k: source.get(k)
                for k in ("source_ref", "role", "commit", "content_hash", "mutable")
            }

        return runtime.work_executions.invoke(
            identity,
            method,
            {"id": identity},
            run,
            request_id=request_id,
            execution=payload.get("execution"),
            read_only=True,
        )
    if method == "work_integration_control":
        return runtime.work_executions.invoke(
            identity,
            method,
            payload,
            lambda: integration_control(
                runtime,
                identity,
                payload["operation"],
                payload["revision"],
                payload.get("caller_execution_id"),
            ),
            request_id=request_id,
            execution=payload.get("execution"),
            read_only=payload["operation"] == "inspect",
        )
    work = runtime.store.work(identity, runtime.id)
    if method == "work_apply_recovery":
        plan = work.get("recovery_plan") or {}
        if not plan.get("action") or payload.get("job_id") not in {
            None,
            plan.get("job_id"),
        }:
            raise ValueError(
                "Select the current recovery proposal with an explicit action."
            )
        from .job_contracts import work_context

        if plan["basis"] != fingerprint(work_context(runtime.maintenance.store, work)):
            raise ValueError("Recovery proposal facts changed; request a current plan.")
        action = {**plan["action"], "start": payload.get("start", False)}
    else:
        action = payload["action"]
    from .work_actions import perform, validate_action

    validate_action(action)
    return runtime.work_executions.invoke(
        identity,
        method,
        {**payload, "action": action},
        lambda: perform(runtime, identity, action, payload["revision"], request_id),
        request_id=request_id,
        execution=payload.get("execution"),
    )
