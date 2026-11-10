"""Host-selected operations policies; no executable or connection supplied by models."""

import json
import re
from pathlib import Path

from homebrew_harness.services.maintenance_spec import number


def validate(raw):
    defaults = {
        "mode": "isolated",
        "operation_seconds": 120,
        "health_seconds": 1,
        "stale_seconds": 30,
        "min_free_bytes": 16_000_000,
        "backup_directory": None,
        "max_backup_bytes": 512_000_000,
        "max_backup_files": 50000,
        "retention_days": 15,
        "keep_recent": 10,
        "candidate_retention_days": 30,
        "audit_globs": ["**/*"],
        "audit_model": None,
        "git_executable": None,
        "upgrade_candidates": {},
        "diagnose_incidents": False,
    }
    if not isinstance(raw, dict) or set(raw) - set(defaults):
        raise ValueError("Invalid service operations policy.")
    value = {**defaults, **raw}
    if (
        value["mode"] not in {"isolated", "local"}
        or type(value["diagnose_incidents"]) is not bool
    ):
        raise ValueError("Invalid operations mode or diagnosis policy.")
    for name, lo, hi in (
        ("operation_seconds", 1, 3600),
        ("health_seconds", 0.1, 60),
        ("stale_seconds", 1, 86400),
        ("min_free_bytes", 0, 10**12),
        ("max_backup_bytes", 1, 2_000_000_000),
        ("max_backup_files", 1, 100000),
        ("retention_days", 1, 3650),
        ("keep_recent", 1, 1000),
        ("candidate_retention_days", 1, 3650),
    ):
        number(
            value[name],
            name,
            lo,
            hi,
            integer=name
            not in {"operation_seconds", "health_seconds", "stale_seconds"},
        )
    for name in ("backup_directory", "git_executable"):
        if value[name] is not None and (
            not isinstance(value[name], str) or not Path(value[name]).is_absolute()
        ):
            raise ValueError("Operations paths must be absolute host declarations.")
    patterns = value["audit_globs"]
    if (
        not isinstance(patterns, list)
        or not 1 <= len(patterns) <= 32
        or any(
            not isinstance(p, str)
            or not p
            or Path(p).is_absolute()
            or ".." in Path(p).parts
            for p in patterns
        )
    ):
        raise ValueError("Invalid fixed audit patterns.")
    aliases = value["upgrade_candidates"]
    if not isinstance(aliases, dict) or len(aliases) > 16:
        raise ValueError("Upgrade candidates must be a bounded host catalog.")
    for alias, candidate in aliases.items():
        if (
            not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", alias)
            or not isinstance(candidate, dict)
            or set(candidate) != {"workspace", "package", "recipe", "version"}
        ):
            raise ValueError("Invalid upgrade candidate declaration.")
        if any(
            not isinstance(candidate[k], str) or not Path(candidate[k]).is_absolute()
            for k in ("workspace", "package")
        ) or any(
            not isinstance(candidate[k], str) or not candidate[k]
            for k in ("recipe", "version")
        ):
            raise ValueError(
                "Upgrade candidates need fixed external package and verification paths."
            )
    return json.loads(json.dumps(value, allow_nan=False))


def validate_location(spec, roots):
    for path in [
        spec["backup_directory"],
        *(
            p
            for c in spec["upgrade_candidates"].values()
            for p in (c["workspace"], c["package"])
        ),
    ]:
        if path is None:
            continue
        root = Path(path)
        if root.is_symlink() or any(
            root.resolve().is_relative_to(Path(r).resolve()) for r in roots
        ):
            raise ValueError(
                "Operations inputs and backups must be outside service workspaces."
            )
