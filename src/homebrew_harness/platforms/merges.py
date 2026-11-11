"""Portable, bounded merge observations and fixed-head plans."""

import re

from .contracts import ObjectRef, bounded

SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
METHODS = frozenset({"merge", "squash", "rebase"})
CLOSING = re.compile(
    r"(?i)\b(?:close[sd]?|fix(?:es|ed)?|resolve[sd]?)\s*:?\s+(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#\d+"
)


def commit(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValueError("Invalid merge commit identity.")
    return value


def validate_plan(plan, spec):
    if (
        not isinstance(plan, dict)
        or not isinstance(plan.get("method"), str)
        or not isinstance(plan.get("branch"), str)
    ):
        raise ValueError("Invalid fixed merge plan.")
    ref = ObjectRef(**plan["subject"])
    if (
        ref.kind != "change_request"
        or ref.number is None
        or ref.scope() != (spec["provider"], spec["instance"], spec["project"])
        or plan.get("method") not in METHODS
        or plan.get("branch", "").startswith(spec["review_prefix"]) is not True
        or spec["merge"]["mode"] != "service_approved"
        or plan["method"] != spec["merge"]["method"]
    ):
        raise ValueError("Merge plan is outside the registered delivery scope.")
    commit(plan["commit"])
    bounded(plan["branch"], "source branch", 200)
    bounded(plan["hash"], "candidate hash", 100)
    bounded(plan["gate_hash"], "merge gate hash", 100)
    return ref


def receipt(subject, observed, head, *, method=None, request=None):
    ref = ObjectRef(**subject)
    commit(head)
    if ref.kind != "change_request" or observed["head"] != head:
        raise ValueError("Merge observation belongs to another candidate.")
    merged = observed.get("merged") is True
    result = observed.get("result_commit")
    if merged:
        commit(result)
    return {
        "subject": ref.data(),
        "head": head,
        "state": "merged" if merged else "pending",
        "result_commit": result if merged else None,
        "merged_at": observed.get("merged_at") if merged else None,
        "actor_id": observed.get("merged_by") if merged else None,
        "method": method,
        "request": request,
        "origin": "platform",
    }
