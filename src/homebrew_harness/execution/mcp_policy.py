"""Frozen host-only execution policy for local MCP processes."""

import hashlib
import json
import math
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class MCPExecutionPolicy:
    mode: str = "host"
    cli: str = ""
    image: str = ""
    inputs: str = "none"
    uid: int | None = None
    cpus: float = 1
    memory_mb: int = 512
    pids: int = 64
    cpu_seconds: int = 3600
    file_bytes: int = 64 * 1024 * 1024
    open_files: int = 256
    max_files: int = 5000
    max_input_bytes: int = 64 * 1024 * 1024

    def __post_init__(self):
        if (
            not isinstance(self.mode, str)
            or not isinstance(self.inputs, str)
            or self.mode not in {"host", "container"}
            or self.inputs
            not in {
                "none",
                "workspace",
            }
        ):
            raise ValueError(
                "MCP execution needs host/container and none/workspace inputs."
            )
        if self.mode == "host":
            if self.cli or self.image or self.inputs != "none" or self.uid is not None:
                raise ValueError("Host MCP execution accepts no container settings.")
            return
        if (
            not isinstance(self.cli, str)
            or not Path(self.cli).is_absolute()
            or "\0" in self.cli
            or not isinstance(self.image, str)
            or not re.fullmatch(
                r"(?:[A-Za-z0-9._/-]+@)?sha256:[0-9a-f]{64}", self.image
            )
        ):
            raise ValueError(
                "Container MCP needs an absolute Docker CLI and immutable image digest."
            )
        uid = self.uid if self.uid is not None else os.getuid() or 65534
        if type(uid) is not int or not 1 <= uid <= 65534:
            raise ValueError("Container MCP requires a supported non-root UID.")
        object.__setattr__(self, "uid", uid)
        if (
            type(self.cpus) not in (int, float)
            or not math.isfinite(self.cpus)
            or not 0.1 <= self.cpus <= 8
        ):
            raise ValueError("Invalid MCP CPU quota.")
        for name, low, high in [
            ("memory_mb", 64, 4096),
            ("pids", 16, 256),
            ("cpu_seconds", 1, 86400),
            ("file_bytes", 1024, 256 * 1024 * 1024),
            ("open_files", 32, 1024),
            ("max_files", 1, 20000),
            ("max_input_bytes", 1024, 256 * 1024 * 1024),
        ]:
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError("Invalid container MCP limit: " + name)

    def fingerprint(self):
        return hashlib.sha256(
            json.dumps(asdict(self), sort_keys=True).encode()
        ).hexdigest()


def execution_policy(value):
    if isinstance(value, MCPExecutionPolicy):
        return value
    if (
        not isinstance(value, dict)
        or "mode" not in value
        or set(value) - set(MCPExecutionPolicy.__dataclass_fields__)
    ):
        raise ValueError(
            "MCP execution requires recognized host policy fields and an explicit mode."
        )
    if value["mode"] == "host" and set(value) != {"mode"}:
        raise ValueError("Host MCP configuration accepts only mode.")
    return MCPExecutionPolicy(**value)
