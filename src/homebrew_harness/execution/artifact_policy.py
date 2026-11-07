"""Small immutable output rules selected and bounded by the host."""

import re
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath

from homebrew_harness.security.data_boundary import sensitive_path


class ArtifactError(ValueError):
    pass


def artifact_path(value, *, target=False):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or "\\" in value
        or "\0" in value
    ):
        raise ArtifactError("Artifact paths must be nonempty relative POSIX paths.")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(p in {"", ".", ".."} for p in value.split("/"))
        or str(path) != value
    ):
        raise ArtifactError(
            "Artifact paths cannot be absolute, normalized aliases or traverse parents."
        )
    protected = {
        ".git",
        ".harness",
        ".memory",
        ".tasks",
        ".mailboxes",
        ".worktrees",
        ".secrets",
        ".config",
        ".ssh",
        ".aws",
        ".docker",
        ".venv",
        ".netrc",
        ".pypirc",
        ".npmrc",
        ".kube",
        ".agents",
        ".codex",
        ".trae",
    }
    if sensitive_path(path.parts) or any(
        p.lower() in protected or p.lower().startswith(".env") for p in path.parts
    ):
        raise ArtifactError(
            "Artifact paths cannot access credentials or internal administration."
        )
    if target and (
        path.parts[0] == "tests"
        or path.name
        in {
            "pyproject.toml",
            "MANIFEST.in",
            "requirements.txt",
            "pytest.ini",
            "conftest.py",
            "setup.py",
            "setup.cfg",
        }
    ):
        raise ArtifactError(
            "Artifact targets cannot replace protected tests or execution configuration."
        )
    return value


@dataclass(frozen=True)
class ArtifactFile:
    source: str
    target: str
    max_bytes: int = 1048576

    def __post_init__(self):
        artifact_path(self.source)
        artifact_path(self.target, target=True)
        if type(self.max_bytes) is not int or not 0 <= self.max_bytes <= 16 * 1048576:
            raise ArtifactError("Invalid artifact file byte limit.")


@dataclass(frozen=True)
class ArtifactProfile:
    name: str
    files: tuple
    max_bytes: int = 16 * 1048576

    def __post_init__(self):
        if not isinstance(self.name, str) or not re.fullmatch(
            r"[a-z][a-z0-9_-]{0,63}", self.name
        ):
            raise ArtifactError("Invalid host artifact rule name.")
        files = tuple(
            f if isinstance(f, ArtifactFile) else ArtifactFile(**f) for f in self.files
        )
        if (
            not 1 <= len(files) <= 16
            or len({f.source for f in files}) != len(files)
            or len({f.target for f in files}) != len(files)
        ):
            raise ArtifactError(
                "Output rules need 1-16 distinct source and target files."
            )
        targets = [f.target for f in files]
        if any(a != b and b.startswith(a + "/") for a in targets for b in targets):
            raise ArtifactError("Output targets cannot contain one another.")
        if type(self.max_bytes) is not int or not 0 <= self.max_bytes <= 16 * 1048576:
            raise ArtifactError("Invalid artifact bundle byte limit.")
        object.__setattr__(self, "files", files)

    def data(self):
        return asdict(self)


def output_profiles(values):
    if not isinstance(values, (tuple, list)) or len(values) > 16:
        raise ArtifactError("Host output rules must be a bounded sequence.")
    profiles = tuple(
        p if isinstance(p, ArtifactProfile) else ArtifactProfile(**p) for p in values
    )
    if len({p.name for p in profiles}) != len(profiles):
        raise ArtifactError("Output rule names must be unique.")
    return profiles
