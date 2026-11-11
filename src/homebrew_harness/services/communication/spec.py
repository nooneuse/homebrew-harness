"""Host declarations; transport values and write credentials remain external."""

import ipaddress
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from homebrew_harness.execution.model_connections import read_environment
from homebrew_harness.platforms.registry import validate_declaration, connection
from homebrew_harness.services.maintenance_spec import number, text

ACTIONS = {"post_comment", "reply_thread", "submit_review"}
INTENTS = {
    "acknowledge",
    "clarify",
    "followup",
    "review_feedback",
    "request_changes",
    "approve_review",
    "decline_direction",
    "roadmap_commitment",
}
AUTOMATIC = {"acknowledge", "clarify", "followup"}
NEXT = {
    "clarify",
    "discuss",
    "operator",
    "develop",
    "defer",
    "decline",
    "wait",
    "settle",
}


def validate(raw, sources):
    if raw == {}:
        return {}
    defaults = {
        "mode": "remote",
        "projects": [],
        "credentials_file": None,
        "enabled_actions": sorted(ACTIONS),
        "auto_intents": [],
        "poll_seconds": 43200,
        "debounce_seconds": 10,
        "max_debounce_seconds": 60,
        "authorization_seconds": 3600,
        "request_seconds": 10,
        "prepare_seconds": 120,
        "no_progress_rounds": 3,
        "model_profile": None,
        "review_model": None,
        "review_globs": ["**/*"],
        "max_paths": 10000,
        "max_bytes": 32000000,
        "max_file_bytes": 1000000,
        "required_tests": [],
        "require_tests_for_approval": False,
    }
    if not isinstance(raw, dict) or set(raw) - set(defaults):
        raise ValueError("Invalid communication policy fields.")
    value = {**defaults, **raw}
    if value["mode"] not in {"remote", "local"}:
        raise ValueError("Unknown communication mode.")
    if not isinstance(value["projects"], list) or not 1 <= len(value["projects"]) <= 8:
        raise ValueError("Communication requires bounded host project declarations.")
    declarations = []
    for project in value["projects"]:
        declaration = validate_declaration(project, "communication", named=True)
        if not any(
            all(
                declaration[k] == source[k] for k in ("provider", "instance", "project")
            )
            for source in sources
        ):
            raise ValueError(
                "Communication project must have a registered repository source."
            )
        declarations.append(declaration)
    if len({(p["provider"], p["instance"], p["project"]) for p in declarations}) != len(
        declarations
    ):
        raise ValueError("Duplicate communication project.")
    value["projects"] = declarations
    for name, choices in (("enabled_actions", ACTIONS), ("auto_intents", AUTOMATIC)):
        if (
            not isinstance(value[name], list)
            or any(not isinstance(v, str) for v in value[name])
            or set(value[name]) - choices
        ):
            raise ValueError("Invalid communication actions or automatic intents.")
    for name, low, high in [
        ("poll_seconds", 0.1, 31536000),
        ("debounce_seconds", 0, 86400),
        ("max_debounce_seconds", 0, 86400),
        ("authorization_seconds", 1, 31536000),
        ("request_seconds", 0.1, 60),
        ("prepare_seconds", 1, 3600),
        ("no_progress_rounds", 1, 100),
        ("max_paths", 1, 100000),
        ("max_bytes", 1000, 128000000),
        ("max_file_bytes", 100, 2000000),
    ]:
        number(
            value[name],
            name,
            low,
            high,
            integer=name
            in {"no_progress_rounds", "max_paths", "max_bytes", "max_file_bytes"},
        )
    if value["max_debounce_seconds"] < value["debounce_seconds"]:
        raise ValueError("Maximum debounce precedes the debounce window.")
    for name in ("model_profile", "review_model"):
        if value[name] is not None and (
            not isinstance(value[name], str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value[name])
        ):
            raise ValueError("Communication models require host aliases.")
    for name in ("review_globs", "required_tests"):
        if (
            not isinstance(value[name], list)
            or len(value[name]) > 32
            or any(not isinstance(v, str) or not v for v in value[name])
        ):
            raise ValueError("Invalid review scope or tests.")
    if type(value["require_tests_for_approval"]) is not bool:
        raise ValueError("Invalid approval test policy.")
    if value["credentials_file"] is not None:
        path = Path(value["credentials_file"]).expanduser()
        if not path.is_absolute():
            raise ValueError(
                "Communication credentials require an absolute external path."
            )
        value["credentials_file"] = str(path)
    if value["mode"] == "remote" and not value["credentials_file"]:
        raise ValueError("Remote communication requires dedicated credentials.")
    return json.loads(json.dumps(value, allow_nan=False))


def open_projects(spec, environment, roots):
    if spec["credentials_file"]:
        path = Path(spec["credentials_file"])
        if (
            path.is_symlink()
            or any(p.is_symlink() for p in path.parents)
            or any(
                path.resolve().is_relative_to(Path(root).resolve()) for root in roots
            )
        ):
            raise ValueError(
                "Publishing credentials belong outside control and code directories."
            )
        values = read_environment(
            path, roots[0], private=True, inherit_environment=False
        )
    else:
        values = dict(environment)
    result = {}
    try:
        from homebrew_harness.platforms.registry import PROVIDERS

        for declaration in spec["projects"]:
            token = values.get(declaration["token_env"])
            if spec["mode"] == "remote" and any(
                token == v for v in environment.values()
            ):
                raise ValueError(
                    "Communication write credentials appear in the model environment."
                )
            endpoint = values.get(declaration["api_url_env"])
            if spec["mode"] == "local":
                host = urlsplit(endpoint or "").hostname
                try:
                    loopback = ipaddress.ip_address(host).is_loopback
                except ValueError:
                    loopback = host == "localhost"
                if not loopback or token != "synthetic":
                    raise ValueError(
                        "Local communication accepts only synthetic loopback peers."
                    )
            transport, identity = connection(declaration, values, roots)
            try:
                adapter = PROVIDERS[declaration["provider"]]["communication"](
                    declaration, transport
                )
            except BaseException:
                transport.close()
                raise
            result[
                (
                    declaration["provider"],
                    declaration["instance"],
                    declaration["project"],
                )
            ] = (adapter, identity)
    except BaseException:
        for adapter, _identity in result.values():
            adapter.close()
        raise
    return result


def validate_proposal(raw, policy):
    allowed = {
        "next_step",
        "reason",
        "alignment",
        "facts",
        "questions",
        "draft",
        "findings",
        "communication",
        "progress",
        "requirements_changed",
        "category",
    }
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise ValueError("Invalid discussion proposal fields.")
    if raw.get("category", "feature") not in {
        "bug",
        "feature",
        "documentation",
        "maintenance",
        "question",
    }:
        raise ValueError("Invalid discussion category.")
    if raw.get("next_step") not in NEXT:
        raise ValueError("Unknown discussion next step.")
    for name in ("reason", "alignment"):
        text(raw.get(name), name, 8000)
    if (
        type(raw.get("progress", True)) is not bool
        or type(raw.get("requirements_changed", False)) is not bool
    ):
        raise ValueError("Discussion flags must be Boolean.")
    for name, limit in [("facts", 32), ("questions", 16), ("findings", 32)]:
        values = raw.get(name, [])
        if not isinstance(values, list) or len(values) > limit:
            raise ValueError("Discussion projection exceeds its bounds.")
        ids = set()
        for entry in values:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", entry["id"])
                or entry["id"] in ids
            ):
                raise ValueError(
                    "Projection entries require unique stable identifiers."
                )
            allowed = (
                {"id", "text", "sources", "status", "path", "severity"}
                if name == "findings"
                else {"id", "text", "sources", "status"}
            )
            if set(entry) - allowed:
                raise ValueError("Unknown discussion projection fields.")
            ids.add(entry["id"])
            text(entry.get("text"), "projection text", 8000)
            if (
                not isinstance(entry.get("sources", []), list)
                or len(entry.get("sources", [])) > 32
                or any(not isinstance(s, str) for s in entry.get("sources", []))
            ):
                raise ValueError("Invalid discussion evidence references.")
            if name == "findings" and entry.get("status", "open") not in {
                "open",
                "needs_explanation",
                "resolved",
                "withdrawn",
                "superseded",
            }:
                raise ValueError("Invalid finding status.")
    if raw.get("draft") is not None:
        draft = raw["draft"]
        if not isinstance(draft, dict) or set(draft) - {
            "text",
            "scope",
            "acceptance",
            "out_of_scope",
        }:
            raise ValueError("Invalid requirement draft.")
        text(draft.get("text"), "draft", 16000)
        text(draft.get("acceptance"), "acceptance", 8000)
        if "out_of_scope" in draft:
            text(draft["out_of_scope"], "out_of_scope", 8000)
        if draft.get("scope") is not None and draft["scope"] not in policy["scopes"]:
            raise ValueError("Draft must choose a host scope.")
    if raw.get("communication") is not None:
        validate_plan(raw["communication"])
    if len(json.dumps(raw, allow_nan=False)) > 96000:
        raise ValueError("Discussion proposal exceeds its size limit.")
    return raw


def validate_plan(plan):
    if not isinstance(plan, dict) or set(plan) - {
        "kind",
        "intent",
        "body",
        "thread_id",
        "event",
        "comments",
    }:
        raise ValueError("Invalid communication plan.")
    if plan.get("kind") not in ACTIONS or plan.get("intent") not in INTENTS:
        raise ValueError("Unknown publication kind or intent.")
    text(plan.get("body"), "body", 16000)
    if plan["kind"] == "reply_thread":
        text(plan.get("thread_id"), "thread", 200)
    if plan["kind"] == "submit_review":
        if plan.get("event") not in {"COMMENT", "REQUEST_CHANGES", "APPROVE"}:
            raise ValueError("Invalid review event.")
        intent = {
            "COMMENT": "review_feedback",
            "REQUEST_CHANGES": "request_changes",
            "APPROVE": "approve_review",
        }[plan["event"]]
        if plan["intent"] != intent:
            raise ValueError("Review event and intent disagree.")
    comments = plan.get("comments", [])
    if not isinstance(comments, list) or len(comments) > 32:
        raise ValueError("Inline review exceeds its bounds.")
    for comment in comments:
        if not isinstance(comment, dict) or set(comment) - {
            "path",
            "line",
            "side",
            "body",
        }:
            raise ValueError("Invalid inline review fields.")
        text(comment.get("path"), "path", 1000)
        text(comment.get("body"), "inline body", 8000)
        number(comment.get("line"), "line", 1, 10000000, integer=True)
        if comment.get("side") not in {"LEFT", "RIGHT"}:
            raise ValueError("Invalid diff side.")
    if comments and plan["kind"] != "submit_review":
        raise ValueError("Inline comments require a review submission.")
    return plan
