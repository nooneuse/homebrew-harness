"""Host policy for service proposals; scope selection can only narrow Run defaults."""

import json
import math
import re
from pathlib import PurePosixPath

NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
CATEGORIES = {"bug", "feature", "documentation", "maintenance", "question"}
DISPOSITIONS = {"accept", "needs_info", "defer", "reject", "merge", "decide"}


def text(value, label="text", maximum=64000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(label + " must be nonempty bounded text.")
    return value


def number(value, label, minimum, maximum, *, integer=False):
    if (
        type(value) not in ({int} if integer else {int, float})
        or not math.isfinite(value)
        or not minimum <= value <= maximum
    ):
        raise ValueError("Invalid " + label)
    return value


def within(path, prefixes):
    path = PurePosixPath(path)
    return any(
        prefix == "." or path.is_relative_to(PurePosixPath(prefix))
        for prefix in prefixes
    )


def validate_sources(sources):
    from homebrew_harness.platforms.registry import validate_declaration

    if not isinstance(sources, (list, tuple)) or len(sources) > 8:
        raise ValueError("Sources must contain at most eight host declarations.")
    result = [validate_declaration(s, "repository_events", named=True) for s in sources]
    if len({s["name"] for s in result}) != len(result):
        raise ValueError("Source names must be unique.")
    instances = {}
    for source in result:
        identity = (source["provider"], source["instance"])
        connection = (source["api_url_env"], source.get("ca_file_env"))
        if identity in instances and instances[identity] != connection:
            raise ValueError(
                "A platform instance alias has conflicting connection declarations."
            )
        instances[identity] = connection
    return tuple(result)


def validate_policy(raw, run_spec):
    if not isinstance(raw, dict) or set(raw) - {
        "objectives",
        "non_goals",
        "scopes",
        "auto_categories",
        "authority_seconds",
        "report_seconds",
        "job_seconds",
        "job_turns",
        "job_attempts",
        "model_profile",
    }:
        raise ValueError("Invalid maintenance policy fields.")
    policy = {
        "objectives": "Maintain the project within its existing contract.",
        "non_goals": "Do not publish external changes.",
        "scopes": {},
        "auto_categories": [],
        "authority_seconds": 3600,
        "report_seconds": 43200,
        "job_seconds": 120,
        "job_turns": 8,
        "job_attempts": 3,
        "model_profile": None,
        **raw,
    }
    text(policy["objectives"], "objectives", 8000)
    text(policy["non_goals"], "non_goals", 8000)
    for key in ("authority_seconds", "report_seconds"):
        number(policy[key], key, 1, 31536000)
    number(policy["job_seconds"], "job_seconds", 1, 3600)
    number(policy["job_turns"], "job_turns", 1, 40, integer=True)
    number(policy["job_attempts"], "job_attempts", 1, 10, integer=True)
    if policy["model_profile"] is not None and (
        not isinstance(policy["model_profile"], str)
        or not NAME.fullmatch(policy["model_profile"])
    ):
        raise ValueError("Invalid service model alias.")
    if not isinstance(policy["auto_categories"], (list, tuple)) or any(
        c not in CATEGORIES for c in policy["auto_categories"]
    ):
        raise ValueError("Invalid automatically accepted categories.")
    if not isinstance(policy["scopes"], dict) or len(policy["scopes"]) > 16:
        raise ValueError("Invalid scope catalog.")
    scopes = {}
    for name, scope in policy["scopes"].items():
        if (
            not NAME.fullmatch(name)
            or not isinstance(scope, dict)
            or set(scope) - {"write_paths", "tools", "allow_tests"}
        ):
            raise ValueError("Invalid execution scope.")
        compiled = {
            key: list(scope.get(key, getattr(run_spec, key)))
            for key in ("write_paths", "tools", "allow_tests")
        }
        from homebrew_harness.runs.spec import RunSpec

        RunSpec(**{**run_spec.data(), **compiled})
        if not all(
            within(path, run_spec.write_paths) for path in compiled["write_paths"]
        ):
            raise ValueError("Scope widens writable paths.")
        if set(compiled["tools"]) - set(run_spec.tools) or set(
            compiled["allow_tests"]
        ) - set(run_spec.allow_tests):
            raise ValueError("Scope widens tools or fixed tests.")
        scopes[name] = compiled
    policy["scopes"] = scopes
    return json.loads(json.dumps(policy, allow_nan=False))


def validate_proposal(raw, kind, policy):
    if kind == "review":
        if (
            not isinstance(raw, dict)
            or set(raw) != {"text", "findings"}
            or not isinstance(raw["findings"], list)
            or len(raw["findings"]) > 32
        ):
            raise ValueError("Invalid independent review proposal.")
        text(raw["text"], maximum=16000)
        for item in raw["findings"]:
            if (
                not isinstance(item, dict)
                or set(item) != {"severity", "path", "message"}
                or item["severity"] not in {"blocking", "warning"}
            ):
                raise ValueError("Invalid review finding.")
            text(item["path"], maximum=1000)
            text(item["message"], maximum=4000)
        return raw
    if kind == "ci_triage":
        if (
            not isinstance(raw, dict)
            or set(raw) != {"classification", "reason", "plan"}
            or raw["classification"]
            not in {"code", "baseline", "flaky", "infrastructure", "unknown"}
        ):
            raise ValueError("Invalid CI diagnosis.")
        text(raw["reason"], maximum=8000)
        text(raw["plan"], maximum=16000)
        return raw

    if kind != "triage":
        if not isinstance(raw, dict) or set(raw) - {"text", "work_id", "plan"}:
            raise ValueError(
                "A conversation/report proposal requires text and optional work_id/plan."
            )
        text(raw.get("text"), maximum=32000)
        if kind == "report" and set(raw) != {"text"}:
            raise ValueError("Reports accept text only.")
        if raw.get("plan") is not None:
            validate_proposal(raw["plan"], "triage", policy)
            if raw["plan"]["disposition"] not in {"accept", "decide"}:
                raise ValueError(
                    "Conversation execution proposals need an acceptance decision."
                )
        return raw
    fields = {
        "disposition",
        "category",
        "reason",
        "plan",
        "scope",
        "priority",
        "related_work",
    }
    if (
        not isinstance(raw, dict)
        or set(raw) - fields
        or raw.get("disposition") not in DISPOSITIONS
        or raw.get("category") not in CATEGORIES
    ):
        raise ValueError("Invalid triage proposal.")
    text(raw.get("reason"), "reason", 8000)
    text(raw.get("plan"), "plan", 16000)
    number(raw.get("priority", 0), "priority", 0, 100, integer=True)
    if (
        raw["disposition"] in {"accept", "decide"}
        and raw.get("scope") not in policy["scopes"]
    ):
        raise ValueError("Select a host execution scope before requesting work.")
    if raw.get("scope") is not None and raw["scope"] not in policy["scopes"]:
        raise ValueError("Unknown scope.")
    return raw
