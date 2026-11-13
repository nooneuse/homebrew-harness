"""Prepared Service phase outputs and the knowledge actually used to produce them."""

import copy

from .protocol import fingerprint
from .project_context import file_version


def overview_signature(project):
    if project is None:
        return None
    data = copy.deepcopy(project)
    data.pop("version", None)  # The full source inventory is navigation, not a dependency.
    for document in data.get("documents", []):
        document.pop("sha256", None)  # Unshown text does not invalidate an unchanged excerpt.
    return fingerprint(data)


def versions(root, paths):
    values = {}
    for path in paths:
        try:
            values[path] = file_version(root, path)
        except (OSError, ValueError):
            values[path] = None
    return values


def identity(job, phase, binding, prompts, plan=None):
    return {
        "input": fingerprint(job["input"]), "phase": phase,
        "model": binding.identity(), "prompts": (prompts or {}).get("signature"),
        "plan": fingerprint(plan) if plan is not None else None,
    }


def valid(output, signature, project, root):
    return bool(output and output["signature"] == signature
                and output["overview"] == overview_signature(project)
                and output["evidence"] == versions(root, output["evidence"]))
