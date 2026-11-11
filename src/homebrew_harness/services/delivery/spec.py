"""Host delivery declarations; values and publishing credentials remain external."""

import ipaddress
import json
import os
import re
import stat
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


def validate(raw):
    if raw == {}:
        return {}
    defaults = {
        "mode": "isolated",
        "target_branch": "main",
        "review_prefix": "harness/",
        "required_tests": [],
        "required_checks": [],
        "check_app_ids": [],
        "reviewer_ids": [],
        "required_approvals": 0,
        "require_review": True,
        "review_model": None,
        "merge": {"mode": "manual", "poll_seconds": 300},
        "auto_actions": [],
        "request_seconds": 10,
        "gate_seconds": 120,
        "authorization_seconds": 3600,
        "ci_poll_seconds": 30,
        "ci_wait_seconds": 3600,
        "ci_fix_limit": 2,
        "auto_ci_fix": False,
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
    }
    if not isinstance(raw, dict) or set(raw) - fields:
        raise ValueError("Invalid delivery declaration fields.")
    value = {**defaults, **raw}
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
        if not isinstance(value[key], (tuple, list)) or len(value[key]) > 32:
            raise ValueError("Invalid delivery collection: " + key)
    if not value["required_tests"] or any(
        not isinstance(n, str) or not n or len(n) > 100
        for n in [*value["required_tests"], *value["required_checks"]]
    ):
        raise ValueError("Delivery requires named fixed tests.")
    if any(type(n) is not int or n < 1 for n in value["check_app_ids"]):
        raise ValueError("Invalid trusted CI app IDs.")
    if any(type(n) is not int or n < 1 for n in value["reviewer_ids"]):
        raise ValueError("Invalid trusted reviewer IDs.")
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
    for key, minimum, maximum in (
        ("request_seconds", 1, 60),
        ("gate_seconds", 1, 3600),
        ("authorization_seconds", 1, 31536000),
        ("ci_poll_seconds", 0.1, 86400),
        ("ci_wait_seconds", 1, 31536000),
        ("ci_fix_limit", 0, 10),
    ):
        number(value[key], key, minimum, maximum, integer=key == "ci_fix_limit")
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
        if not isinstance(value["credentials_file"], str):
            raise ValueError("Credentials file must be an external host path.")
        value["credentials_file"] = str(Path(value["credentials_file"]).expanduser())
    return json.loads(json.dumps(value, allow_nan=False))


def read_values(spec, environment, roots):
    values = dict(environment)
    if spec["credentials_file"]:
        path = Path(spec["credentials_file"])
        if (
            not path.is_absolute()
            or path.is_symlink()
            or any(path.resolve().is_relative_to(Path(r).resolve()) for r in roots)
        ):
            raise ValueError("Delivery credentials must be an external host file.")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
            ):
                raise ValueError(
                    "Delivery credentials must be an owner-only regular file."
                )
            with os.fdopen(fd, "r", closefd=False) as stream:
                raw = stream.read(65537)
        finally:
            os.close(fd)
        if len(raw) > 65536:
            raise ValueError("Delivery credential file exceeds its limit.")
        import io
        from dotenv import dotenv_values

        dedicated = dict(dotenv_values(stream=io.StringIO(raw), interpolate=False))
        values = dedicated
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
