"""Shared content snapshots and bounded workspace metadata observations."""

import fnmatch
import hashlib
import json
import os
import stat
from pathlib import Path

from homebrew_harness.security.data_boundary import sensitive_path
from homebrew_harness.security.safe_files import SafeFile, canonical_root

_EXCLUDED = {
    ".git",
    ".venv",
    ".harness",
    ".tasks",
    ".mailboxes",
    ".memory",
    ".worktrees",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}


class TestRunnerError(ValueError):
    pass


def validate_limits(value):
    defaults = {"max_files": 5000, "max_bytes": 32000000, "max_paths": 100000}
    if not isinstance(value, dict) or set(value) - set(defaults):
        raise ValueError("Invalid host snapshot limits.")
    result = {**defaults, **value}
    if any(
        type(number) is not int
        or not 1 <= number <= (256000000 if key == "max_bytes" else 1000000)
        for key, number in result.items()
    ):
        raise ValueError("Snapshot limits must be bounded positive integers.")
    return result


def content_digest(records):
    values = {
        name: (value["sha256"], value["executable"]) for name, value in records.items()
    }
    return hashlib.sha256(
        json.dumps(
            values, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


def matches(path, patterns):
    return any(
        fnmatch.fnmatchcase(path, pattern)
        or pattern.startswith("**/")
        and fnmatch.fnmatchcase(path, pattern[3:])
        for pattern in patterns
    )


def snapshot(
    root, patterns=("**/*",), *, max_files=5000, max_bytes=32_000_000, max_paths=100000
):
    root = canonical_root(root)
    records, total, paths_seen = {}, 0, 0
    for directory, dirs, files in os.walk(
        root,
        followlinks=False,
        onerror=lambda error: (_ for _ in ()).throw(
            TestRunnerError("Cannot inspect all declared test inputs.")
        ),
    ):
        from homebrew_harness.execution.execution_control import check

        check()
        parent = Path(directory)
        relative = parent.relative_to(root)
        kept = []
        for name in dirs:
            path = parent / name
            if name in _EXCLUDED or sensitive_path((*relative.parts, name)):
                continue
            if path.is_symlink():
                raise TestRunnerError(
                    "Watched source directories must not be symlinks."
                )
            kept.append(name)
        dirs[:] = kept
        for name in sorted(files):
            paths_seen += 1
            if paths_seen > max_paths:
                raise TestRunnerError(
                    "Source inventory exceeds max_paths="
                    + str(max_paths)
                    + "; narrow the project or adjust the host inventory limit."
                )
            path = (relative / name).as_posix()
            if (
                name in _EXCLUDED
                or sensitive_path(Path(path).parts)
                or not matches(path, patterns)
            ):
                continue
            with SafeFile(root, path) as target, target.open_read() as stream:
                info = os.fstat(stream.fileno())
                total += info.st_size
                if total > max_bytes or len(records) >= max_files:
                    raise TestRunnerError(
                        "Watched source exceeds max_files="
                        + str(max_files)
                        + " or max_bytes="
                        + str(max_bytes)
                        + "; watched patterns="
                        + repr(patterns)
                        + "; register narrower inputs."
                    )
                hasher = hashlib.sha256()
                while chunk := stream.read(65536):
                    check()
                    hasher.update(chunk)
                after = os.fstat(stream.fileno())
                target.verify_binding()
                named = os.stat(
                    target.parts[-1], dir_fd=target.fds[-1], follow_symlinks=False
                )
                version = (
                    info.st_dev,
                    info.st_ino,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                )
                if version != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ) or version != (
                    named.st_dev,
                    named.st_ino,
                    named.st_size,
                    named.st_mtime_ns,
                    named.st_ctime_ns,
                ):
                    raise TestRunnerError("Watched source changed during snapshot.")
                records[path] = {
                    "sha256": hasher.hexdigest(),
                    "version": list(version),
                    "executable": bool(info.st_mode & 0o111),
                }
    return records


def prefix_patterns(paths):
    escaped = {"[": "[[]", "]": "[]]", "*": "[*]", "?": "[?]"}
    result = []
    for path in paths:
        if path == ".":
            result.append("**/*")
        else:
            literal = "".join(escaped.get(char, char) for char in path.rstrip("/"))
            result.extend((literal, literal + "/**"))
    return tuple(dict.fromkeys(result))


def metadata_boundary(root, patterns, *, max_paths=100000, git=None):
    """An outside-scope change witness; it never substitutes for content hashes."""
    from homebrew_harness.execution.execution_control import check

    root = canonical_root(root)
    hasher, seen = hashlib.sha256(), 0
    for directory, dirs, files in os.walk(
        root,
        followlinks=False,
        onerror=lambda error: (_ for _ in ()).throw(
            TestRunnerError("Cannot inspect the workspace boundary.")
        ),
    ):
        check()
        parent = Path(directory)
        relative = parent.relative_to(root)
        kept = []
        for name in sorted(dirs):
            path = parent / name
            if name in _EXCLUDED or sensitive_path((*relative.parts, name)):
                continue
            if path.is_symlink():
                raise TestRunnerError(
                    "Workspace source directories must not be symlinks."
                )
            kept.append(name)
        dirs[:] = kept
        for name in sorted(files):
            seen += 1
            if seen > max_paths:
                raise TestRunnerError(
                    "Workspace boundary exceeds max_paths=" + str(max_paths)
                )
            path = (relative / name).as_posix()
            if (
                name in _EXCLUDED
                or sensitive_path(Path(path).parts)
                or matches(path, patterns)
            ):
                continue
            info = os.stat(parent / name, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise TestRunnerError(
                    "Unwatched workspace files must be regular files."
                )
            value = (
                path,
                info.st_dev,
                info.st_ino,
                info.st_mode,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )
            hasher.update(
                json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
            )
            hasher.update(b"\n")
    identity = {}
    if git is not None:
        cap = min(64 * 1024 * 1024, max(65536, max_paths * 1024))
        head = git._git(["rev-parse", "HEAD"], cwd=root).stdout.strip()
        index = git._git(
            ["ls-files", "--stage", "-z"], cwd=root, output_bytes=cap
        ).stdout
        flags = git._git(["ls-files", "-v", "-z"], cwd=root, output_bytes=cap).stdout
        if any(
            row[:1].islower() or row.startswith("S ")
            for row in flags.split("\0")
            if row
        ):
            raise TestRunnerError(
                "Managed snapshots refuse assume-unchanged and skip-worktree index flags."
            )
        identity = {"head": head, "index": hashlib.sha256(index.encode()).hexdigest()}
    root_info = os.stat(root, follow_symlinks=False)
    return {
        "root": [root_info.st_dev, root_info.st_ino],
        "outside": hasher.hexdigest(),
        "git": identity,
    }
