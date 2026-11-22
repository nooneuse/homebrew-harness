"""Explicit same-Work source selection; local preparations are retained for replay."""

import hashlib
import json
from pathlib import Path
from homebrew_harness.execution.source_snapshot import snapshot
from homebrew_harness.execution.execution_control import check
from homebrew_harness.security.safe_files import SafeFile
from homebrew_harness.runs.spec import RunSpec
from .protocol import request_key, fingerprint
from .work_sources import WorkSources


def validate_action(value):
    fields = {"mode", "source_ref", "reason", "start", "integration"}
    if (
        not isinstance(value, dict)
        or set(value) - fields
        or value.get("mode") not in {"resume", "fork", "adopt_candidate"}
    ):
        raise ValueError(
            "Work action selects resume, fork or adopt_candidate and an owned source."
        )
    if not isinstance(value.get("reason"), str) or not value["reason"].strip():
        raise ValueError("Work action requires its reason.")
    if value.get("mode") != "resume" and not isinstance(value.get("source_ref"), str):
        raise ValueError("Select source_ref explicitly.")
    if "start" in value and type(value["start"]) is not bool:
        raise ValueError("start must be Boolean.")
    integration = value.get("integration")
    if value["mode"] == "resume" and integration is not None:
        raise ValueError(
            "Resume keeps its source; choose fork with a fixed target for integration."
        )
    if integration is not None and (
        not isinstance(integration, dict)
        or set(integration) != {"method", "target_ref"}
        or integration["method"] not in {"merge", "rebase"}
        or not isinstance(integration["target_ref"], str)
    ):
        raise ValueError("Integration selects merge/rebase and one fixed target_ref.")
    return value


def perform(runtime, identity, action, revision, request_id):
    action = validate_action(action)
    sources = WorkSources(runtime)
    work = sources.work(identity)
    key = request_key("work-action", runtime.id, request_id)
    saved = runtime.store.runtime.get("service_control_attempts", key)
    digest = fingerprint([identity, action, revision])
    if saved and saved["payload"] != digest:
        raise ValueError("Work action request has another payload.")
    if saved and saved.get("result"):
        return saved["result"]
    if (
        work.get("cancel_requested")
        or not work.get("pause_requested")
        or work["revision"] != revision
    ):
        raise ValueError(
            "Work action requires its current paused, uncancelled revision."
        )
    if not runtime.maintenance.store.valid_authority(work):
        raise ValueError("Current Work execution authority is required.")
    if work.get("run_id"):
        previous = runtime.child_for(work).run_store.get(work["run_id"])
        if not previous.get("cleanup_complete"):
            raise ValueError("Wait for current Run cleanup before changing source.")
    if action["mode"] == "resume":
        if action.get("source_ref") and action["source_ref"] != "draft:" + identity:
            raise ValueError(
                "Resume keeps the current source; use fork for another source."
            )
        handle = runtime._handle(work)
        if action.get("start", False):
            native = runtime.child_for(work).run_store.get(work["run_id"])
            from homebrew_harness.runtime.limits import expired

            if native["status"] == "blocked" or expired(
                native.get("deadline_at"), __import__("time").time()
            ):
                handle.retry(
                    expires_at=work["authority"]["expires_at"],
                    request_id="work-action-" + key,
                )
            else:
                handle.resume()
            runtime.store.update_work(runtime.header, identity, pause_requested=False)
        return {
            "id": identity,
            "run_id": work["run_id"],
            "mode": "resume",
            "execution_resumed": action.get("start", False),
        }
    prepared, workspace, origin, target_commit, conflicts = prepare_source(
        runtime, identity, action, revision, key, digest, saved, sources
    )
    selected, preserved = prepared["source"], prepared["preserved"]
    integration = action.get("integration")
    prompt = work["prompt"]
    if work.get("requirements_id"):
        from .requirements import instruction

        prompt += instruction(
            runtime.maintenance.store.get("requirements", work["requirements_id"])
        )
    prompt += "\n\nCurrent operator execution context:\n" + "\n\n".join(
        [*work.get("amendments", []), *work.get("execution_context", [])]
    )
    prompt += (
        "\n\nCurrent continuation choice (supersedes earlier integration plans):\n"
        + json.dumps(action, ensure_ascii=False)
    )
    prompt += "\n\n" + runtime.maintenance.store.development_handoff
    scope = work["run_spec"]
    spec = RunSpec(
        **{
            **scope,
            "prompt": prompt,
            "work_context": {
                **scope.get("work_context", {}),
                "control_root": str(runtime.store.root),
                "service_id": runtime.id,
                "work_id": identity,
                "requirements_revision": work["requirements_revision"],
                "source_ref": selected["source_ref"],
                "previous_progress_ref": work.get("progress_ref"),
                "progress": {
                    "summary": action["reason"],
                    "remaining": [],
                    "complete": False,
                },
                "verification": [],
            },
        }
    )
    with runtime.store.db.write() as conn:
        runtime.store.guard(conn, runtime.header)
        current = runtime.store.db._get(conn, "service_work", identity)
        if (
            current["revision"] != revision
            or current.get("cancel_requested")
            or not current.get("pause_requested")
        ):
            raise ValueError(
                "Work changed during continuation preparation; preserved sources remain available."
            )
        if not runtime.maintenance.store.valid_authority(current):
            raise ValueError("Work authority changed during preparation.")
        previous_id = current.get("run_id")
        current.update(
            run_history=[
                *current.get("run_history", []),
                *([previous_id] if previous_id else []),
            ],
            run_id=None,
            run_workspace=str(workspace),
            run_spec=spec.data(),
            baseline={
                "files": snapshot(workspace, **spec.snapshot_limits),
                "head": origin,
            },
            state="approved",
            pause_requested=not action.get("start", False),
            repair_of=current.get("changeset_id"),
            progress_history=[
                *current.get("progress_history", []),
                *([current["progress_ref"]] if current.get("progress_ref") else []),
            ],
            progress_ref=None,
            launch_id="service_" + request_key("continue-work", runtime.id, key),
            continuation={
                "request_id": request_id,
                "source_ref": selected["source_ref"],
                "preserved_ref": preserved["source_ref"],
                "integration": integration,
                "target_commit": target_commit,
                "pre_integration_target": selected.get("target_base")
                or prepared.get("pre_integration_target"),
                "published_head": sources._published(work),
                "conflicts": conflicts,
            },
            revision=current["revision"] + 1,
            error=None,
        )
        runtime.store.db._put(conn, "service_work", identity, current)
        result = {
            "id": identity,
            "mode": action["mode"],
            "source_ref": selected["source_ref"],
            "preserved_ref": preserved["source_ref"],
            "workspace": str(workspace),
            "conflicts": conflicts,
            "execution_resumed": False,
            "start_requested": action.get("start", False),
            "revision": current["revision"],
        }
        runtime.store.db._put(
            conn,
            "service_control_attempts",
            key,
            {**prepared, "state": "completed", "result": result},
        )
        runtime.maintenance.store.changed(conn, identity)
    return result


def prepare_source(runtime, identity, action, revision, key, digest, saved, sources):
    preserved = (
        saved["preserved"]
        if saved
        else sources.preserve(identity, "draft:" + identity, "preserve-" + key)
    )
    selected = (
        saved["source"]
        if saved
        else sources.preserve(identity, action["source_ref"], "selected-" + key)
    )
    directory = (
        runtime.store.root
        / ".harness/delivery/repairs"
        / identity
        / "continuations"
        / key
    )
    directory.mkdir(parents=True, exist_ok=True)
    workspace = directory / "workspace"
    workspace.mkdir(exist_ok=True)
    repo = directory / "repo.git"
    prepared = {
        "id": key,
        "payload": digest,
        "work_id": identity,
        "action": action,
        "revision": revision,
        "source": selected,
        "preserved": preserved,
        "workspace": str(workspace),
        "state": "prepared",
        "service_id": runtime.id,
        "checkpoint": "service:" + runtime.id,
    }
    prepared = saved or prepared

    def record(**values):
        prepared.update(values)
        with runtime.store.db.write() as conn:
            runtime.store.guard(conn, runtime.header)
            runtime.store.db._put(conn, "service_control_attempts", key, prepared)

    if prepared.get("phase") == "git_ready":
        return (
            prepared,
            workspace,
            prepared["origin"],
            prepared["target_commit"],
            prepared["conflicts"],
        )
    record()
    git = sources.git
    if prepared.get("phase") not in {"source_ready", "integration_started"}:
        files = snapshot(Path(selected["workspace"]), **selected["snapshot_limits"])
        for path, meta in files.items():
            check()
            with SafeFile(Path(selected["workspace"]), path) as origin:
                raw = origin.snapshot()[0]
            if raw is None or hashlib.sha256(raw).hexdigest() != meta["sha256"]:
                raise ValueError("Selected source changed.")
            with SafeFile(workspace, path, create_parents=True) as target:
                existing, version, _ = target.snapshot()
                if existing is not None and existing != raw:
                    raise ValueError("Prepared continuation has conflicting bytes.")
                if existing is None:
                    target.publish(raw, version, 0o755 if meta["executable"] else 0o644)
        git = sources.git
        if not repo.exists():
            git.run(["init", "--bare", "--template=", str(repo)])
        origin = selected.get("commit")
        source_repo = selected.get("repo")
        if origin and source_repo:
            git.run(
                [
                    "--git-dir=" + str(repo),
                    "fetch",
                    "--no-tags",
                    "--no-recurse-submodules",
                    source_repo,
                    origin,
                ]
            )
        else:
            target = sources.target(identity, "initial-target-" + key)
            origin = target["commit"]
            git.run(
                ["--git-dir=" + str(repo), "fetch", "--no-tags", target["repo"], origin]
            )
        git.run(
            ["--git-dir=" + str(repo), "update-ref", "refs/heads/candidate", origin]
        )
        git.run(
            ["--git-dir=" + str(repo), "symbolic-ref", "HEAD", "refs/heads/candidate"]
        )
        git.run(["--git-dir=" + str(repo), "config", "core.bare", "false"])
        git.run(["--git-dir=" + str(repo), "config", "core.worktree", str(workspace)])
        (workspace / ".git").write_text("gitdir: " + str(repo) + "\n")
        git.run(["--git-dir=" + str(repo), "read-tree", origin])
        identity_env = {
            "GIT_AUTHOR_NAME": git.values["author_name"],
            "GIT_COMMITTER_NAME": git.values["author_name"],
            "GIT_AUTHOR_EMAIL": git.values["author_email"],
            "GIT_COMMITTER_EMAIL": git.values["author_email"],
        }
        git.run(["--git-dir=" + str(repo), "add", "--all", "--", "."], cwd=workspace)
        tree = git.run(["--git-dir=" + str(repo), "write-tree"]).decode().strip()
        if (
            tree
            != git.run(["--git-dir=" + str(repo), "rev-parse", origin + "^{tree}"])
            .decode()
            .strip()
        ):
            origin = (
                git.run(
                    [
                        "--git-dir=" + str(repo),
                        "commit-tree",
                        tree,
                        "-p",
                        origin,
                        "-m",
                        "Preserved Work source",
                    ],
                    env_extra=identity_env,
                )
                .decode()
                .strip()
            )
            git.run(
                ["--git-dir=" + str(repo), "update-ref", "refs/heads/candidate", origin]
            )
        record(
            phase="source_ready",
            origin=origin,
            pre_integration_target=selected.get("target_base") or origin,
            target_commit=selected.get("target_base") or origin,
        )
    origin = prepared["origin"]
    integration = action.get("integration")
    target_commit = selected.get("target_base") or origin
    conflicts = []
    if integration:
        target = sources.resolve(identity, integration["target_ref"])
        target_commit = target["commit"]
        if target.get("role") != "target":
            raise ValueError(
                "Integration target must be an observed registered target."
            )
        git.run(
            [
                "--git-dir=" + str(repo),
                "fetch",
                "--no-tags",
                target["repo"],
                target["commit"],
            ]
        )
        identity_env = {
            "GIT_AUTHOR_NAME": git.values["author_name"],
            "GIT_COMMITTER_NAME": git.values["author_name"],
            "GIT_AUTHOR_EMAIL": git.values["author_email"],
            "GIT_COMMITTER_EMAIL": git.values["author_email"],
            "GIT_EDITOR": "true",
        }
        active = any(
            Path(
                git.run(["--git-dir=" + str(repo), "rev-parse", "--git-path", name])
                .decode()
                .strip()
            ).exists()
            for name in ("rebase-merge", "rebase-apply", "MERGE_HEAD")
        )
        current_head = git.head(workspace)
        # If the previous process already applied or paused Git replay, inspect
        # its real files instead of copying the old selected tree over them.
        already_started = prepared.get("phase") == "integration_started" and (
            active or current_head != origin
        )
        command = (
            ["rebase", target["commit"]]
            if integration["method"] == "rebase"
            else ["merge", "--no-edit", target["commit"]]
        )
        if not already_started:
            record(phase="integration_started", target_commit=target_commit)
            git.run(
                ["--git-dir=" + str(repo), *command],
                cwd=workspace,
                env_extra=identity_env,
                accepted_codes=(0, 1),
            )
        conflicts = (
            git.run(
                ["--git-dir=" + str(repo), "diff", "--name-only", "--diff-filter=U"],
                cwd=workspace,
            )
            .decode()
            .splitlines()
        )
        origin = git.head(workspace)
    record(
        phase="git_ready",
        origin=origin,
        target_commit=target_commit,
        conflicts=conflicts,
    )
    return prepared, workspace, origin, target_commit, conflicts
