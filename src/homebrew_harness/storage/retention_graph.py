"""Shared retained-state roots; bounded host dependency snapshots for retirement."""

import json
import os
import re
from pathlib import Path

from homebrew_harness.security.safe_files import SafeFile, canonical_root

_REFERENCE = re.compile(r"\.harness/[A-Za-z0-9_./-]+")


def references(value):
    if isinstance(value, str):
        return {match.rstrip(".") for match in _REFERENCE.findall(value)}
    if isinstance(value, dict):
        return set().union(*(references(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(references(v) for v in value)) if value else set()
    return set()


def state_roots(state, external=()):
    roots = references(
        {
            k: v
            for k, v in state.items()
            if k not in {"artifacts", "session_meta", "artifact_bundles"}
        }
    )
    roots.update(external)
    for meta in state.get("session_meta", {}).values():
        if meta.get("lifecycle") == "retired":
            roots.update(references(meta.get("protected_refs", [])))
            roots.update(references(meta.get("cold_archive", {})))
        elif meta.get("lifecycle") != "purged":
            roots.update(references(meta))
        else:
            roots.update(references(meta.get("usage_summary", {}).get("ledger")))
    return artifact_closure(state, roots)


def artifact_closure(state, roots):
    """Follow bundle manifests only after all retained roots have been collected."""
    roots = set(roots)
    edges = {}
    for bundle in state.get("artifact_bundles", {}).values():
        targets = references(bundle)
        reference = bundle.get("reference")
        if reference:
            edges.setdefault(reference, set()).update(targets)
        if bundle.get("state") not in {"published", "discarded"}:
            roots.update(targets)
    pending = list(roots)
    visited = set()
    while pending:
        reference = pending.pop()
        if reference in visited:
            continue
        visited.add(reference)
        for target in edges.get(reference, ()):
            if target not in roots:
                roots.add(target)
                pending.append(target)
    return roots


def external_snapshot(root, *, max_files=10000, max_bytes=64 * 1024 * 1024):
    root = canonical_root(Path(root))
    records, versions, total, seen = {}, {}, 0, 0
    for base in (
        ".harness/goals",
        ".harness/workflows",
        ".harness/containers",
    ):
        directory = root / base
        if not directory.exists():
            continue
        if directory.is_symlink():
            raise ValueError("Lifecycle dependencies must not use symlink directories.")
        for parent, dirs, names in os.walk(
            directory,
            followlinks=False,
            onerror=lambda error: (_ for _ in ()).throw(
                ValueError("Dependency inventory is incomplete.")
            ),
        ):
            from homebrew_harness.execution.execution_control import check

            check()
            if any((Path(parent) / name).is_symlink() for name in dirs):
                raise ValueError("Lifecycle dependencies contain an internal symlink.")
            for name in sorted(names):
                seen += 1
                if seen > max_files:
                    raise ValueError(
                        "Lifecycle dependencies exceed the file count limit."
                    )
                if not name.endswith(".json"):
                    continue
                path = (Path(parent) / name).relative_to(root).as_posix()
                with SafeFile(root, path) as target:
                    raw, identity, _ = target.snapshot(max_bytes=max_bytes - total)
                if raw is None:
                    raise ValueError(
                        "Lifecycle dependency disappeared during inspection."
                    )
                total += len(raw)
                records[path] = json.loads(raw)
                versions[path] = [*identity[:-1], identity[-1].hex()]
    path = ".harness/artifact-pins.json"
    try:
        with SafeFile(root, path) as target:
            raw, identity, _ = target.snapshot(max_bytes=max_bytes - total)
        if raw is not None:
            records[path] = json.loads(raw)
            versions[path] = [*identity[:-1], identity[-1].hex()]
    except FileNotFoundError:
        pass
    return {"records": records, "versions": versions, "references": references(records)}
