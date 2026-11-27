from homebrew_harness.runtime.limits import current_limits
"""Host delivery declarations; values and publishing credentials remain external."""

import ipaddress
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from homebrew_harness.services.maintenance_spec import number
from homebrew_harness.security.data_boundary import register_private_values

ACTIONS = frozenset(
    {"publish_branch", "ensure_change_request", "merge_change_request", "post_comment"}
)
ENV = re.compile(r"[A-Z][A-Z0-9_]{0,100}\Z")
REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}\Z")


def branch(value, *, prefix=False):
    if (
        not isinstance(value, str)
        or not REF.fullmatch(value)
        or any(s in value for s in ("..", "@{", "//", "\\"))
        or value.endswith((".lock", "."))
        or not prefix
        and value.endswith("/")
        or any(p in {"", "."} for p in value.rstrip("/").split("/"))
    ):
        raise ValueError("Invalid host delivery branch.")
    if prefix and not value.endswith("/"):
        raise ValueError("Review branch prefix must end with /.")
    return value


def validate_base_update(mode):
    if not isinstance(mode, str) or mode not in {"manual", "automatic", "periodic"}:
        raise ValueError("Base updates must be manual, automatic or periodic.")
    return {"base_update": mode}


PERIODIC_DEFAULTS = {
    "assessment_seconds": 6 * 3600,
    "min_integration_seconds": 6 * 3600,
    "max_deferral_seconds": 24 * 3600,
}


def periodic_settings(spec):
    return {**PERIODIC_DEFAULTS, **spec.get("periodic_integration", {})}


def validate_periodic(raw):
    from homebrew_harness.runtime.limits import positive

    if not isinstance(raw, dict) or raw.keys() - PERIODIC_DEFAULTS.keys():
        raise ValueError("Invalid periodic integration settings.")
    values = periodic_settings({"periodic_integration": raw})
    for key, value in values.items():
        positive(value, key, zero=key == "min_integration_seconds")
        if value > 30 * 86400:
            raise ValueError("Periodic integration windows cannot exceed 30 days.")
    if values["max_deferral_seconds"] < max(values["assessment_seconds"], values["min_integration_seconds"]):
        raise ValueError("Maximum integration deferral must cover the assessment and minimum integration intervals.")
    return values


def validate_ci_limits(limit, warning):
    if limit is not None:
        number(limit, "ci_fix_limit", 0, 10, integer=True)
    if warning is not None:
        number(warning, "ci_fix_warning", 1, 1024, integer=True)
    return {"ci_fix_limit": limit, "ci_fix_warning": warning}


def validate(raw):
    if raw == {}:
        return {}
    defaults = {
        "mode": "isolated",
        "target_branch": "main",
        "base_update": "manual",
        "review_prefix": "harness/",
        "required_tests": [],
        "required_checks": [],
        "check_app_ids": [],
        "reviewer_ids": [],
        "required_approvals": 0,
        "require_review": False,
        "review_model": None,
        "merge": {"mode": "manual", "poll_seconds": 300},
        "auto_actions": [],
        "request_seconds": 10,
        "gate_seconds": None,
        "authorization_seconds": None,
        "ci_poll_seconds": 30,
        "ci_wait_seconds": None,
        "ci_wait_warning_seconds": 3600,
        "ci_fix_limit": None,
        "ci_fix_warning": 2,
        "auto_ci_fix": True,
        "credentials_file": None,
        "git_token_env": None,
        "git_user_env": None,
        "ca_file_env": None,
    }
    fields = set(defaults) | {
        "git_executable",
        "remote_env",
        "provider",
        "instance",
        "project",
        "api_url_env",
        "api_token_env",
        "author_name_env",
        "author_email_env",
        "periodic_integration",
        "auto_publish_pr_text",
        "allow_closing_keywords",
    }
    if not isinstance(raw, dict) or set(raw) - fields:
        raise ValueError("Invalid delivery declaration fields.")
    value = {**defaults, **raw}
    if "periodic_integration" in value:
        value["periodic_integration"] = validate_periodic(value["periodic_integration"])
    merge = value["merge"]
    if not isinstance(merge, dict) or set(merge) - {"mode", "method", "poll_seconds"}:
        raise ValueError("Invalid merge policy.")
    merge = {"mode": "manual", "poll_seconds": 300, **merge}
    if not isinstance(merge["mode"], str) or merge["mode"] not in {
        "manual",
        "service_approved",
    }:
        raise ValueError("Unknown merge responsibility mode.")
    if merge["mode"] == "manual" and "method" in merge:
        raise ValueError("Manual merge does not select the maintainer's method.")
    if merge["mode"] == "service_approved":
        from homebrew_harness.platforms.merges import METHODS

        merge.setdefault("method", "merge")
        if not isinstance(merge["method"], str) or merge["method"] not in METHODS:
            raise ValueError("Unknown native merge method.")
    number(merge["poll_seconds"], "merge poll seconds", 0.05, 86400)
    value["merge"] = merge
    if not isinstance(value["mode"], str) or value["mode"] not in {"isolated", "local"}:
        raise ValueError("Unknown delivery execution mode.")
    if (
        not isinstance(value.get("git_executable"), str)
        or not Path(value["git_executable"]).is_absolute()
    ):
        raise ValueError("Delivery requires a fixed absolute Git executable.")
    validate_base_update(value["base_update"])
    branch(value["target_branch"])
    branch(value["review_prefix"], prefix=True)
    if value["target_branch"].startswith(value["review_prefix"]):
        raise ValueError("Target branch cannot share the generated review namespace.")
    for key in (
        "remote_env",
        "api_url_env",
        "api_token_env",
        "author_name_env",
        "author_email_env",
        "git_token_env",
        "git_user_env",
        "ca_file_env",
    ):
        item = value.get(key)
        if key in {"git_token_env", "git_user_env", "ca_file_env"} and item is None:
            continue
        if not isinstance(item, str) or not ENV.fullmatch(item):
            raise ValueError("Delivery values require host environment references.")
    for key in (
        "required_tests",
        "required_checks",
        "auto_actions",
        "check_app_ids",
        "reviewer_ids",
    ):
        if not isinstance(value[key], (tuple, list)) :
            raise ValueError("Invalid delivery collection: " + key)
    if any(
        not isinstance(n, str) or not n or len(n) > 100
        for n in [*value["required_tests"], *value["required_checks"]]
    ):
        raise ValueError("Delivery requires named fixed tests.")
    if any(type(n) is not int or n < 1 for n in value["check_app_ids"]):
        raise ValueError("Invalid trusted CI app IDs.")
    if any(type(n) is not int or n < 1 for n in value["reviewer_ids"]):
        raise ValueError("Invalid trusted reviewer IDs.")
    if value["reviewer_ids"] and value["required_approvals"] > len(set(value["reviewer_ids"])):
        raise ValueError("required_approvals exceeds the eligible reviewer set.")
    number(
        value["required_approvals"],
        "required_approvals",
        0,
        len(set(value["reviewer_ids"])),
        integer=True,
    )
    if value["required_checks"] and not value["check_app_ids"]:
        raise ValueError("Required checks need trusted app IDs.")
    if any(not isinstance(n, str) for n in value["auto_actions"]) or set(
        value["auto_actions"]
    ) - {"publish_branch", "ensure_change_request"}:
        raise ValueError("Only bounded review-branch actions can be preauthorized.")
    for key in ("require_review", "auto_ci_fix"):
        if type(value[key]) is not bool:
            raise ValueError("Invalid delivery boolean.")
    # Keep omitted opt-ins out of the persisted spec and its binding fingerprint.
    if type(value.get("allow_closing_keywords", False)) is not bool:
        raise ValueError("allow_closing_keywords must be Boolean.")
    auto_publish_pr_text = value.get("auto_publish_pr_text", False)
    if type(auto_publish_pr_text) is not bool:
        raise ValueError("auto_publish_pr_text must be Boolean.")
    if auto_publish_pr_text and "ensure_change_request" not in value["auto_actions"]:
        raise ValueError("auto_publish_pr_text requires ensure_change_request in auto_actions.")
    for key, minimum, maximum in (
        ("request_seconds", 1, 60),
        ("gate_seconds", 1, 3600),
        ("authorization_seconds", 1, 31536000),
        ("ci_poll_seconds", 0.1, 86400),
        ("ci_wait_seconds", 1, 31536000),
    ):
        number(value[key], key, minimum, maximum)
    validate_ci_limits(value["ci_fix_limit"], value["ci_fix_warning"])
    from homebrew_harness.platforms.registry import PROVIDERS

    if (
        value.get("provider") not in PROVIDERS
        or "delivery" not in PROVIDERS[value["provider"]]
    ):
        raise ValueError("Platform has no implemented delivery adapter.")
    value["project"] = PROVIDERS[value["provider"]]["project"](value.get("project"))
    if not isinstance(value.get("instance"), str) or not re.fullmatch(
        r"[a-z][a-z0-9_-]{0,63}", value["instance"]
    ):
        raise ValueError("Invalid delivery instance.")
    if value["mode"] == "isolated" and not value["credentials_file"]:
        raise ValueError(
            "Isolated delivery needs a dedicated external credentials file."
        )
    if value["credentials_file"] is not None:
        if not isinstance(value["credentials_file"], str) or not value["credentials_file"].strip():
            raise ValueError("Credentials file must be an external host path.")
        cred_path = Path(value["credentials_file"]).expanduser()
        if not cred_path.is_absolute():
            raise ValueError("Credentials file must be an external host path.")
        value["credentials_file"] = str(cred_path)
    return json.loads(json.dumps(value, allow_nan=False))


def read_values(spec, environment, roots):
    values = dict(environment)
    if spec["credentials_file"]:
        from homebrew_harness.platforms.registry import read_credentials_file

        values = read_credentials_file(
            spec["credentials_file"], roots=roots, label="Delivery credentials"
        )
    keys = {
        "remote",
        "api_url",
        "api_token",
        "author_name",
        "author_email",
        "git_token",
        "git_user",
        "ca_file",
    }
    result = {
        name: values.get(spec.get(name + "_env")) if spec.get(name + "_env") else None
        for name in keys
    }
    for key in ("remote", "api_url", "api_token", "author_name", "author_email"):
        if (
            not isinstance(result[key], str)
            or not result[key]
            or any(ord(c) < 32 for c in result[key])
        ):
            raise ValueError("Missing or invalid delivery host value: " + key)
    if spec.get("ca_file_env") and (
        not isinstance(result["ca_file"], str)
        or not result["ca_file"]
        or any(ord(c) < 32 for c in result["ca_file"])
    ):
        raise ValueError("Platform CA configuration is missing.")
    for key in ("api_token", "git_token"):
        if result.get(key):
            register_private_values(result[key])
    register_private_values(result["remote"], result["api_url"])
    remote = result["remote"]
    parsed = urlsplit(remote)
    if parsed.scheme in {"https", "ssh"}:
        if (
            parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.scheme == "https"
            and parsed.username
        ):
            raise ValueError("Git remote cannot embed credentials.")
    elif not Path(remote).is_absolute():
        raise ValueError(
            "Delivery Git remote requires HTTPS/SSH or a local absolute path."
        )
    if Path(remote).is_absolute() and any(
        Path(remote).resolve().is_relative_to(Path(r).resolve()) for r in roots
    ):
        raise ValueError(
            "A local Git remote must be outside mutable workspaces and control state."
        )
    if spec["mode"] == "local":
        if not Path(remote).is_absolute() or not Path(remote).is_dir():
            raise ValueError("Local delivery requires a local bare remote.")
        host = urlsplit(result["api_url"]).hostname
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host == "localhost"
        if (
            not loopback
            or result["api_token"] != "synthetic"
            or result.get("git_token")
        ):
            raise ValueError(
                "Local delivery only accepts a loopback fixture without publishing credentials."
            )
    else:
        for key in ("api_token", "git_token"):
            secret = result.get(key)
            if secret and any(secret == v for v in environment.values()):
                raise ValueError(
                    "Publishing credentials are present in the development environment."
                )
    return result
