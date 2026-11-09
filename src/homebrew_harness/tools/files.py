"""files for the harness."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

from homebrew_harness.execution.execution_control import check as execution_check
from homebrew_harness.execution.execution_leases import file_update_lease
from homebrew_harness.security.file_visibility import assert_visible, private_parts
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
    if private_parts(Path(path).parts):
        raise ValueError("Private runtime files are not model file resources.")


def run_read(
    path: str, workdir: Path, limit: int | None = None, offset: int = 0
) -> str:
    assert_visible(workdir, path)
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
        target.verify_binding()
        relative = "/".join(target.parts)
        assert_visible(target.root, relative)
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
    from .file_iteration import FileWalk

    walk = FileWalk(workdir, pattern)
    paths = []
    for path in walk:
        try:
            with SafeFile(walk.root, path) as target, target.open_read():
                assert_visible(walk.root, path)
                paths.append(path)
        except (OSError, ValueError):
            walk.skipped += 1
            walk.truncated, walk.reason = True, "unreadable_file"
        if len(paths) > GLOB_RESULT_LIMIT:
            break
    truncated = len(paths) > GLOB_RESULT_LIMIT or walk.truncated
    return json.dumps(
        {
            "paths": sorted(paths[:GLOB_RESULT_LIMIT]),
            "truncated": truncated,
            "stop_reason": "result_limit"
            if len(paths) > GLOB_RESULT_LIMIT
            else walk.reason,
        },
        ensure_ascii=False,
    )
