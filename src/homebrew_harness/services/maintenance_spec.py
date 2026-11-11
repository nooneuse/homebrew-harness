"""Host policy for service proposals; scope selection can only narrow Run defaults."""

import json
import math
import re
from pathlib import PurePosixPath

NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
CATEGORIES = {"bug", "feature", "documentation", "maintenance", "question"}
DISPOSITIONS = {"accept", "needs_info", "defer", "reject", "merge", "decide"}
INSTRUCTION_TARGETS = frozenset({
    "common", "development", "triage", "discussion", "review", "ci_triage",
    "audit", "conversation", "report", "incident", "upgrade_assessment",
})


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


def instructions_for(policy, kind):
    """Expose only shared and relevant writing/development guidance."""
    values = policy.get("instructions", {})
    return {key: values[key] for key in ("common", kind) if key in values}


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
        "instructions",
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
        "instructions": {},
        **raw,
    }
    text(policy["objectives"], "objectives", 8000)
    text(policy["non_goals"], "non_goals", 8000)
    instructions = policy["instructions"]
    if not isinstance(instructions, dict) or set(instructions) - INSTRUCTION_TARGETS:
        raise ValueError("Instructions must select known service task names.")
    from homebrew_harness.security.data_boundary import redact

    for value in instructions.values():
        text(value, "task instructions", 8000)
        if redact(value) != value:
            raise ValueError("Task instructions cannot contain private connection values.")
    if sum(map(len, instructions.values())) > 16000:
        raise ValueError("Combined task instructions exceed 16000 characters.")
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
