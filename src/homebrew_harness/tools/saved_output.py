"""Readable views of owned file-shaped tool outputs; raw archives stay intact."""

import io
import json
import re
from pathlib import Path

from homebrew_harness.execution.execution_control import check
from homebrew_harness.runtime.errors import boundary_error
from homebrew_harness.security.file_visibility import current_archive_store, assert_visible
from homebrew_harness.security.safe_files import SafeFile

PARSE_CHARS = 64_000
MAX_LINKS = 8
VERSION = re.compile(r"[a-f0-9]{64}\Z")


def file_response(value):
    keys = {"path", "version", "offset", "content", "truncated", "next_offset"}
    return (isinstance(value, dict) and keys <= set(value) and set(value) <= keys | {"source", "view"}
            and isinstance(value["path"], str) and isinstance(value["version"], str)
            and VERSION.fullmatch(value["version"]) and type(value["offset"]) is int and value["offset"] >= 0
            and isinstance(value["content"], str) and type(value["truncated"]) is bool
            and (value["next_offset"] is None or type(value["next_offset"]) is int and value["next_offset"] >= value["offset"]))


def read_view(workdir, path, offset, limit, expected_version):
    store = current_archive_store(workdir)
    if store is None or not store.owns(path) or Path(path).parent.name != "tool-results":
        return None
    reference, linked_version, visited = path, expected_version, set()
    first_version = None

    def raw_link_view(reference, version):
        if len(visited) == 1:
            return None
        from .files import run_read
        result = json.loads(run_read(reference, workdir, limit=limit, offset=offset,
                                     expected_version=version, raw=True))
        source = {"path": reference, "version": version, "offset": 0,
                  "truncated": result["truncated"], "next_offset": result["next_offset"]}
        return {**result, "path": path, "version": first_version,
                "view": "recorded_tool_output", "source": source}
    for _ in range(MAX_LINKS):
        canonical = str((Path(workdir) / reference).resolve())
        if canonical in visited:
            raise ValueError("Saved output reference cycle; inspect the raw archive with raw=true.")
        visited.add(canonical)
        assert_visible(workdir, reference)
        with SafeFile(workdir, reference) as target, target.open_read() as stream:
            version = target.read_version(stream)
            if linked_version is not None and linked_version != version:
                raise boundary_error(ValueError, "file_read_version_changed", "Saved output changed since its recorded read.",
                    file_path=reference, expected_version=linked_version, observed_version=version, restart_offset=0)
            if first_version is None:
                first_version = version
            check()
            reader = io.TextIOWrapper(stream, encoding="utf-8", newline="")
            text = reader.read(PARSE_CHARS + 1)
            reader.detach()
            check()
            if target.read_version(stream) != version:
                raise boundary_error(ValueError, "file_read_changed", "Saved output changed during reading.",
                    file_path=reference, expected_version=version, observed_version=target.read_version(stream), restart_offset=0)
        if len(text) > PARSE_CHARS:
            return raw_link_view(reference, version)
        try:
            value = json.loads(text)
        except ValueError:
            return raw_link_view(reference, version)
        if not file_response(value):
            return raw_link_view(reference, version)
        is_view = value.get("view") in {"recorded_file_content", "recorded_tool_output"}
        if not is_view and store.owns(value["path"]) and Path(value["path"]).parent.name == "tool-results":
            reference, linked_version = value["path"], value["version"]
            continue
        content = value["content"][offset:]
        if limit is not None:
            content = "".join(content.splitlines(keepends=True)[:limit])
        source = value.get("source") if is_view else {
            key: value[key] for key in ("path", "version", "offset", "truncated", "next_offset")}
        if (not isinstance(source, dict) or set(source) != {"path", "version", "offset", "truncated", "next_offset"}
                or not isinstance(source.get("path"), str) or not isinstance(source.get("version"), str)
                or not VERSION.fullmatch(source["version"]) or type(source.get("offset")) is not int
                or source["offset"] < 0 or type(source.get("truncated")) is not bool):
            return None
        return {"path": path, "version": first_version, "offset": offset, "content": content,
                "truncated": offset + len(content) < len(value["content"]),
                "next_offset": None, "view": value.get("view") if is_view else "recorded_file_content", "source": dict(source)}
    raise boundary_error(ValueError, "saved_output_link_limit", "Saved output nesting reached its readable-view bound; use raw=true to inspect it.",
        limit_name="saved_output_links", limit_value=MAX_LINKS, observed_value=MAX_LINKS + 1, unit="links", file_path=path)
