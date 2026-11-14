"""Host declarations; transport values and write credentials remain external."""

from homebrew_harness.runtime.errors import boundary_error
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
        "assessment_model": None,
        "prompt_files": {},
        "review_globs": ["**/*"],
        "max_paths": 10000,
        "max_bytes": 32000000,
        "max_file_bytes": 1000000,
        "required_tests": [],
        "require_tests_for_approval": False,
    }
    if not isinstance(raw, dict) or set(raw) - set(defaults):
        raise boundary_error(ValueError, "communication_contract", "Invalid communication policy fields.")
    value = {**defaults, **raw}
    files = value["prompt_files"]
    if (not isinstance(files, dict) or set(files) - {"assessment", "draft"}
            or any(not isinstance(path, str) or not Path(path).expanduser().is_absolute()
                   for path in files.values())):
        raise boundary_error(ValueError, "communication_contract", "Discussion prompts require known phases and external absolute paths.")
    value["prompt_files"] = {name: str(Path(path).expanduser()) for name, path in files.items()}
    if value["mode"] not in {"remote", "local"}:
        raise boundary_error(ValueError, "communication_contract", "Unknown communication mode.")
    if not isinstance(value["projects"], list) or not 1 <= len(value["projects"]) <= 8:
        raise boundary_error(ValueError, "communication_contract", "Communication requires bounded host project declarations.")
    declarations = []
    for project in value["projects"]:
        declaration = validate_declaration(project, "communication", named=True)
        if not any(
            all(
                declaration[k] == source[k] for k in ("provider", "instance", "project")
            )
            for source in sources
        ):
            raise boundary_error(ValueError, "communication_contract",
                "Communication project must have a registered repository source."
            )
        declarations.append(declaration)
    if len({(p["provider"], p["instance"], p["project"]) for p in declarations}) != len(
        declarations
    ):
        raise boundary_error(ValueError, "communication_contract", "Duplicate communication project.")
    value["projects"] = declarations
    for name, choices in (("enabled_actions", ACTIONS), ("auto_intents", AUTOMATIC)):
        if (
            not isinstance(value[name], list)
            or any(not isinstance(v, str) for v in value[name])
            or set(value[name]) - choices
        ):
            raise boundary_error(ValueError, "communication_contract", "Invalid communication actions or automatic intents.")
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
        raise boundary_error(ValueError, "communication_contract", "Maximum debounce precedes the debounce window.")
    for name in ("model_profile", "review_model", "assessment_model"):
        if value[name] is not None and (
            not isinstance(value[name], str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value[name])
        ):
            raise boundary_error(ValueError, "communication_contract", "Communication models require host aliases.")
    for name in ("review_globs", "required_tests"):
        if (
            not isinstance(value[name], list)
            or len(value[name]) > 32
            or any(not isinstance(v, str) or not v for v in value[name])
        ):
            raise boundary_error(ValueError, "communication_contract", "Invalid review scope or tests.")
    if type(value["require_tests_for_approval"]) is not bool:
        raise boundary_error(ValueError, "communication_contract", "Invalid approval test policy.")
    if value["credentials_file"] is not None:
        path = Path(value["credentials_file"]).expanduser()
        if not path.is_absolute():
            raise boundary_error(ValueError, "communication_contract",
                "Communication credentials require an absolute external path."
            )
        value["credentials_file"] = str(path)
    if value["mode"] == "remote" and not value["credentials_file"]:
        raise boundary_error(ValueError, "communication_contract", "Remote communication requires dedicated credentials.")
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
            raise boundary_error(ValueError, "communication_contract",
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
                raise boundary_error(ValueError, "communication_contract",
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
                    raise boundary_error(ValueError, "communication_contract",
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
        "reply_brief",
    }
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise boundary_error(ValueError, "communication_contract", "Invalid discussion proposal fields.")
    if raw.get("category", "feature") not in {
        "bug",
        "feature",
        "documentation",
        "maintenance",
        "question",
    }:
        raise boundary_error(ValueError, "communication_contract", "Invalid discussion category.")
    if raw.get("next_step") not in NEXT:
        raise boundary_error(ValueError, "communication_contract", "Unknown discussion next step.")
    for name in ("reason", "alignment"):
        text(raw.get(name), name, 8000)
    if (
        type(raw.get("progress", True)) is not bool
        or type(raw.get("requirements_changed", False)) is not bool
    ):
        raise boundary_error(ValueError, "communication_contract", "Discussion flags must be Boolean.")
    for name, limit in [("facts", 32), ("questions", 16), ("findings", 32)]:
        values = raw.get(name, [])
        if not isinstance(values, list) or len(values) > limit:
            raise boundary_error(ValueError, "communication_contract", "Discussion projection exceeds its bounds.")
        ids = set()
        for entry in values:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", entry["id"])
                or entry["id"] in ids
            ):
                raise boundary_error(ValueError, "communication_contract",
                    "Projection entries require unique stable identifiers."
                )
            allowed = (
                {"id", "text", "sources", "status", "path", "severity"}
                if name == "findings"
                else ({"id", "text", "sources", "status", "recipient"}
                      if name == "questions" else {"id", "text", "sources", "status"})
            )
            if set(entry) - allowed:
                raise boundary_error(ValueError, "communication_contract", "Unknown discussion projection fields.")
            ids.add(entry["id"])
            text(entry.get("text"), "projection text", 8000)
            if (
                not isinstance(entry.get("sources", []), list)
                or len(entry.get("sources", [])) > 32
                or any(not isinstance(s, str) for s in entry.get("sources", []))
            ):
                raise boundary_error(ValueError, "communication_contract", "Invalid discussion evidence references.")
            if name == "questions" and entry.get("recipient", "requester") not in {"requester", "maintainer"}:
                raise boundary_error(ValueError, "communication_contract", "Question recipient must be requester or maintainer.")
            if name in {"questions", "findings"} and entry.get("status", "open") not in {
                "open",
                "needs_explanation",
                "resolved",
                "withdrawn",
                "superseded",
            }:
                raise boundary_error(ValueError, "communication_contract", "Invalid finding status.")
    if raw.get("draft") is not None:
        draft = raw["draft"]
        if not isinstance(draft, dict) or set(draft) - {
            "text",
            "scope",
            "acceptance",
            "out_of_scope",
        }:
            raise boundary_error(ValueError, "communication_contract", "Invalid requirement draft.")
        text(draft.get("text"), "draft", 16000)
        text(draft.get("acceptance"), "acceptance", 8000)
        if "out_of_scope" in draft:
            text(draft["out_of_scope"], "out_of_scope", 8000)
        if draft.get("scope") is not None and draft["scope"] not in policy["scopes"]:
            raise boundary_error(ValueError, "communication_contract", "Draft must choose a host scope.")
    if "reply_brief" in raw:
        brief = raw["reply_brief"]
        if not isinstance(brief, dict) or set(brief) - {"scope", "next_step", "verification"}:
            raise boundary_error(ValueError, "communication_contract", "Invalid public reply brief.")
        text(brief.get("scope"), "reply brief scope", 2000)
        text(brief.get("next_step"), "reply brief next step", 1000)
        if "verification" in brief:
            text(brief["verification"], "reply brief verification", 1000)
    internal = open_questions(raw, "maintainer")
    external = open_questions(raw, "requester")
    if internal and not external and raw["next_step"] != "operator":
        raise boundary_error(ValueError, "communication_contract", "Only maintainer questions remain; use operator instead of public clarification.")
    if raw.get("communication") is not None:
        plan = validate_plan(raw["communication"])
        validate_public_questions(plan, raw)
    if len(json.dumps(raw, allow_nan=False)) > 96000:
        raise boundary_error(ValueError, "communication_contract", "Discussion proposal exceeds its size limit.")
    return raw


def open_questions(proposal, recipient):
    return [q for q in proposal.get("questions", [])
            if q.get("recipient", "requester") == recipient
            and q.get("status", "open") in {"open", "needs_explanation"}]


def validate_public_questions(plan, projection):
    internal = open_questions(projection, "maintainer")
    external = open_questions(projection, "requester")
    allowed = {q["id"] for q in external}
    if any(identity not in allowed for identity in plan.get("question_ids", [])):
        raise boundary_error(ValueError, "communication_contract", "Public questions must reference current open requester questions.")
    if internal and not external and plan["intent"] == "clarify":
        raise boundary_error(ValueError, "communication_contract", "Maintainer decisions cannot be requested through public clarification.")


def validate_plan(plan):
    if not isinstance(plan, dict) or set(plan) - {
        "kind",
        "intent",
        "body",
        "thread_id",
        "event",
        "comments",
        "question_ids",
    }:
        raise boundary_error(ValueError, "communication_contract", "Invalid communication plan.")
    if plan.get("kind") not in ACTIONS or plan.get("intent") not in INTENTS:
        raise boundary_error(ValueError, "communication_contract", "Unknown publication kind or intent.")
    ids = plan.get("question_ids", [])
    if (not isinstance(ids, list) or len(ids) > 16
            or any(not isinstance(identity, str) for identity in ids)
            or len(set(ids)) != len(ids)):
        raise boundary_error(ValueError, "communication_contract", "Public question IDs must be a bounded unique list.")
    text(plan.get("body"), "body", 16000)
    if plan["kind"] == "reply_thread":
        text(plan.get("thread_id"), "thread", 200)
    if plan["kind"] == "submit_review":
        if plan.get("event") not in {"COMMENT", "REQUEST_CHANGES", "APPROVE"}:
            raise boundary_error(ValueError, "communication_contract", "Invalid review event.")
        intent = {
            "COMMENT": "review_feedback",
            "REQUEST_CHANGES": "request_changes",
            "APPROVE": "approve_review",
        }[plan["event"]]
        if plan["intent"] != intent:
            raise boundary_error(ValueError, "communication_contract", "Review event and intent disagree.")
    comments = plan.get("comments", [])
    if not isinstance(comments, list) or len(comments) > 32:
        raise boundary_error(ValueError, "communication_contract", "Inline review exceeds its bounds.")
    for comment in comments:
        if not isinstance(comment, dict) or set(comment) - {
            "path",
            "line",
            "side",
            "body",
        }:
            raise boundary_error(ValueError, "communication_contract", "Invalid inline review fields.")
        text(comment.get("path"), "path", 1000)
        text(comment.get("body"), "inline body", 8000)
        number(comment.get("line"), "line", 1, 10000000, integer=True)
        if comment.get("side") not in {"LEFT", "RIGHT"}:
            raise boundary_error(ValueError, "communication_contract", "Invalid diff side.")
    if comments and plan["kind"] != "submit_review":
        raise boundary_error(ValueError, "communication_contract", "Inline comments require a review submission.")
    return plan
