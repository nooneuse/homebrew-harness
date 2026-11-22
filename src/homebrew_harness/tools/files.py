"""files for the harness."""

from __future__ import annotations

from homebrew_harness.runtime.limits import current_limits
import json
from contextlib import contextmanager
from pathlib import Path

from homebrew_harness.runtime.errors import boundary_error
from homebrew_harness.execution.execution_control import check as execution_check
from homebrew_harness.execution.execution_leases import file_update_lease
from homebrew_harness.security.file_visibility import assert_visible, assert_model_write
from homebrew_harness.security.safe_files import (
    FilePublicationError,
    FileRemovalError,
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


def run_read(
    path: str, workdir: Path, limit: int | None = None, offset: int = 0,
    expected_version: str | None = None, raw: bool = False,
) -> str:
    from homebrew_harness.runtime.limits import current_limits
    output_limit = current_limits().file_read_chars
    assert_visible(workdir, path)
    if type(raw) is not bool:
        raise ValueError("raw must be Boolean.")
    if not raw:
        from .saved_output import read_view
        view = read_view(workdir, path, offset, limit, expected_version)
        if view is not None:
            return read_response(view)
    with SafeFile(workdir, path) as target, target.open_read() as stream:
        import io

        version = target.read_version(stream)
        relative = "/".join(target.parts)
        if expected_version is not None and expected_version != version:
            raise boundary_error(ValueError, "file_read_version_changed",
                "File changed since the previous read; discard old page offsets and read the current version.",
                file_path=relative, expected_version=expected_version, observed_version=version, restart_offset=0)
        reader = io.TextIOWrapper(stream, encoding="utf-8", newline="")
        remaining = offset
        while remaining:
            execution_check()
            skipped = reader.read(min(remaining, 8192))
            if not skipped:
                break
            remaining -= len(skipped)
        execution_check()
        content = reader.read(output_limit + 1)
        reader.detach()
        observed = target.read_version(stream)
        if observed != version:
            raise boundary_error(ValueError, "file_read_changed",
                "File changed during reading; discard this page and read the current file.",
                file_path=relative, expected_version=version, observed_version=observed, restart_offset=0)
        assert_visible(target.root, relative)
    selected = (
        content if limit is None else "".join(content.splitlines(keepends=True)[:limit])
    )
    shown = selected[:output_limit]
    truncated = len(selected) < len(content) or len(selected) > output_limit
    return read_response(
        {
            "path": relative,
            "version": version,
            "offset": offset,
            "content": shown,
            "truncated": truncated,
            "next_offset": offset + len(shown) if truncated else None,
        },
    )


def read_response(value):
    """Bound the complete JSON response, including escaping and metadata."""
    output_limit = current_limits().file_read_chars
    content = value["content"][:output_limit]
    original_size = len(value["content"])
    source = value.get("source")

    def encode(size):
        truncated = value["truncated"] or size < original_size
        result = {**value, "content": content[:size], "truncated": truncated,
                  "next_offset": value["offset"] + size if truncated else None}
        if source is not None:
            start = source["offset"] + value["offset"]
            remaining = truncated or source.get("truncated", False)
            result["source"] = {**source, "offset": start, "truncated": remaining,
                                "next_offset": start + size if remaining else None}
        return json.dumps(result, ensure_ascii=False)

    full = encode(len(content))
    if len(full) <= output_limit:
        return full
    if len(encode(0)) > output_limit:
        raise boundary_error(ValueError, "file_read_response_limit", "File-read metadata exceeds the response budget.",
            limit_name="file_read_response_characters", limit_value=output_limit, observed_value=len(encode(0)), unit="characters", file_path=value["path"])
    low, high = 0, len(content)
    while low < high:
        middle = (low + high + 1) // 2
        if len(encode(middle)) <= output_limit:
            low = middle
        else:
            high = middle - 1
    if not low and content:
        raise boundary_error(ValueError, "file_read_response_limit", "File-read metadata leaves no room for a content page.",
            limit_name="file_read_response_characters", limit_value=output_limit, observed_value=len(encode(0)), unit="characters", file_path=value["path"])
    return encode(low)


@contextmanager
def file_publication_outcome():
    try:
        yield
    except (FilePublicationError, FileRemovalError) as error:
        from homebrew_harness.tools.tool_outcome import ToolExecutionError

        raise ToolExecutionError(
            str(error), outcome=error.outcome, details=error.details
        ) from error


def run_write(path: str, content: str, workdir: Path) -> str:
    assert_model_write(workdir, path)
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
    assert_model_write(workdir, path)
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


def run_delete(path: str, workdir: Path, expected_version: str | None = None) -> str:
    assert_model_write(workdir, path)
    with file_publication_outcome(), file_update_lease(workdir, path), SafeFile(workdir, path) as target:
        relative = "/".join(target.parts)
        try:
            with target.open_read() as stream:
                observed = target.read_version(stream)
        except FileNotFoundError:
            target.verify_binding()
            return json.dumps({"path": relative, "deleted": False, "already_absent": True}, ensure_ascii=False)
        if expected_version is not None and observed != expected_version:
            raise boundary_error(ValueError, "file_delete_version_changed",
                "File changed since the previous read; inspect its current version before deleting.",
                file_path=relative, expected_version=expected_version, observed_version=observed)
        assert_model_write(target.root, relative)
        target.unlink(expected_read_version=observed)
        return json.dumps({"path": relative, "deleted": True, "previous_version": observed}, ensure_ascii=False)


def run_glob(pattern: str, workdir: Path) -> str:
    from .file_iteration import FileWalk
    result_limit = current_limits().glob_results

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
        if len(paths) > result_limit:
            break
    truncated = len(paths) > result_limit or walk.truncated
    return json.dumps(
        {
            "paths": sorted(paths[:result_limit]),
            "truncated": truncated,
            "stop_reason": "result_limit"
            if len(paths) > result_limit
            else walk.reason,
        },
        ensure_ascii=False,
    )
