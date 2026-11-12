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


GIT_METADATA_LIMIT = 100000


def _git(workdir):
    root = canonical_root(workdir)
    git = WorktreeManager(root, None)
    top = Path(git._git(["rev-parse", "--show-toplevel"]).stdout.strip()).resolve()
    if top != root:
        raise ValueError("Git inspection requires the bound repository root.")
    return git


def _status_records(git, *, untracked):
    result = git._git(
        [
            "--no-optional-locks",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=" + ("all" if untracked else "no"),
        ]
    )
    values = iter(result.stdout.split("\0"))
    count = 0
    for raw in values:
        check()
        if not raw:
            continue
        count += 1
        if count > GIT_METADATA_LIMIT:
            raise ValueError(
                "Git metadata limit reached; the requested changes were not fully inspected."
            )
        if len(raw) < 4 or raw[2] != " ":
            raise ValueError("Invalid Git status metadata.")
        code, path = raw[:2], raw[3:]
        original = next(values, None) if "R" in code or "C" in code else None
        if ("R" in code or "C" in code) and not original:
            raise ValueError("Incomplete Git rename metadata.")
        yield {
            "status": code,
            "path": path,
            **({"original": original} if original else {}),
        }


def _entry_visible(git, entry):
    return _visible(git.workdir, entry["path"]) and (
        "original" not in entry or _visible(git.workdir, entry["original"])
    )


def git_status(*, workdir):
    git = _git(workdir)
    entries = []
    for row in _status_records(git, untracked=True):
        if _entry_visible(git, row):
            entries.append(row)
            if len(entries) > 200:
                break
    return json.dumps(
        redact({"files": entries[:200], "truncated": len(entries) > 200}),
        ensure_ascii=False,
    )


def _untracked(git, paths):
    result = git._git(
        [
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            *(
                ["--", *(":(literal)" + p for p in sorted(paths))]
                if paths is not None
                else []
            ),
        ]
    )
    found = []
    for index, path in enumerate(filter(None, result.stdout.split("\0"))):
        check()
        if index >= GIT_METADATA_LIMIT:
            raise ValueError("Git untracked metadata limit reached.")
        if (paths is None or path in paths) and _visible(git.workdir, path):
            found.append(path)
            if len(found) > 200:
                break
    return found[:200], len(found) > 200


def git_diff(*, workdir, paths=None):
    git = _git(workdir)
    if paths is not None and (
        not isinstance(paths, list)
        or not 1 <= len(paths) <= 32
        or any(not isinstance(p, str) or not _visible(git.workdir, p) for p in paths)
    ):
        raise ValueError("Diff paths must be 1-32 visible relative workspace files.")
    requested = set(paths) if paths is not None else None
    selected, entries, reason = set(), 0, None
    for row in _status_records(git, untracked=False):
        pair = {row["path"]} | ({row["original"]} if "original" in row else set())
        if requested is not None and not requested.intersection(pair):
            continue
        if not _entry_visible(git, row):
            continue
        entries += 1
        if requested is None and entries > 32:
            reason = "file_limit"
            break
        selected.update(pair)
    untracked, truncated = [], False
    remaining = requested - selected if requested is not None else None
    if not selected or remaining:
        untracked, truncated = _untracked(git, remaining)
    if not selected:
        return json.dumps(
            redact(
                {
                    "diff": "",
                    "truncated": truncated,
                    "untracked": untracked,
                    "stop_reason": "untracked_limit" if truncated else None,
                }
            ),
            ensure_ascii=False,
        )
    # Do not rediscover rename partners that were not checked above.
    result = git._git(
        [
            "diff",
            "HEAD",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--",
            *(":(literal)" + p for p in sorted(selected)),
        ]
    )
    if len(result.stdout) > 12000:
        reason = "output_limit"
    return json.dumps(
        redact(
            {
                "diff": result.stdout[:12000],
                "truncated": reason is not None,
                "stop_reason": reason,
                **({"untracked": untracked} if requested is not None else {}),
            }
        ),
        ensure_ascii=False,
    )


def diff_snapshot(*, workdir, base=None, paths=None):
    """A bounded review snapshot; never accepts executable Git options."""
    import hashlib
    from homebrew_harness.security.file_visibility import host_managed_parts

    def project_visible(path):
        return _visible(git.workdir, path) and not host_managed_parts(Path(path).parts) and ".worktrees" not in Path(path).parts

    git = _git(workdir)
    if base is not None and (not isinstance(base, str) or not base or len(base) > 200 or base.startswith("-") or any(ord(c) < 32 for c in base)):
        raise ValueError("Base must be a short Git revision.")
    if paths is not None and (not 1 <= len(paths) <= 32 or any(not project_visible(p) for p in paths)):
        raise ValueError("Diff paths must be 1-32 visible workspace paths.")
    revision = git._git(["rev-parse", "--verify", "--end-of-options", (base or "HEAD") + "^{commit}"]).stdout.strip()
    head = git._git(["rev-parse", "HEAD"]).stdout.strip()
    names = git._git(["diff", "--name-only", "-z", "--no-renames", "--no-ext-diff", "--no-textconv", revision]).stdout.split("\0")
    requested = set(paths) if paths else None
    changed = sorted({p for p in names if p and project_visible(p) and (requested is None or p in requested)})
    raw_untracked = git._git(["ls-files", "--others", "--exclude-standard", "-z"]).stdout.split("\0")
    if len(raw_untracked) > GIT_METADATA_LIMIT:
        raise ValueError("Git untracked metadata limit reached.")
    untracked = sorted(p for p in raw_untracked if p and project_visible(p) and (requested is None or p in requested))
    limited = len(untracked) > 200
    untracked = untracked[:200]
    truncated = limited or len(set(changed) | set(untracked)) > 32
    selected = sorted(set(changed) | set(untracked))[:32]
    patch = git._git(["diff", "--no-renames", "--no-ext-diff", "--no-textconv", "--no-color", revision, "--",
        *(":(literal)" + p for p in selected)]).stdout if selected else ""
    fingerprint = hashlib.sha256((revision + head + patch).encode())
    binary = []
    for path in selected:
        check()
        try:
            with SafeFile(git.workdir, path) as source, source.open_read() as stream:
                raw = stream.read(1_000_001)
                source.verify_binding()
        except FileNotFoundError:
            raw = b""
        fingerprint.update(path.encode() + hashlib.sha256(raw).digest())
        if len(raw) > 1_000_000:
            truncated = True
        if b"\0" in raw and path not in binary:
            binary.append(path)
        if path in untracked:
            try:
                text = raw.decode("utf-8")
                if "\0" in text:
                    raise UnicodeError()
            except UnicodeError:
                if path not in binary:
                    binary.append(path)
            else:
                patch += "\n--- /dev/null\n+++ " + path + "\n" + "".join("+" + line + "\n" for line in text.splitlines())
    truncated |= len(patch) > 12000
    return redact({"workspace": str(git.workdir), "base": revision, "head": head, "paths": selected, "diff": patch[:12000],
        "untracked": untracked[:32], "binary": binary, "truncated": truncated,
        "fingerprint": fingerprint.hexdigest()})
