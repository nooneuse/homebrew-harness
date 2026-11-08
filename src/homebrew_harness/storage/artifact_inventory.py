"""Bounded metadata inventory and conservative reference roots for host GC."""

import hashlib
import json
import math
import os
import re
import stat
import time
from pathlib import Path

from homebrew_harness.security.safe_files import SafeFile, canonical_root
from homebrew_harness.storage.durable_runtime import DurableStore
from homebrew_harness.storage.retention_graph import (
    artifact_closure,
    references,
    state_roots,
)

_ARCHIVE = re.compile(
    r"\.harness/(?:[A-Za-z0-9_-]{1,80}/)?(tool-results|transcripts)/[0-9a-f]{32}\.(?:txt|jsonl)\Z"
)


def archive_path(path):
    return (
        isinstance(path, str)
        and ".." not in Path(path).parts
        and _ARCHIVE.fullmatch(path) is not None
    )


def register_artifact(root, path, data, kind):
    DurableStore(root).put(
        "artifacts",
        path,
        {
            "path": path,
            "kind": kind,
            "created_at": time.time(),
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "format": 1,
        },
    )


def version(value):
    return [*value[:-1], value[-1].hex()]


def inspect_artifacts(
    root, *, retention_days=30, now=None, max_files=10000, max_bytes=64 * 1024 * 1024
):
    if (
        not isinstance(retention_days, (int, float))
        or not math.isfinite(retention_days)
        or retention_days < 1
    ):
        raise ValueError("Artifact retention must be at least one day.")
    root = canonical_root(Path(root))
    # A read-only inventory must not create state/lock directories.
    state = DurableStore(root).backend().read()
    registry = state.get("artifacts", {})
    if not isinstance(registry, dict):
        raise ValueError("Invalid artifact registry; cleanup is unavailable.")
    roots = state_roots(state)
    files, total = [], 0
    clock = time.time() if now is None else now
    for base in (".harness",):
        directory = root / base
        if not directory.exists():
            continue
        if directory.is_symlink():
            raise ValueError("Runtime inventory refuses symlink directories.")
        for parent, dirs, names in os.walk(
            directory,
            followlinks=False,
            onerror=lambda error: (_ for _ in ()).throw(
                ValueError("Runtime inventory is incomplete.")
            ),
        ):
            from homebrew_harness.execution.execution_control import check

            check()
            for name in dirs:
                if (Path(parent) / name).is_symlink():
                    raise ValueError("Runtime inventory refuses internal symlinks.")
            with SafeFile(root, str(Path(parent) / ".inventory-anchor")) as anchor:
                for name in sorted(names):
                    info = os.stat(name, dir_fd=anchor.fds[-1], follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode):
                        raise ValueError("Runtime inventory requires regular files.")
                    path = (Path(parent) / name).relative_to(root).as_posix()
                    if len(files) >= max_files:
                        raise ValueError(
                            "Runtime inventory exceeds the file count limit."
                        )
                    record = {
                        "path": path,
                        "bytes": info.st_size,
                        "kind": "runtime_metadata",
                        "reason": "outside_first_gc_scope",
                    }
                    if archive_path(path):
                        total += info.st_size
                        with SafeFile(root, path) as target:
                            raw, identity, _ = target.snapshot(
                                max_bytes=max_bytes - total + info.st_size
                            )
                        record.update(
                            kind="archive",
                            version=version(identity),
                            sha256=hashlib.sha256(raw).hexdigest(),
                        )
                    elif (
                        "/goals/" in path
                        or "/workflows/" in path
                        or path == ".harness/artifact-pins.json"
                    ) and path.endswith(".json"):
                        with SafeFile(root, path) as target:
                            raw, _, _ = target.snapshot(max_bytes=max_bytes - total)
                        total += len(raw)
                        roots.update(references(json.loads(raw)))
                    if total > max_bytes:
                        raise ValueError(
                            "Runtime inventory exceeds the content scan limit."
                        )
                    files.append(record)
                anchor.verify_binding()
    roots = artifact_closure(state, roots)
    candidates = []
    for record in files:
        if record["kind"] != "archive":
            continue
        path = record["path"]
        registered = registry.get(path)
        if path in roots:
            record["reason"] = "referenced_by_retained_state"
        elif not isinstance(registered, dict) or registered.get("format") != 1:
            record["reason"] = "unregistered_archive"
        elif (
            registered.get("sha256") != record["sha256"]
            or registered.get("bytes") != record["bytes"]
        ):
            record["reason"] = "changed_since_registration"
        elif (
            not isinstance(registered.get("created_at"), (int, float))
            or clock - registered["created_at"] < retention_days * 86400
        ):
            record["reason"] = "retention_window"
        else:
            record["reason"] = "expired_unreferenced_registered_archive"
            candidates.append(
                {k: record[k] for k in ("path", "version", "bytes", "sha256")}
            )
    return {
        "format": 1,
        "read_only": True,
        "retention_days": retention_days,
        "files": files,
        "total_bytes": sum(item["bytes"] for item in files),
        "candidate_bytes": sum(item["bytes"] for item in candidates),
        "candidates": candidates,
        "reference_count": len(roots),
        "references": sorted(roots),
    }
