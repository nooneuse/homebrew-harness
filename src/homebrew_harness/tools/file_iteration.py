"""A bounded iterator shared by glob display and content search."""

import fnmatch
import os
from functools import lru_cache
from pathlib import Path

from homebrew_harness.execution.execution_control import check
from homebrew_harness.execution.source_snapshot import _EXCLUDED
from homebrew_harness.security.file_visibility import (
    check_pattern,
    traversable,
    visible,
)
from homebrew_harness.security.safe_files import SafeFile, canonical_root


def pattern_matches(parts, pattern):
    @lru_cache(None)
    def match(i, j):
        if j == len(pattern):
            return i == len(parts)
        if pattern[j] == "**":
            return match(i, j + 1) or i < len(parts) and match(i + 1, j)
        return (
            i < len(parts)
            and fnmatch.fnmatchcase(parts[i], pattern[j])
            and match(i + 1, j + 1)
        )

    return match(0, 0)


class FileWalk:
    def __init__(self, root, pattern, *, max_paths=100000):
        check_pattern(pattern)
        self.root = canonical_root(root)
        self.pattern = Path(pattern).parts
        self.prefix = []
        for part in self.pattern:
            if any(char in part for char in "*?["):
                break
            self.prefix.append(part)
        self.max_paths = max_paths
        self.visited = self.skipped = 0
        self.truncated = False
        self.reason = None

    def _descend(self, path):
        parts = path.parts
        prefix = tuple(self.prefix)
        return parts[: len(prefix)] == prefix or prefix[: len(parts)] == parts

    def __iter__(self):
        stack = [Path(".")]
        while stack:
            check()
            directory = stack.pop()
            try:
                entries = []
                with SafeFile(self.root, str(directory / ".model-scan")) as parent:
                    with os.scandir(parent.fds[-1]) as scan:
                        for entry in scan:
                            check()
                            self.visited += 1
                            if self.visited > self.max_paths:
                                self.truncated, self.reason = True, "inventory_limit"
                                return
                            entries.append(
                                (
                                    entry.name,
                                    entry.is_symlink(),
                                    entry.is_dir(follow_symlinks=False),
                                    entry.is_file(follow_symlinks=False),
                                )
                            )
                    parent.verify_binding()
            except (OSError, ValueError):
                self.skipped += 1
                self.truncated, self.reason = True, "unreadable_path"
                continue
            directories = []
            for name, symlink, is_dir, is_file in sorted(entries):
                check()
                path = directory / name
                if symlink or not traversable(self.root, path):
                    continue
                if is_dir:
                    explicit = bool(self.prefix) and name in self.prefix
                    if self._descend(path) and (name not in _EXCLUDED or explicit):
                        directories.append(path)
                elif (
                    is_file
                    and pattern_matches(path.parts, self.pattern)
                    and visible(self.root, path)
                ):
                    yield path.as_posix()
            stack.extend(reversed(directories))
