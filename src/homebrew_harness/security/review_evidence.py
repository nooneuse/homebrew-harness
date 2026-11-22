"""Bounded evidence observations; the reviewer decides relevance and sufficiency."""

from homebrew_harness.runtime.errors import boundary_error

from pathlib import Path
import errno

from .file_visibility import assert_visible, host_managed_parts, model_parts
from .safe_files import SafeFile, canonical_root

PAGE_CHARACTERS = 10_000
FILE_BYTES = 8 * 1024 * 1024
TOTAL_BYTES = 16 * 1024 * 1024
MAX_FILES = 16
MAX_PAGES = 64
NAVIGATION_PATHS = 10_000
NAVIGATION_BYTES = 8 * 1024 * 1024
NAVIGATION_FILE_BYTES = 2 * 1024 * 1024


class EvidenceLimitReached(RuntimeError):
    """A host resource boundary, rather than a model query error."""


def digest(version):
    return version[-1].hex() if version and isinstance(version[-1], bytes) else None


class ReviewEvidence:
    def __init__(self, root):
        from homebrew_harness.runtime.limits import current_limits
        self.limits = current_limits()
        self.root = canonical_root(Path(root))
        self.files = {}
        self.pages = 0
        self.bytes = 0
        self.navigation_bytes = 0
        self.hints = {}

    def _snapshot(self, path):
        try:
            with SafeFile(self.root, path) as file:
                try:
                    raw, version, _ = file.snapshot(max_bytes=self.limits.review_file_bytes)
                except ValueError as error:
                    if str(error) == "File snapshot exceeds the host size limit.":
                        raise boundary_error(EvidenceLimitReached, "review_file_bytes_limit", str(error), limit_name="review_file_bytes", limit_value=self.limits.review_file_bytes, observed_value=getattr(error, "details", {}).get("observed_value"), unit="bytes") from error
                    raise
                file.verify_binding()
            return raw, version
        except FileNotFoundError:
            return None, None

    def _remember(self, path, raw, version):
        previous = self.files.get(path)
        size = len(raw) if raw is not None else 0
        prior_size = previous["bytes"] if previous else 0
        delta = max(0, size - prior_size)
        if previous is None and self.limits.review_files is not None and len(self.files) >= self.limits.review_files:
            raise boundary_error(EvidenceLimitReached, "review_file_count_limit", "Review evidence file limit reached (file count).", limit_name="review_files", limit_value=self.limits.review_files, observed_value=len(self.files)+1, unit="files")
        if self.bytes + delta > self.limits.review_total_bytes:
            raise boundary_error(EvidenceLimitReached, "review_total_bytes_limit", "Review evidence total content limit reached.", limit_name="review_total_bytes", limit_value=self.limits.review_total_bytes, observed_value=self.bytes+delta, unit="bytes")
        changed = previous is not None and previous["version"] != version
        if previous is None or changed:
            previous = self.files[path] = {
                "version": version, "length": None, "ranges": [],
                "bytes": max(size, prior_size), "last_read_sha256": None,
            }
        self.bytes += delta
        return previous, changed

    @staticmethod
    def error(code, message, path=None):
        return {"ok": False, "error": {"code": code, "message": message, "path": path}}

    def read(self, arguments):
        if self.limits.review_pages is not None and self.pages >= self.limits.review_pages:
            raise boundary_error(EvidenceLimitReached, "review_page_limit", "Review evidence page limit reached.", limit_name="review_pages", limit_value=self.limits.review_pages, observed_value=self.pages+1, unit="pages")
        self.pages += 1
        path = None
        try:
            if (
                not isinstance(arguments, dict)
                or set(arguments) - {"path", "offset", "limit"}
                or not isinstance(arguments.get("path"), str)
                or not 1 <= len(arguments["path"]) <= 4096
            ):
                return self.error("invalid_arguments", "Use path and optional character offset/limit.")
            path = arguments["path"]
            offset, limit = arguments.get("offset", 0), arguments.get("limit", self.limits.review_page_chars)
            if type(offset) is not int or not 0 <= offset <= self.limits.review_file_bytes or type(limit) is not int or not 1 <= limit <= self.limits.review_page_chars:
                return self.error("invalid_arguments", "Invalid character offset or page limit.", path)
            _, parts = model_parts(self.root, path)
            if host_managed_parts(parts) or ".worktrees" in parts:
                return self.error("forbidden", "Host-managed state is outside reviewer read scope.", path)
            assert_visible(self.root, path)
            path = "/".join(parts)
            raw, version = self._snapshot(path)
            assert_visible(self.root, path)
            reference, changed = self._remember(path, raw, version)
            if raw is None:
                result = self.error("not_found", "This path does not exist. Choose another path or evaluate this absence.", path)
                result["changed_since_last_read"] = changed
                return result
            text = raw.decode("utf-8")
            if offset > len(text):
                return self.error("invalid_arguments", "Offset exceeds the file length.", path)
            end = min(len(text), offset + limit)
            ranges = sorted(reference["ranges"] + [(offset, end)])
            merged = []
            for start, stop in ranges:
                if merged and start <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(stop, merged[-1][1]))
                else:
                    merged.append((start, stop))
            reference.update(length=len(text), ranges=merged, last_read_sha256=digest(version))
            return {
                "ok": True, "path": path, "content": text[offset:end], "offset": offset,
                "length": len(text), "truncated": end < len(text),
                "next_offset": end if end < len(text) else None,
                "read_complete": merged == [(0, len(text))],
                "sha256": digest(version), "changed_since_last_read": changed,
                **({"differs_from_navigation_hint": self.hints[path] != digest(version)}
                   if self.hints.get(path) is not None else {}),
            }
        except UnicodeError:
            return self.error("not_utf8", "This file cannot be read as UTF-8 text.", path)
        except PermissionError:
            return self.error("permission_denied", "The host could not read this path.", path)
        except (ValueError, OSError) as error:
            code = "read_error" if isinstance(error, OSError) else "forbidden"
            if str(error) == "File tools require a regular file.":
                code = "not_a_file"
            elif isinstance(error, OSError) and error.errno == errno.ELOOP:
                code = "forbidden"
            return self.error(code, str(error)[:1000], path)

    def catalog(self, entries):
        """Stored path hints are navigation, not current file or approval proof."""
        result = []
        for entry in entries[:self.limits.review_hint_paths] if isinstance(entries, list) else []:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                continue
            try:
                _, parts = model_parts(self.root, entry["path"])
                if host_managed_parts(parts) or ".worktrees" in parts:
                    continue
                assert_visible(self.root, entry["path"])
            except (OSError, ValueError):
                continue
            path = "/".join(parts)
            value = entry.get("observed_sha256")
            value = value if isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value) else None
            if path in self.hints:
                continue
            self.hints[path] = value
            result.append({"path": path, "observed_sha256": value})
        return result

    def navigate(self, arguments, *, search=False):
        from homebrew_harness.execution.execution_control import check
        from homebrew_harness.tools.file_iteration import FileWalk
        from .file_visibility import file_access_scope
        if self.limits.review_pages is not None and self.pages >= self.limits.review_pages:
            raise boundary_error(EvidenceLimitReached, "review_page_limit", "Review evidence page limit reached.", limit_name="review_pages", limit_value=self.limits.review_pages, observed_value=self.pages + 1, unit="pages")
        self.pages += 1
        allowed = {"pattern", "cursor", "limit"} | ({"text"} if search else set())
        if not isinstance(arguments, dict) or set(arguments) - allowed:
            return self.error("invalid_arguments", "Use a relative pattern, cursor and limit, plus literal text for search.")
        pattern, cursor, limit = arguments.get("pattern", "**/*"), arguments.get("cursor", 0), arguments.get("limit", min(50, self.limits.query_results))
        text = arguments.get("text")
        if (not isinstance(pattern, str) or not 1 <= len(pattern) <= self.limits.query_pattern_chars
                or type(cursor) is not int or not 0 <= cursor <= self.limits.review_navigation_paths
                or type(limit) is not int or not 1 <= limit <= self.limits.query_results
                or search and (not isinstance(text, str) or not 1 <= len(text) <= self.limits.query_pattern_chars)):
            return self.error("invalid_arguments", "Navigation needs pattern/text within query_pattern_chars, a cursor within review_navigation_paths and a positive limit within query_results.")
        try:
            if host_managed_parts(Path(pattern).parts) or ".worktrees" in Path(pattern).parts:
                raise ValueError("Host-managed state is outside reviewer scope.")
            walk = FileWalk(self.root, pattern, max_paths=self.limits.review_navigation_paths)
        except (OSError, ValueError) as error:
            return self.error("forbidden", str(error)[:500])
        items, skipped, next_cursor, stop_reason = [], 0, None, None
        with file_access_scope(restrict_archives=True):
            for position, path in enumerate(walk):
                check()
                if position < cursor:
                    continue
                if host_managed_parts(Path(path).parts) or ".worktrees" in Path(path).parts:
                    continue
                if search and self.navigation_bytes >= self.limits.review_navigation_bytes:
                    stop_reason = "byte_limit"
                    break
                try:
                    assert_visible(self.root, path)
                    with SafeFile(self.root, path) as file:
                        if search:
                            raw, version, _ = file.snapshot(max_bytes=min(self.limits.review_navigation_file_bytes, self.limits.review_navigation_bytes - self.navigation_bytes))
                            if raw is None:
                                skipped += 1
                                continue
                            self.navigation_bytes += len(raw)
                        else:
                            with file.open_read():
                                pass
                        file.verify_binding()
                    assert_visible(self.root, path)
                except (OSError, ValueError) as error:
                    if (search and getattr(error, "code", None) == "file_snapshot_bytes_limit"
                            and self.limits.review_navigation_bytes - self.navigation_bytes < self.limits.review_navigation_file_bytes):
                        stop_reason = "byte_limit"
                        break
                    skipped += 1
                    continue
                if search:
                    try:
                        decoded = raw.decode("utf-8")
                    except UnicodeError:
                        skipped += 1
                        continue
                    if "\0" in decoded:
                        skipped += 1
                        continue
                    for line, content in enumerate(decoded.splitlines(), 1):
                        check()
                        column = content.find(text)
                        if column >= 0:
                            start = max(0, column - 100)
                            items.append({"path": path, "line": line, "column": column + 1,
                                          "text": content[start:start + 400], "sha256": digest(version)})
                            break
                else:
                    items.append(path)
                if len(items) >= limit:
                    next_cursor, stop_reason = position + 1, "result_limit"
                    break
        from .data_boundary import redact
        return redact({"ok": True, "matches" if search else "paths": items,
                       "next_cursor": next_cursor, "truncated": bool(stop_reason or walk.truncated or skipped or walk.skipped),
                       "stop_reason": stop_reason or walk.reason or ("unreadable_file" if skipped or walk.skipped else None),
                       "visited_paths": walk.visited, "skipped_files": skipped + walk.skipped,
                       "navigation_bytes": self.navigation_bytes, "navigation_byte_limit": self.limits.review_navigation_bytes,
                       "trust": "Navigation observations only. Read a relevant file before using it as approval evidence."})

    def observed(self):
        return [{"path": path, "last_read_sha256": value["last_read_sha256"],
                 "status": "present" if value["version"] is not None else "not_found",
                 "read_complete": value["length"] is not None and value["ranges"] == [(0, value["length"])]}
                for path, value in self.files.items()]

    def observation(self, round_number, tool, arguments, result):
        """A bounded query/result summary, without file contents or reasoning."""
        from .data_boundary import redact
        row = {"round": round_number, "tool": tool, "ok": result.get("ok", False)}
        if isinstance(arguments, dict):
            for key in ("pattern", "text"):
                value = arguments.get(key)
                if isinstance(value, str):
                    row[key] = value[:200] if not Path(value).is_absolute() and ".." not in Path(value).parts else "<outside_review_root>"
            for key in ("offset", "limit", "cursor"):
                if type(arguments.get(key)) is int:
                    row[key] = arguments[key]
        path = result.get("path") or result.get("error", {}).get("path")
        if isinstance(path, str):
            try:
                _, parts = model_parts(self.root, path)
                row["path"] = "/".join(parts)[:4096]
            except (OSError, ValueError):
                row["path"] = "<outside_review_root>"
        for key in ("offset", "length", "read_complete", "next_offset", "sha256", "truncated", "next_cursor", "stop_reason", "visited_paths", "navigation_bytes"):
            if key in result:
                row[key] = result[key]
        if not result.get("ok"):
            row["error_code"] = result.get("error", {}).get("code")
        if "content" in result:
            row["returned_characters"] = len(result["content"])
        if "paths" in result or "matches" in result:
            paths = result.get("paths", [item["path"] for item in result.get("matches", [])])
            row["result_count"] = len(paths)
            row["result_paths"] = [path[:256] for path in paths[:16]]
        return redact(row)

    def verify(self, selected=None):
        changes, evidence = [], []
        for path, reference in list(self.files.items()):
            if selected is not None and path not in selected:
                continue
            try:
                assert_visible(self.root, path)
                raw, version = self._snapshot(path)
                status = "present" if version is not None else "not_found"
            except (OSError, ValueError) as error:
                raw, version, status = None, (type(error).__name__, str(error)), "unreadable"
            if version != reference["version"]:
                changes.append({"path": path, "previous_sha256": digest(reference["version"]),
                                "current_sha256": digest(version), "current_status": status})
                observed = reference["last_read_sha256"]
                reference, _ = self._remember(path, raw, version)
                reference["last_read_sha256"] = observed
            evidence.append({"path": path, "sha256": digest(version), "status": status,
                             "last_read_sha256": reference["last_read_sha256"],
                             "read_complete": reference["length"] is not None and reference["ranges"] == [(0, reference["length"])]})
        return {"changes": changes, "evidence": evidence}
