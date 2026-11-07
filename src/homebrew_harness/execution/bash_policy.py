"""Host-owned Bash target and bounded, credential-free workspace selection."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from pathlib import Path

from homebrew_harness.execution.execution_control import check
from homebrew_harness.security.data_boundary import redact, sensitive_path
from homebrew_harness.security.safe_files import SafeFile, canonical_root


class BashPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class BashPolicy:
    mode: str = "host"
    cli: str = ""
    image: str = ""
    bash_path: str = "/bin/bash"
    uid: int | None = None
    cpus: float = 1
    memory_mb: int = 512
    pids: int = 64
    timeout: float = 120
    output_bytes: int = 8 * 1024 * 1024
    workspace_bytes: int = 128 * 1024 * 1024
    max_files: int = 5000
    max_input_bytes: int = 64 * 1024 * 1024
    cpu_seconds: int = 120
    file_bytes: int = 64 * 1024 * 1024
    open_files: int = 256
    outputs: tuple = ()
    export_python: str = "/usr/local/bin/python"

    def __post_init__(self):
        if not isinstance(self.mode, str) or self.mode not in {"host", "container"}:
            raise BashPolicyError("Bash mode must be host or container.")
        from homebrew_harness.execution.artifact_policy import output_profiles

        object.__setattr__(self, "outputs", output_profiles(self.outputs))
        if self.outputs and self.mode != "container":
            raise BashPolicyError("Output collection requires container Bash.")
        if self.outputs and (
            not isinstance(self.export_python, str)
            or not self.export_python.startswith("/")
            or "\0" in self.export_python
        ):
            raise BashPolicyError(
                "Artifact collection needs an absolute image Python path."
            )
        if self.mode == "host":
            if self.cli or self.image:
                raise BashPolicyError("Host mode accepts no container configuration.")
            return
        import re

        if (
            not isinstance(self.cli, str)
            or not Path(self.cli).is_absolute()
            or not isinstance(self.image, str)
            or not re.fullmatch(
                r"(?:[A-Za-z0-9._/-]+@)?sha256:[0-9a-f]{64}", self.image
            )
        ):
            raise BashPolicyError(
                "Container Bash needs an absolute Docker CLI and immutable image digest."
            )
        if (
            not isinstance(self.bash_path, str)
            or not self.bash_path.startswith("/")
            or "\0" in self.bash_path
        ):
            raise BashPolicyError(
                "Container Bash executable must be an absolute image path."
            )
        uid = self.uid if self.uid is not None else os.getuid() or 65534
        if type(uid) is not int or not 1 <= uid <= 65534:
            raise BashPolicyError(
                "Container Bash requires a non-root UID within supported bounds."
            )
        object.__setattr__(self, "uid", uid)
        for name, lower, upper in (
            ("memory_mb", 64, 4096),
            ("pids", 16, 256),
            ("output_bytes", 1024, 8 * 1024 * 1024),
            ("workspace_bytes", 1024 * 1024, 512 * 1024 * 1024),
            ("max_files", 1, 20000),
            ("max_input_bytes", 1024, 256 * 1024 * 1024),
            ("cpu_seconds", 1, 600),
            ("file_bytes", 1024, 256 * 1024 * 1024),
            ("open_files", 32, 1024),
        ):
            value = getattr(self, name)
            if type(value) is not int or not lower <= value <= upper:
                raise BashPolicyError("Invalid container Bash limit: " + name)
        for name, lower, upper in (("cpus", 0.1, 8), ("timeout", 0.01, 600)):
            value = getattr(self, name)
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not lower <= value <= upper
            ):
                raise BashPolicyError("Invalid container Bash limit: " + name)

    def fingerprint(self):
        values = (
            {"format": 1, "mode": "host"}
            if self.mode == "host"
            else {"format": 1, **asdict(self)}
        )
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()

    def identity(self):
        return {"mode": self.mode, "fingerprint": self.fingerprint()}

    def allows_child(self, child):
        if self.mode == "host":
            return True
        fixed = ("mode", "cli", "image", "bash_path", "uid", "export_python")
        limits = (
            "cpus",
            "memory_mb",
            "pids",
            "timeout",
            "output_bytes",
            "workspace_bytes",
            "max_files",
            "max_input_bytes",
            "cpu_seconds",
            "file_bytes",
            "open_files",
        )
        permitted = {p.name: p for p in self.outputs}
        output_access = all(
            p.name in permitted
            and set(p.files).issubset(set(permitted[p.name].files))
            and p.max_bytes <= permitted[p.name].max_bytes
            for p in child.outputs
        )
        return (
            output_access
            and all(getattr(child, k) == getattr(self, k) for k in fixed)
            and all(getattr(child, k) <= getattr(self, k) for k in limits)
        )


_ACTIVE = ContextVar("bash_execution_policy", default=None)
HOST_POLICY = BashPolicy()


def current_bash_policy():
    return _ACTIVE.get() or HOST_POLICY


@contextmanager
def bash_policy_scope(policy):
    if not isinstance(policy, BashPolicy):
        raise BashPolicyError("Bash policy must be supplied by the host.")
    parent = _ACTIVE.get()
    if parent is not None and not parent.allows_child(policy):
        raise BashPolicyError(
            "A delegated loop cannot weaken or replace its inherited Bash sandbox."
        )
    token = _ACTIVE.set(policy)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def load_bash_policy(path, workdir, *, mode=None, missing_ok=False):
    path = Path(path).expanduser().absolute()
    if path.resolve().is_relative_to(Path(workdir).resolve()):
        raise BashPolicyError(
            "Bash execution configuration must be outside the model workspace."
        )
    if not path.exists() and missing_ok:
        if mode == "container":
            raise BashPolicyError(
                "Container Bash requires an explicit host configuration; no fallback is allowed."
            )
        return HOST_POLICY
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 16384:
                raise BashPolicyError(
                    "Bash configuration must be a bounded regular JSON file."
                )
            values = json.load(stream)
    except (OSError, json.JSONDecodeError) as error:
        raise BashPolicyError(
            "Bash configuration could not be read as a regular policy JSON ("
            + type(error).__name__
            + ")."
        ) from None
    if (
        not isinstance(values, dict)
        or set(values) - set(BashPolicy.__dataclass_fields__)
        or "mode" not in values
    ):
        raise BashPolicyError(
            "Bash configuration requires an explicit mode and recognized policy fields."
        )
    if values["mode"] == "host" and set(values) != {"mode"}:
        raise BashPolicyError("Host Bash configuration accepts only the explicit mode.")
    if mode is not None and mode != values["mode"]:
        if mode == "host":
            return HOST_POLICY
        raise BashPolicyError(
            "Container mode needs a matching container configuration."
        )
    return BashPolicy(**values)


_EXCLUDED = {
    ".git",
    ".harness",
    ".memory",
    ".tasks",
    ".mailboxes",
    ".worktrees",
    ".secrets",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".docker",
    ".config",
    ".cache",
    ".netrc",
    ".pypirc",
    ".npmrc",
    ".kube",
    "node_modules",
}


def bash_snapshot(root, policy, deadline=None, cancel_events=()):
    root = canonical_root(Path(root))
    records = {}
    size = 0
    omitted = 0

    def excluded(parts):
        return sensitive_path(parts) or any(
            part.lower() in _EXCLUDED
            or part.lower().startswith(".env")
            or part.endswith(".egg-info")
            for part in parts
        )

    for parent, dirs, files in os.walk(root, followlinks=False):
        check(deadline, cancel_events)
        relative = Path(parent).relative_to(root)
        keep = []
        for name in sorted(dirs):
            path = Path(parent) / name
            if excluded((*relative.parts, name)) or path.is_symlink():
                omitted += 1
            else:
                keep.append(name)
        dirs[:] = keep
        for name in sorted(files):
            check(deadline, cancel_events)
            path = Path(parent) / name
            parts = (*relative.parts, name)
            info = path.lstat()
            if excluded(parts) or not stat.S_ISREG(info.st_mode):
                omitted += 1
                continue
            with SafeFile(root, str(relative / name)) as target:
                raw, version, mode = target.snapshot(
                    max_bytes=min(policy.max_input_bytes, 32_000_000)
                )
            if raw is None:
                raise BashPolicyError(
                    "Workspace changed while preparing the Bash sandbox."
                )
            decoded = raw.decode("utf-8", "replace")
            if (
                redact(decoded) != decoded
                or "PRIVATE KEY-----" in decoded
                or path.suffix.lower() == ".pem"
            ):
                omitted += 1
                continue
            size += len(raw)
            if len(records) >= policy.max_files or size > policy.max_input_bytes:
                raise BashPolicyError(
                    "Bash workspace snapshot exceeds host limits; no command was started."
                )
            records[(relative / name).as_posix()] = {
                "sha256": hashlib.sha256(raw).hexdigest(),
                "version": list(version[:-1]),
                "size": len(raw),
                "executable": bool(mode & 0o111),
            }
    return records, {
        "included_files": len(records),
        "omitted_entries": omitted,
        "input_bytes": size,
    }
