"""Versioned, bounded text pages used by the independent approval reviewer."""

from pathlib import Path

from .file_visibility import assert_visible, host_managed_parts, model_parts
from .safe_files import SafeFile

PAGE_CHARACTERS = 10_000
FILE_BYTES = 8 * 1024 * 1024
TOTAL_BYTES = 16 * 1024 * 1024
MAX_FILES = 16
MAX_PAGES = 64


class ReviewEvidence:
    def __init__(self, root):
        self.root = Path(root)
        self.files = {}
        self.pages = 0
        self.bytes = 0

    def read(self, arguments):
        if (
            not isinstance(arguments, dict)
            or set(arguments) - {"path", "offset", "limit"}
            or not isinstance(arguments.get("path"), str)
            or not 1 <= len(arguments["path"]) <= 4096
        ):
            raise ValueError("Invalid review evidence query.")
        offset, limit = arguments.get("offset", 0), arguments.get("limit", PAGE_CHARACTERS)
        if (
            type(offset) is not int
            or not 0 <= offset <= FILE_BYTES
            or type(limit) is not int
            or not 1 <= limit <= PAGE_CHARACTERS
        ):
            raise ValueError("Invalid review evidence page bounds.")
        if self.pages >= MAX_PAGES:
            raise ValueError("Review evidence page limit reached.")
        _, parts = model_parts(self.root, arguments["path"])
        if host_managed_parts(parts) or ".worktrees" in parts:
            raise ValueError("Approval evidence cannot read host-managed state.")
        assert_visible(self.root, arguments["path"])
        with SafeFile(self.root, arguments["path"]) as file:
            raw, version, _ = file.snapshot(max_bytes=FILE_BYTES)
            path = "/".join(file.parts)
            file.verify_binding()
        assert_visible(self.root, path)
        if version is None:
            raise ValueError("Required review evidence is unavailable.")
        previous = self.files.get(path)
        if previous and previous["version"] != version:
            raise ValueError("Review evidence changed between pages.")
        if previous is None and (
            len(self.files) >= MAX_FILES or self.bytes + len(raw) > TOTAL_BYTES
        ):
            raise ValueError("Review evidence file limit reached.")
        text = raw.decode("utf-8")
        if offset > len(text):
            raise ValueError("Review evidence offset exceeds the file length.")
        if previous is None:
            previous = self.files[path] = {"version": version, "length": len(text), "ranges": []}
            self.bytes += len(raw)
        end = min(len(text), offset + limit)
        ranges = sorted(previous["ranges"] + [(offset, end)])
        merged = []
        for start, stop in ranges:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(stop, merged[-1][1]))
            else:
                merged.append((start, stop))
        previous["ranges"] = merged
        self.pages += 1
        return {
            "path": path,
            "content": text[offset:end],
            "offset": offset,
            "length": len(text),
            "truncated": end < len(text),
            "next_offset": end if end < len(text) else None,
            "read_complete": merged == [(0, len(text))],
            "sha256": version[-1].hex(),
        }

    def verify(self):
        result = []
        for path, reference in self.files.items():
            assert_visible(self.root, path)
            with SafeFile(self.root, path) as file:
                if file.snapshot(max_bytes=FILE_BYTES)[1] != reference["version"]:
                    raise ValueError("Review evidence changed.")
                file.verify_binding()
            result.append({
                "path": path,
                "sha256": reference["version"][-1].hex(),
                "read_complete": reference["ranges"] == [(0, reference["length"])],
            })
        return result
