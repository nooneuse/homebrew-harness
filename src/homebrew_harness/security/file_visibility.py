"""Model-visible files, separate from internal descriptor-safe storage."""

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from .data_boundary import sensitive_path
from .safe_files import relative_parts, canonical_root

_ACCESS = ContextVar("model_file_access", default=None)


def private_parts(parts):
    return (
        sensitive_path(parts)
        or ".git" in parts
        or any(
            parts[i : i + 2]
            in {(".harness", "protocol-state"), (".harness", "artifact-bundles")}
            for i in range(len(parts) - 1)
        )
    )


@contextmanager
def file_access_scope(archive_store=None, *, restrict_archives=False):
    token = _ACCESS.set((archive_store, restrict_archives))
    try:
        yield
    finally:
        _ACCESS.reset(token)


def visible(root, path):
    try:
        selected = Path(path)
        if selected.is_absolute() and selected.is_relative_to(root.absolute()):
            selected = selected.relative_to(root.absolute())
        root = canonical_root(root)
        parts = relative_parts(str(selected), root)
        if private_parts(parts):
            return False
        access = _ACCESS.get()
        if ".harness" in parts and access and access[1]:
            return access[0] is not None and access[0].owns(str(root.joinpath(*parts)))
        return True
    except (OSError, ValueError, RuntimeError):
        return False


def assert_visible(root, path):
    if not visible(root, path):
        raise ValueError(
            "This model context cannot read private runtime data or another context's archive."
        )


def traversable(root, path):
    parts = Path(path).parts
    if private_parts(parts):
        return False
    access = _ACCESS.get()
    if ".harness" in parts and access and access[1]:
        saved = getattr(access[0], "_saved", set()) if access[0] else set()
        return any(Path(ref).is_relative_to(root / path) for ref in saved.copy())
    return True


def check_pattern(pattern):
    if (
        Path(pattern).is_absolute()
        or ".." in Path(pattern).parts
        or private_parts(Path(pattern).parts)
    ):
        raise ValueError(
            "Use a visible relative pattern without parent traversal or private runtime paths."
        )
