"""files for the harness."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

from homebrew_harness.execution.execution_control import check as execution_check
from homebrew_harness.execution.execution_leases import file_update_lease
from homebrew_harness.security.data_boundary import sensitive_path
from homebrew_harness.security.safe_files import (
    FilePublicationError,
    SafeFile,
    canonical_root,
)

OUTPUT_CHAR_LIMIT = 10_000


GLOB_RESULT_LIMIT = 200


def safe_path(path: str, workdir: Path) -> Path:
    """Resolve '..' and symlinks before checking the file-tool boundary."""
    root = canonical_root(workdir)
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("File paths must stay inside the working directory.")
    return resolved


def model_file_path(path):
    parts = Path(path).parts
    if any(
        parts[i : i + 2] == (".harness", "protocol-state")
        for i in range(len(parts) - 1)
    ):
        raise ValueError(
            "Opaque provider replay state is private runtime data, not a file-tool resource."
        )


def run_read(
    path: str, workdir: Path, limit: int | None = None, offset: int = 0
) -> str:
    model_file_path(path)
    with SafeFile(workdir, path) as target, target.open_read() as stream:
        import io

        reader = io.TextIOWrapper(stream, encoding="utf-8", newline="")
        remaining = offset
        while remaining:
            execution_check()
            skipped = reader.read(min(remaining, 8192))
            if not skipped:
                break
            remaining -= len(skipped)
        execution_check()
        content = reader.read(OUTPUT_CHAR_LIMIT + 1)
        reader.detach()
        relative = "/".join(target.parts)
    selected = (
        content if limit is None else "".join(content.splitlines(keepends=True)[:limit])
    )
    shown = selected[:OUTPUT_CHAR_LIMIT]
    truncated = len(selected) < len(content) or len(selected) > OUTPUT_CHAR_LIMIT
    return json.dumps(
        {
            "path": relative,
            "content": shown,
            "truncated": truncated,
            "next_offset": offset + len(shown) if truncated else None,
        },
        ensure_ascii=False,
    )


@contextmanager
def file_publication_outcome():
    try:
        yield
    except FilePublicationError as error:
        from homebrew_harness.tools.tool_outcome import ToolExecutionError

        raise ToolExecutionError(
            str(error), outcome=error.outcome, details=error.details
        ) from error


def run_write(path: str, content: str, workdir: Path) -> str:
    model_file_path(path)
    with (
        file_publication_outcome(),
        file_update_lease(workdir, path),
        SafeFile(workdir, path, create_parents=True) as target,
    ):
        _, version, mode = target.snapshot()
        data = content.encode("utf-8")
        target.publish(data, version, mode)
        return json.dumps(
            {"path": "/".join(target.parts), "bytes_written": len(data)},
            ensure_ascii=False,
        )


def run_edit(path: str, old_text: str, new_text: str, workdir: Path) -> str:
    model_file_path(path)
    with (
        file_publication_outcome(),
        file_update_lease(workdir, path),
        SafeFile(workdir, path) as target,
    ):
        data, version, mode = target.snapshot()
        if data is None:
            raise ValueError("File not found; read an existing file before editing.")
        content = data.decode("utf-8")
        if not old_text or old_text not in content:
            raise ValueError("old_text was not found; the file was not changed.")
        target.publish(
            content.replace(old_text, new_text, 1).encode("utf-8"), version, mode
        )
        return json.dumps(
            {"path": "/".join(target.parts), "replacements": 1}, ensure_ascii=False
        )


def run_glob(pattern: str, workdir: Path) -> str:
    root = canonical_root(workdir)
    if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
        raise ValueError("glob requires a relative pattern without '..'.")
    safe_path(pattern, root)
    import fnmatch
    from functools import lru_cache

    pattern_parts = Path(pattern).parts

    def matches_pattern(relative):
        parts = relative.parts

        @lru_cache(None)
        def match(i, j):
            if j == len(pattern_parts):
                return i == len(parts)
            if pattern_parts[j] == "**":
                return match(i, j + 1) or i < len(parts) and match(i + 1, j)
            return (
                i < len(parts)
                and fnmatch.fnmatchcase(parts[i], pattern_parts[j])
                and match(i + 1, j + 1)
            )

        return match(0, 0)

    matches = set()
    visited = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        execution_check()
        parent = Path(directory)
        dirs[:] = [
            name
            for name in dirs
            if not (parent / name).is_symlink() and not sensitive_path((name,))
        ]
        for name in files:
            execution_check()
            visited += 1
            if visited > 100_000:
                raise ValueError(
                    "Directory traversal exceeds the host limit; use a narrower directory."
                )
            candidate = parent / name
            relative = candidate.relative_to(root)
            text = relative.as_posix()
            if not matches_pattern(relative):
                continue
            if candidate.is_symlink() or sensitive_path(relative.parts):
                continue
            with SafeFile(root, text) as target, target.open_read():
                matches.add(text)
            if len(matches) > GLOB_RESULT_LIMIT:
                break
        if len(matches) > GLOB_RESULT_LIMIT:
            break
    paths = sorted(matches)
    return json.dumps(
        {
            "paths": paths[:GLOB_RESULT_LIMIT],
            "truncated": len(paths) > GLOB_RESULT_LIMIT,
        },
        ensure_ascii=False,
    )
