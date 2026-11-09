"""Bounded text and Git inspection without an arbitrary command interface."""

import json
from pathlib import Path

from homebrew_harness.execution.execution_control import check
from homebrew_harness.orchestration.worktrees import WorktreeManager
from homebrew_harness.security.data_boundary import redact, sensitive_path
from homebrew_harness.security.safe_files import (
    SafeFile,
    canonical_root,
    relative_parts,
)
from .file_iteration import FileWalk
from homebrew_harness.security.file_visibility import assert_visible, visible

QUERY_TOOLS = {"search_text", "git_status", "git_diff"}


def search_text(text, *, workdir, pattern="**/*", limit=50):
    if (
        not isinstance(text, str)
        or not 1 <= len(text) <= 1000
        or type(limit) is not int
        or not 1 <= limit <= 100
    ):
        raise ValueError("Search needs a literal string and a limit between 1 and 100.")
    walk = FileWalk(workdir, pattern)
    found, skipped, total, reason = [], 0, 0, None
    for path in walk:
        check()
        try:
            assert_visible(walk.root, path)
            with SafeFile(walk.root, path) as target:
                content, _, _ = target.snapshot(
                    max_bytes=min(2_000_000, 8_000_000 - total)
                )
                assert_visible(walk.root, path)
        except (OSError, ValueError):
            skipped += 1
            continue
        if content is None:
            skipped += 1
            continue
        total += len(content)
        try:
            decoded = content.decode("utf-8")
        except UnicodeError:
            skipped += 1
            continue
        if "\0" in decoded:
            skipped += 1
            continue
        for line, value in enumerate(decoded.splitlines(), 1):
            check()
            column = value.find(text)
            if column >= 0:
                start = max(0, column - 200)
                found.append(
                    {
                        "path": path,
                        "line": line,
                        "column": column + 1,
                        "text": value[start : start + 1000],
                    }
                )
                if len(found) == limit:
                    reason = "match_limit"
                    break
        if reason:
            break
        if total >= 8_000_000:
            reason = "byte_limit"
            break
    return json.dumps(
        redact(
            {
                "matches": found,
                "truncated": bool(reason)
                or walk.truncated
                or bool(skipped or walk.skipped),
                "skipped_files": skipped + walk.skipped,
                "stop_reason": reason
                or walk.reason
                or ("unreadable_file" if skipped else None),
            }
        ),
        ensure_ascii=False,
    )


def _visible(root, path):
    try:
        if not path or Path(path).is_absolute() or ".." in Path(path).parts:
            return False
        if not visible(root, path):
            return False
        parts = relative_parts(path, root)
        if sensitive_path(parts):
            return False
        with SafeFile(root, path):
            pass
        return not (root / path).is_symlink()
    except FileNotFoundError:
        # Deleted files still have inspectable changes; validate their existing ancestors.
        target = root
        for part in Path(path).parts:
            target /= part
            if target.is_symlink():
                return False
        return True
    except (OSError, ValueError):
        return False


def _status(workdir):
    root = canonical_root(workdir)
    git = WorktreeManager(root, None)
    top = Path(git._git(["rev-parse", "--show-toplevel"]).stdout.strip()).resolve()
    if top != root:
        raise ValueError("Git inspection requires the bound repository root.")
    result = git._git(
        [
            "--no-optional-locks",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        ]
    )
    entries, values = [], iter(result.stdout.split("\0"))
    for raw in values:
        if not raw:
            continue
        code, path = raw[:2], raw[3:]
        original = next(values, "") if "R" in code or "C" in code else None
        if (
            not _visible(root, path)
            or original is not None
            and not _visible(root, original)
        ):
            continue
        entries.append(
            {
                "status": code,
                "path": path,
                **({"original": original} if original else {}),
            }
        )
        if len(entries) > 200:
            break
    return git, entries


def git_status(*, workdir):
    _, entries = _status(workdir)
    return json.dumps(
        redact({"files": entries[:200], "truncated": len(entries) > 200}),
        ensure_ascii=False,
    )


def git_diff(*, workdir, paths=None):
    git, entries = _status(workdir)
    if paths is not None and (
        not isinstance(paths, list)
        or not 1 <= len(paths) <= 32
        or any(not isinstance(p, str) or not _visible(git.workdir, p) for p in paths)
    ):
        raise ValueError("Diff paths must be 1-32 visible relative workspace files.")
    tracked = {v["path"] for v in entries if v["status"] != "??"}
    selected = sorted(tracked if paths is None else tracked & set(paths))[:32]
    if not selected:
        return json.dumps(
            {
                "diff": "",
                "truncated": len(entries) > 200,
                "untracked": [v["path"] for v in entries if v["status"] == "??"],
            }
        )
    result = git._git(
        [
            "diff",
            "HEAD",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--",
            *(":(literal)" + p for p in selected),
        ]
    )
    return json.dumps(
        redact(
            {
                "diff": result.stdout[:12000],
                "truncated": len(result.stdout) > 12000
                or len(tracked) > 32
                or len(entries) > 200,
            }
        ),
        ensure_ascii=False,
    )
