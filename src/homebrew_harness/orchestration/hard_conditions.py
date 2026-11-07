"""Deterministic, host-selected completion predicates; no model verdicts."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from homebrew_harness.execution.test_runner import TestRunnerError
from homebrew_harness.security.data_boundary import redact
from homebrew_harness.security.safe_files import SafeFile, relative_parts


class ConditionError(ValueError):
    pass


def validate_spec(spec):
    if not isinstance(spec, dict) or set(spec) - {"condition", "predicates", "judge"}:
        raise ConditionError(
            "Structured goal must contain condition, predicates and optional judge."
        )
    condition, predicates = spec.get("condition"), spec.get("predicates")
    if not isinstance(condition, str) or not 1 <= len(condition.strip()) <= 4000:
        raise ConditionError(
            "Structured completion condition must be nonempty text under 4000 characters."
        )
    if (
        not isinstance(predicates, list)
        or not 1 <= len(predicates) <= 16
        or type(spec.get("judge", True)) is not bool
    ):
        raise ConditionError(
            "A structured goal needs 1-16 predicates and a boolean judge flag."
        )
    result, ids = [], set()
    for raw in predicates:
        if not isinstance(raw, dict):
            raise ConditionError("Goal predicate must be an object.")
        kind, predicate_id = raw.get("type"), raw.get("id")
        if (
            not isinstance(predicate_id, str)
            or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", predicate_id)
            or predicate_id in ids
        ):
            raise ConditionError("Predicate IDs must be valid and unique.")
        ids.add(predicate_id)
        workspace = raw.get("workspace", "base")
        if (
            not isinstance(workspace, str)
            or workspace != "base"
            and not re.fullmatch(r"task_[0-9a-f]{8}", workspace)
        ):
            raise ConditionError(
                "Predicate workspace must be base or an explicit task ID."
            )
        fields = {"id", "type", "workspace"}
        if kind in {"file_exists", "file_contains", "file_sha256"}:
            fields |= {"path", "min_bytes"}
            path = raw.get("path")
            if (
                not isinstance(path, str)
                or not path
                or Path(path).is_absolute()
                or ".." in Path(path).parts
            ):
                raise ConditionError(
                    "Predicate file path must be relative to its selected workspace."
                )
            try:
                relative_parts(path, Path.cwd())
            except ValueError as error:
                raise ConditionError(str(error)) from error
            minimum = raw.get("min_bytes", 0)
            if type(minimum) is not int or not 0 <= minimum <= 16_000_000:
                raise ConditionError("Invalid predicate minimum size.")
            if kind == "file_contains":
                fields.add("text")
                if (
                    not isinstance(raw.get("text"), str)
                    or not 1 <= len(raw["text"]) <= 4096
                ):
                    raise ConditionError(
                        "file_contains requires a nonempty exact text under 4096 characters."
                    )
            if kind == "file_sha256":
                fields.add("sha256")
                if not isinstance(raw.get("sha256"), str) or not re.fullmatch(
                    r"[0-9a-f]{64}", raw["sha256"]
                ):
                    raise ConditionError(
                        "file_sha256 requires a lowercase SHA-256 value."
                    )
        elif kind == "tests_passed":
            fields.add("recipe")
            if not isinstance(raw.get("recipe"), str) or not re.fullmatch(
                r"[a-z][a-z0-9_-]{0,63}", raw["recipe"]
            ):
                raise ConditionError("tests_passed requires a registered recipe name.")
        else:
            raise ConditionError("Unknown hard predicate type.")
        if set(raw) - fields:
            raise ConditionError("Unknown predicate fields are not accepted.")
        result.append({**raw, "workspace": workspace})
    normalized = {
        "condition": condition.strip(),
        "predicates": result,
        "judge": spec.get("judge", True),
    }
    if redact(normalized) != normalized:
        raise ConditionError(
            "Structured goal contains private credential-like material."
        )
    return json.loads(json.dumps(normalized))


class HardVerifier:
    def __init__(self, workdir, *, test_runner=None, workspace_resolver=None):
        self.workdir = Path(workdir).resolve()
        self.test_runner, self.workspace_resolver = test_runner, workspace_resolver

    def workspace(self, selector):
        if selector == "base":
            return self.workdir
        if self.workspace_resolver is None:
            raise ConditionError("Task workspace cannot be resolved by this host.")
        cwd = Path(self.workspace_resolver(selector)).resolve()
        if not cwd.is_relative_to(self.workdir):
            raise ConditionError("Task workspace escapes the bound root.")
        return cwd

    def check(self, spec, *, since=0):
        spec = validate_spec(spec)
        results = []
        for predicate in spec["predicates"]:
            result = {"id": predicate["id"], "type": predicate["type"], "ok": False}
            try:
                cwd = self.workspace(predicate["workspace"])
                if predicate["type"] == "tests_passed":
                    if self.test_runner is None:
                        raise ConditionError(
                            "A trusted TestRunner is required for tests_passed."
                        )
                    task_id = (
                        predicate["workspace"]
                        if predicate["workspace"] != "base"
                        else None
                    )
                    result.update(
                        self.test_runner.verify(
                            predicate["recipe"], cwd, since=since, task_id=task_id
                        )
                    )
                    if not result["ok"]:
                        result[
                            "requires_approval"
                        ] = not self.test_runner.authorization_available(
                            predicate["recipe"], cwd, task_id
                        )
                else:
                    with (
                        SafeFile(cwd, predicate["path"]) as target,
                        target.open_read() as stream,
                    ):
                        info = os.fstat(stream.fileno())
                        if info.st_size > 16_000_000:
                            raise ConditionError(
                                "Hard file check exceeds the host read budget."
                            )
                        content = stream.read(16_000_001)
                        after = os.fstat(stream.fileno())
                        target.verify_binding()
                        named = os.stat(
                            target.parts[-1],
                            dir_fd=target.fds[-1],
                            follow_symlinks=False,
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
                            raise ConditionError(
                                "File changed during hard verification."
                            )
                    sha = hashlib.sha256(content).hexdigest()
                    ok = len(content) >= predicate.get("min_bytes", 0)
                    if predicate["type"] == "file_contains":
                        ok = ok and predicate["text"] in content.decode("utf-8")
                    if predicate["type"] == "file_sha256":
                        ok = ok and sha == predicate["sha256"]
                    result.update(
                        ok=bool(ok),
                        path=predicate["path"],
                        workspace=predicate["workspace"],
                        sha256=sha,
                        bytes=len(content),
                        version=list(version),
                        reason="Current file satisfies the predicate."
                        if ok
                        else "Current file does not satisfy the predicate.",
                    )
            except (OSError, ValueError, UnicodeError) as error:
                result["reason"] = (
                    str(error)
                    if isinstance(error, (ConditionError, TestRunnerError))
                    else "Hard verification unavailable (" + type(error).__name__ + ")."
                )
            results.append(result)
        return {"ok": all(result["ok"] for result in results), "results": results}
