"""Bundled guidance and host-selected external overrides, frozen per job."""

import hashlib
from pathlib import Path

from homebrew_harness.security.safe_files import SafeFile
from homebrew_harness.services.protocol import fingerprint

NAMES = {"assessment": "discussion-assessment.md", "draft": "discussion-draft.md"}


def read(path):
    path = Path(path)
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError("Discussion prompt files cannot be symlinks.")
    with SafeFile(path.parent, path.name) as file:
        raw, _, _ = file.snapshot(max_bytes=16384)
    if raw is None:
        raise FileNotFoundError(path)
    text = raw.decode("utf-8")
    if not text.strip() or len(text) > 8000:
        raise ValueError("Discussion prompt text must be nonempty and at most 8000 characters.")
    return text


def defaults():
    directory = Path(__file__).parent.parent / "prompts"
    return {name: read(directory / filename) for name, filename in NAMES.items()}


def load(files, *, builtin=None, roots=()):
    values = dict(builtin or defaults())
    for name, value in files.items():
        if name not in NAMES:
            raise ValueError("Unknown discussion prompt phase.")
        path = Path(value).expanduser()
        if not path.is_absolute() or any(path.resolve().is_relative_to(Path(root).resolve()) for root in roots):
            raise ValueError("Prompt overrides require external absolute paths.")
        values[name] = read(path)
    return {"texts": values, "signature": fingerprint(values)}


def identity(files, builtin):
    # Editing selected external text takes effect for new jobs, not fixed service identity.
    return {"files": dict(files), "builtin": {
        name: hashlib.sha256(text.encode()).hexdigest()
        for name, text in builtin.items() if name not in files
    }}
