"""Task-bound Git worktrees. Removal is a host operation, never a model tool."""

from __future__ import annotations

import json
import os
import re
from dataclasses import replace
from pathlib import Path

from homebrew_harness.execution.background import ShellRunner
from homebrew_harness.tools.tool_outcome import ToolExecutionError


def validate_worktree_name(name: str) -> str:
    if (
        not isinstance(name, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name)
        or ".." in name
    ):
        raise ValueError(
            "Invalid worktree name; use letters, digits, dots, underscores or dashes without '..'."
        )
    return name


class WorktreeManager:
    def __init__(self, workdir: Path, store):
        self.workdir = workdir.resolve()
        self.store = store
        self.root = self.workdir / ".worktrees"

    def path(self, name: str) -> Path:
        validate_worktree_name(name)
        path = self.root / name
        if self.root.is_symlink() or path.is_symlink() or path.resolve() != path:
            raise ValueError(
                "Worktree directories must not be symlinks or escape the workspace."
            )
        return path

    @staticmethod
    def branch(name: str) -> str:
        return "codex/wt/" + validate_worktree_name(name)

    def _git(self, args, *, cwd=None, check=True, on_started=None):
        # Git may execute configured programs even for status. This host path
        # accepts no ambient Git overrides or checkout/clean/process filters.
        env = {
            "PATH": os.defpath,
            "LANG": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
        prefix = [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-c",
            "core.attributesFile=/dev/null",
            "-c",
            "diff.external=",
            "-c",
            "credential.helper=",
        ]
        runner = ShellRunner()
        try:
            # Refuse includes even when currently inactive: checkout can change
            # branch/worktree conditions. Inspect both files without expanding
            # includes; system/global/ambient configuration is disabled above.
            unsafe = r"^(include\.path|includeif\..*\.path|filter\..*\.(clean|smudge|process))$"
            scoped = runner.run(
                [
                    *prefix,
                    "config",
                    "--local",
                    "--no-includes",
                    "--bool",
                    "extensions.worktreeConfig",
                ],
                cwd or self.workdir,
                env,
                30,
            )
            if scoped.returncode not in {0, 1} or getattr(
                scoped, "output_limit", False
            ):
                raise ValueError("Cannot validate Git worktree configuration.")
            scopes = (
                ("--local", "--worktree")
                if scoped.stdout.strip() == "true"
                else ("--local",)
            )
            for scope in scopes:
                config = runner.run(
                    [
                        *prefix,
                        "config",
                        scope,
                        "--no-includes",
                        "--name-only",
                        "--get-regexp",
                        unsafe,
                    ],
                    cwd or self.workdir,
                    env,
                    30,
                )
                if (
                    config.returncode not in {0, 1}
                    or config.stdout.strip()
                    or getattr(config, "output_limit", False)
                ):
                    raise ValueError(
                        "Automatic Git operations refuse config includes and checkout filters; use a host-managed Git workflow."
                    )
            result = runner.run(
                [*prefix, *args],
                cwd or self.workdir,
                env,
                30,
                **({"on_started": on_started} if on_started is not None else {}),
            )
        finally:
            runner.close(timeout=0)
        if getattr(result, "output_limit", False):
            raise ValueError(
                "Git output limit reached; inspect possible partial effects."
            )
        if check and result.returncode:
            raise ValueError(
                "Git worktree operation failed: " + result.stderr.strip()[:2000]
            )
        return result

    def _registry(self) -> dict[Path, dict]:
        raw = self._git(["worktree", "list", "--porcelain", "-z"]).stdout
        entries, current = {}, {}
        for token in raw.split("\0"):
            if not token:
                if "worktree" in current:
                    entries[Path(current["worktree"]).resolve()] = current
                current = {}
            else:
                key, _, value = token.partition(" ")
                current[key] = value
        return entries

    def resolve(self, name: str) -> Path:
        path = self.path(name)
        entry = self._registry().get(path)
        if (
            not path.is_dir()
            or entry is None
            or entry.get("branch") != "refs/heads/" + self.branch(name)
        ):
            raise ValueError(
                "Task worktree is missing or no longer registered on its expected branch."
            )
        return path

    def run_tool(self, name, task_id, *, workdir):
        if workdir.resolve() != self.workdir:
            raise ValueError("Worktree creation is bound to the base workspace.")
        return json.dumps(self.create(name=name, task_id=task_id), ensure_ascii=False)

    def create(self, name: str, task_id: str) -> dict:
        path, branch = self.path(name), self.branch(name)
        with self.store._lock():
            task = self.store._load(task_id)
            if (
                task.status != "pending"
                or task.owner is not None
                or task.worktree is not None
            ):
                raise ValueError(
                    "Worktree binding requires a pending unowned unbound task."
                )
            if any(item.worktree == name for item in self.store._list()):
                raise ValueError("Worktree is already bound to another task.")
            if (
                Path(
                    self._git(["rev-parse", "--show-toplevel"]).stdout.strip()
                ).resolve()
                != self.workdir
            ):
                raise ValueError("Workdir must be the Git repository root.")
            dirty = self._git(
                ["status", "--porcelain=v1", "--untracked-files=normal"]
            ).stdout.strip()
            if dirty:
                raise ValueError(
                    "Current workspace has uncommitted or untracked files; a HEAD worktree would omit them. Preserve a clean baseline before creating a task worktree."
                )
            baseline = self._git(["rev-parse", "HEAD"]).stdout.strip()
            self._git(["check-ref-format", "--branch", branch])
            if (
                path.exists()
                or path in self._registry()
                or self._git(
                    ["show-ref", "--verify", "--quiet", "refs/heads/" + branch],
                    check=False,
                ).returncode
                == 0
            ):
                raise ValueError("Worktree path or branch already exists.")
            self.root.mkdir(mode=0o700, exist_ok=True)
            details = {
                "task_id": task.id,
                "worktree": name,
                "path": path.relative_to(self.workdir).as_posix(),
                "expected_branch": branch,
                "phase": "checkout",
                "binding": "not_attempted",
            }
            launched = False

            def started(_process):
                nonlocal launched
                launched = True

            try:
                result = self._git(
                    ["worktree", "add", "-b", branch, str(path), "HEAD"],
                    check=False,
                    on_started=started,
                )
            except Exception as error:
                if not launched:
                    raise
                raise ToolExecutionError(
                    "Worktree command was accepted but its result is unavailable; inspect retained artifacts.",
                    outcome="unknown",
                    details={**details, "error_type": type(error).__name__},
                ) from error
            if result.returncode:
                observed = {}
                try:
                    observed["path_exists"] = path.exists()
                    if not observed["path_exists"]:
                        observed["registered"] = path in self._registry()
                    if not any(observed.values()):
                        observed["branch_exists"] = (
                            self._git(
                                [
                                    "show-ref",
                                    "--verify",
                                    "--quiet",
                                    "refs/heads/" + branch,
                                ],
                                check=False,
                            ).returncode
                            == 0
                        )
                except Exception as error:
                    raise ToolExecutionError(
                        "Worktree command failed and retained artifacts could not be verified.",
                        outcome="unknown",
                        details={
                            **details,
                            "observed": observed,
                            "error_type": type(error).__name__,
                        },
                    ) from error
                if any(observed.values()):
                    raise ToolExecutionError(
                        "Partial worktree creation; Git artifacts were retained and task remains unbound.",
                        outcome="partial",
                        details={**details, "observed": observed},
                    )
                raise ValueError(
                    "Git could not create worktree: " + result.stderr.strip()[:2000]
                )
            details["phase"] = "verification"
            details["checkout"] = "unverified"
            try:
                self.resolve(name)
                details.update(phase="binding", checkout="verified", binding="unknown")
                self.store._write(replace(task, worktree=name))
            except Exception as error:
                raise ToolExecutionError(
                    "Worktree creation or task binding failed after Git completed; inspect the checkout and current task record.",
                    outcome="unknown",
                    details={**details, "error_type": type(error).__name__},
                ) from error
        return {
            "task_id": task.id,
            "worktree": name,
            "path": str(path),
            "branch": branch,
            "baseline": baseline,
        }

    def remove(
        self, name: str, *, leased_paths=(), discard_changes: bool = False
    ) -> dict:
        """The host must obtain explicit approval before choosing discard_changes."""
        with self.store._lock():
            path = self.resolve(name)
            bound = [task for task in self.store._list() if task.worktree == name]
            if not bound or any(task.status != "completed" for task in bound):
                raise ValueError("Complete bound tasks before removing their worktree.")
            if path in {Path(item).resolve() for item in leased_paths}:
                raise ValueError("Worktree is still leased by an active work turn.")
            dirty = self._git(
                ["status", "--porcelain=v1", "--ignored"], cwd=path
            ).stdout.strip()
            if dirty and not discard_changes:
                raise ValueError(
                    "Worktree has tracked, untracked or ignored changes; preserve them before removal."
                )
            self._git(
                [
                    "worktree",
                    "remove",
                    *(["--force"] if discard_changes else []),
                    str(path),
                ]
            )
            try:
                for task in bound:
                    self.store._write(replace(task, worktree=None))
            except Exception as error:
                raise ValueError(
                    "Worktree removed and branch retained, but task unbinding failed; repair its record."
                ) from error
        return {"removed": name, "branch_retained": self.branch(name)}
