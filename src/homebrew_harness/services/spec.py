"""Host declarations for one serial workspace and fixed service schedules."""

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from homebrew_harness.runs.spec import RunSpec
from .protocol import fingerprint as fingerprint, request_key as request_key
from homebrew_harness.security.safe_files import canonical_root

NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
SERVICE_ID = re.compile(r"svc_[0-9a-f]{32}\Z")
WORK_ID = re.compile(r"work_[0-9a-f]{32}\Z")


def seconds(value, name, minimum=0.01, maximum=604800):
    if (
        type(value) not in {int, float}
        or not math.isfinite(value)
        or not minimum <= value <= maximum
    ):
        raise ValueError("Invalid service duration: " + name)
    return value


@dataclass(frozen=True)
class ScheduleSpec:
    name: str
    every_seconds: float
    action: str = "record_tick"
    prompt: str | None = None

    def __post_init__(self):
        if not isinstance(self.name, str) or not NAME.fullmatch(self.name):
            raise ValueError("Schedule names must be short identifiers.")
        seconds(self.every_seconds, "every_seconds", minimum=0.1, maximum=31536000)
        if self.action not in {"record_tick", "submit_work"}:
            raise ValueError("P1 schedules support record_tick or submit_work.")
        if self.action == "submit_work":
            RunSpec(self.prompt)
        elif self.prompt is not None:
            raise ValueError("record_tick does not accept a prompt.")


@dataclass(frozen=True)
class ServiceSpec:
    workspace: str
    run_defaults: dict = field(default_factory=dict)
    schedules: tuple = ()
    profile: str = "manual"
    poll_seconds: float = 0.1
    shutdown_seconds: float = 5
    queue_limit: int = 1024
    control_port: int = 0
    sources: tuple = ()
    maintenance: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.profile not in {"manual", "repository_maintenance"}:
            raise ValueError("Unsupported service profile.")
        path = Path(self.workspace).expanduser()
        if not path.is_absolute():
            raise ValueError("Service workspace must be an absolute host path.")
        object.__setattr__(self, "workspace", str(canonical_root(path)))
        if not isinstance(self.run_defaults, dict) or "prompt" in self.run_defaults:
            raise ValueError(
                "run_defaults contains fixed RunSpec settings, without prompt."
            )
        defaults = dict(self.run_defaults)
        unknown = set(defaults) - (set(RunSpec.__dataclass_fields__) - {"prompt"})
        if unknown:
            raise ValueError("Unknown service Run defaults.")
        RunSpec("Validate service execution scope", **defaults)
        object.__setattr__(
            self, "run_defaults", json.loads(json.dumps(defaults, allow_nan=False))
        )
        if not isinstance(self.schedules, (tuple, list)) or len(self.schedules) > 32:
            raise ValueError("Service schedules must contain at most 32 entries.")
        schedules = tuple(
            s if isinstance(s, ScheduleSpec) else ScheduleSpec(**s)
            for s in self.schedules
        )
        if len({s.name for s in schedules}) != len(schedules):
            raise ValueError("Schedule names must be unique.")
        object.__setattr__(self, "schedules", schedules)
        seconds(self.poll_seconds, "poll_seconds", maximum=60)
        seconds(self.shutdown_seconds, "shutdown_seconds", maximum=60)
        if type(self.queue_limit) is not int or not 1 <= self.queue_limit <= 10000:
            raise ValueError("Invalid service queue limit.")
        from .maintenance_spec import validate_sources, validate_policy

        object.__setattr__(self, "sources", validate_sources(self.sources))
        if self.profile == "manual" and (self.sources or self.maintenance):
            raise ValueError(
                "Maintenance sources/policy require repository_maintenance profile."
            )
        if self.profile == "repository_maintenance":
            object.__setattr__(
                self,
                "maintenance",
                validate_policy(
                    self.maintenance, self.run_spec("Validate maintenance scope")
                ),
            )
        if type(self.control_port) is not int or not 0 <= self.control_port <= 65535:
            raise ValueError("Invalid service control port.")

    def run_spec(self, prompt, goal=None):
        values = dict(self.run_defaults)
        if goal is not None:
            values["goal"] = goal
        return RunSpec(prompt, **values)

    def data(self):
        return json.loads(json.dumps(asdict(self), allow_nan=False))

    def validate_location(self, control):
        import os, stat

        control, workspace = canonical_root(Path(control)), Path(self.workspace)
        info = control.stat()
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("Service control directory must be owner-only.")
        if control.is_relative_to(workspace) or workspace.is_relative_to(control):
            raise ValueError(
                "Service control and execution directories must be separate."
            )
        import homebrew_harness

        installed = Path(homebrew_harness.__file__).resolve().parent
        if installed.is_relative_to(workspace):
            raise ValueError(
                "Service runtime must be installed outside its mutable workspace."
            )

    @classmethod
    def load(cls, path, control):
        path = Path(path).expanduser().resolve()
        with path.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("Service specification exceeds 64 KiB.")
        data = json.loads(raw)
        if (
            not isinstance(data, dict)
            or type(data.get("version")) is not int
            or data.pop("version", None) != 1
        ):
            raise ValueError("Service specification requires version 1.")
        spec = cls(**data)
        spec.validate_location(control)
        if path.is_relative_to(Path(spec.workspace)) or path.is_relative_to(
            Path(control).resolve()
        ):
            raise ValueError(
                "Service specification must remain outside both workspaces."
            )
        return spec


def runtime_binding():
    import homebrew_harness

    root = Path(homebrew_harness.__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return {"version": homebrew_harness.__version__, "fingerprint": digest.hexdigest()}
