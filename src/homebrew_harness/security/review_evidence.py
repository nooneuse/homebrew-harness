"""Bounded evidence observations; the reviewer decides relevance and sufficiency."""

from pathlib import Path
import errno

from .file_visibility import assert_visible, host_managed_parts, model_parts
from .safe_files import SafeFile, canonical_root

PAGE_CHARACTERS = 10_000
FILE_BYTES = 8 * 1024 * 1024
TOTAL_BYTES = 16 * 1024 * 1024
MAX_FILES = 16
MAX_PAGES = 64


class EvidenceLimitReached(RuntimeError):
    """A host resource boundary, rather than a model query error."""


def digest(version):
    return version[-1].hex() if version and isinstance(version[-1], bytes) else None


class ReviewEvidence:
    def __init__(self, root):
        self.root = canonical_root(Path(root))
        self.files = {}
        self.pages = 0
        self.bytes = 0

    def _snapshot(self, path):
        try:
            with SafeFile(self.root, path) as file:
                try:
                    raw, version, _ = file.snapshot(max_bytes=FILE_BYTES)
                except ValueError as error:
                    if str(error) == "File snapshot exceeds the host size limit.":
                        raise EvidenceLimitReached(str(error)) from error
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
        if (previous is None and len(self.files) >= MAX_FILES) or self.bytes + delta > TOTAL_BYTES:
            raise EvidenceLimitReached("Review evidence file limit reached.")
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
        if self.pages >= MAX_PAGES:
            raise EvidenceLimitReached("Review evidence page limit reached.")
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
            offset, limit = arguments.get("offset", 0), arguments.get("limit", PAGE_CHARACTERS)
            if type(offset) is not int or not 0 <= offset <= FILE_BYTES or type(limit) is not int or not 1 <= limit <= PAGE_CHARACTERS:
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
