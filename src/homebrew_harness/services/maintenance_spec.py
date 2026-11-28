from homebrew_harness.runtime.limits import current_limits
"""Host policy for service proposals; scope selection can only narrow Run defaults."""

from homebrew_harness.runtime.errors import boundary_error
from homebrew_harness.configuration import (
    DEFAULT_TASK_SECONDS,
)

import json
import math
import re
from pathlib import PurePosixPath

NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
CATEGORIES = {"bug", "feature", "documentation", "maintenance", "question"}
DISPOSITIONS = {"accept", "needs_info", "defer", "reject", "merge", "decide"}
INSTRUCTION_TARGETS = frozenset({
    "common", "development", "triage", "discussion", "review", "ci_triage", "validation_plan", "publication",
    "audit", "conversation", "report", "incident", "upgrade_assessment", "recovery_plan",
    "integration_assessment",
})


def text(value, label="text", maximum=64000):
    if not isinstance(value, str) or not value.strip() or (current_limits().service_text_chars is not None and len(value) > current_limits().service_text_chars):
        raise boundary_error(ValueError, "service_configuration", label + " must be nonempty bounded text.")
    return value


def number(value, label, minimum, maximum, *, integer=False):
    if value is None and label in {"authority_seconds", "authorization_seconds", "gate_seconds", "ci_wait_seconds", "prepare_seconds", "job_seconds"}:
        return None
    if (
        type(value) not in ({int} if integer else {int, float})
        or not integer and not math.isfinite(value)
        or value < minimum
        or label in {"cursor", "offset"} and value > 2**63 - 1
    ):
        raise boundary_error(ValueError, "service_configuration", "Invalid " + label)
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

    if not isinstance(sources, (list, tuple)) or (current_limits().service_sources is not None and len(sources) > current_limits().service_sources):
        raise boundary_error(ValueError, "service_configuration", "Sources must be a list within any configured service_sources limit.")
    result = [validate_declaration(s, "repository_events", named=True) for s in sources]
    if len({s["name"] for s in result}) != len(result):
        raise boundary_error(ValueError, "service_configuration", "Source names must be unique.")
    instances = {}
    for source in result:
        if source.get("credentials_file"):
            continue
        identity = (source["provider"], source["instance"])
        connection = (source["api_url_env"], source.get("ca_file_env"))
        if identity in instances and instances[identity] != connection:
            raise boundary_error(ValueError, "service_configuration",
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
        "grant_on_requirements_acceptance",
        "authority_seconds",
        "report_seconds",
        "job_seconds",
        "job_turns",
        "job_call_warning",
        "job_attempts",
        "job_attempt_warning",
        "job_warning_seconds",
        "model_profile",
        "instructions",
    }:
        raise boundary_error(ValueError, "service_configuration", "Invalid maintenance policy fields.")
    policy = {
        "objectives": "Maintain the project within its existing contract.",
        "non_goals": "Do not publish external changes.",
        "scopes": {},
        "auto_categories": [],
        "authority_seconds": None,
        "report_seconds": 43200,
        "job_seconds": None,
        "job_warning_seconds": DEFAULT_TASK_SECONDS,
        "job_turns": None,
        "job_call_warning": 40,
        "job_attempts": None,
        "job_attempt_warning": 3,
        "model_profile": None,
        "instructions": {},
        **raw,
    }
    text(policy["objectives"], "objectives", 8000)
    text(policy["non_goals"], "non_goals", 8000)
    instructions = policy["instructions"]
    if not isinstance(instructions, dict) or set(instructions) - INSTRUCTION_TARGETS:
        raise boundary_error(ValueError, "service_configuration", "Instructions must select known service task names.")
    from homebrew_harness.security.data_boundary import redact

    for value in instructions.values():
        text(value, "task instructions", 8000)
        if redact(value) != value:
            raise boundary_error(ValueError, "service_configuration", "Task instructions cannot contain private connection values.")
    if current_limits().service_text_chars is not None and sum(map(len, instructions.values())) > current_limits().service_text_chars:
        raise boundary_error(ValueError, "service_configuration", "Combined task instructions exceed 16000 characters.")
    from homebrew_harness.runtime.limits import positive
    positive(policy["authority_seconds"], "authority_seconds", nullable=True)
    positive(policy["report_seconds"], "report_seconds")
    positive(policy["job_seconds"], "job_seconds", nullable=True)
    positive(policy["job_warning_seconds"], "job_warning_seconds", nullable=True)
    positive(policy["job_attempt_warning"], "job_attempt_warning", nullable=True, integer=True)
    if policy["job_turns"] is not None:
        positive(policy["job_turns"], "job_turns", nullable=True, integer=True)
    if policy["job_call_warning"] is not None:
        positive(policy["job_call_warning"], "job_call_warning", nullable=True, integer=True)
    positive(policy["job_attempts"], "job_attempts", nullable=True, integer=True)
    if policy["model_profile"] is not None and (
        not isinstance(policy["model_profile"], str)
        or not NAME.fullmatch(policy["model_profile"])
    ):
        raise boundary_error(ValueError, "service_configuration", "Invalid service model alias.")
    if not isinstance(policy["auto_categories"], (list, tuple)) or any(
        c not in CATEGORIES for c in policy["auto_categories"]
    ):
        raise boundary_error(ValueError, "service_configuration", "Invalid automatically accepted categories.")
    # An omitted opt-in stays absent so persisted Work authority and discussion bases still match.
    if type(policy.get("grant_on_requirements_acceptance", False)) is not bool:
        raise boundary_error(ValueError, "service_configuration", "grant_on_requirements_acceptance must be Boolean.")
    if not isinstance(policy["scopes"], dict) or (current_limits().service_scopes is not None and len(policy["scopes"]) > current_limits().service_scopes):
        raise boundary_error(ValueError, "service_configuration", "Invalid scope catalog.")
    scopes = {}
    for name, scope in policy["scopes"].items():
        if (
            not NAME.fullmatch(name)
            or not isinstance(scope, dict)
            or set(scope) - {"write_paths", "tools", "allow_tests", "allow_attention"}
        ):
            raise boundary_error(ValueError, "service_configuration", "Invalid execution scope.")
        allow_attention = scope.get("allow_attention", True)
        if type(allow_attention) is not bool:
            raise boundary_error(ValueError, "service_configuration", "allow_attention must be Boolean.")
        compiled = {
            key: list(scope.get(key, getattr(run_spec, key)))
            for key in ("write_paths", "tools", "allow_tests")
        }
        tools = compiled["tools"]
        if allow_attention:
            if "request_attention" in run_spec.tools and "request_attention" not in tools:
                tools.append("request_attention")
        elif "request_attention" in tools:
            if "tools" in scope:
                raise boundary_error(ValueError, "service_configuration", "allow_attention=False conflicts with request_attention in scope tools.")
            tools.remove("request_attention")
        from homebrew_harness.runs.spec import RunSpec

        RunSpec(**{**run_spec.data(), **compiled})
        if not all(
            within(path, run_spec.write_paths) for path in compiled["write_paths"]
        ):
            raise boundary_error(ValueError, "service_configuration", "Scope widens writable paths.")
        if set(compiled["tools"]) - set(run_spec.tools) or set(
            compiled["allow_tests"]
        ) - set(run_spec.allow_tests):
            raise boundary_error(ValueError, "service_configuration", "Scope widens tools or fixed tests.")
        if "allow_attention" in scope:
            compiled["allow_attention"] = allow_attention
        scopes[name] = compiled
    policy["scopes"] = scopes
    return json.loads(json.dumps(policy, allow_nan=False))
