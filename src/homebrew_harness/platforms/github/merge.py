"""GitHub native merge operations and conservative server-side rule evidence."""

from homebrew_harness.platforms.contracts import ObjectRef, digest
from homebrew_harness.platforms.http import RemoteFailure
from homebrew_harness.platforms.merges import commit, receipt, validate_plan


def metadata(adapter, subject):
    ref = ObjectRef(**subject)
    if (
        ref.kind != "change_request"
        or ref.number is None
        or ref.scope()
        != (adapter.spec["provider"], adapter.spec["instance"], adapter.spec["project"])
    ):
        raise ValueError("Merge observation is outside delivery scope.")
    row = adapter.request("GET", adapter.prefix + "/pulls/" + str(ref.number))
    head, base = row.get("head") or {}, row.get("base") or {}
    source = (head.get("repo") or {}).get("full_name")
    target = (base.get("repo") or {}).get("full_name")
    if source != adapter.spec["project"] or target != adapter.spec["project"]:
        raise ValueError("PR source or target project changed.")
    if (
        type(row.get("merged")) is not bool
        or row.get("state") not in {"open", "closed"}
        or row["merged"]
        and row["state"] != "closed"
    ):
        raise ValueError("Invalid native PR state.")
    value = {
        "head": commit(head.get("sha")),
        "base": commit(base.get("sha")),
        "source_project": source,
        "source_branch": head.get("ref"),
        "base_project": target,
        "base_branch": base.get("ref"),
        "state": row["state"],
        "draft": row.get("draft") is True,
        "mergeable": row.get("mergeable"),
        "mergeable_state": row.get("mergeable_state"),
        "merged": row["merged"],
        "result_commit": commit(row.get("merge_commit_sha")) if row["merged"] else None,
        "merged_at": row.get("merged_at"),
        "merged_by": (row.get("merged_by") or {}).get("id"),
    }
    return value


def rule_evidence(adapter, method):
    spec = adapter.spec
    repository = adapter.request("GET", adapter.prefix)
    allowed = (
        repository.get(
            {
                "merge": "allow_merge_commit",
                "squash": "allow_squash_merge",
                "rebase": "allow_rebase_merge",
            }[method]
        )
        is True
    )
    rules = adapter.request(
        "GET", adapter.prefix + "/rules/branches/" + spec["target_branch"]
    )
    if not isinstance(rules, list) or len(rules) > 100:
        raise ValueError("Incomplete active branch rule evidence.")
    summaries = []
    for rid in {r.get("ruleset_id") for r in rules}:
        if type(rid) is not int:
            raise ValueError("Active rule lacks a ruleset identity.")
        rule = next(r for r in rules if r["ruleset_id"] == rid)
        source = rule.get("ruleset_source_type")
        if source == "Repository":
            path = adapter.prefix + "/rulesets/" + str(rid)
        elif (
            source == "Organization"
            and rule.get("ruleset_source") == spec["project"].split("/")[0]
        ):
            path = "orgs/" + rule["ruleset_source"] + "/rulesets/" + str(rid)
        else:
            raise ValueError("Unsupported active rule source.")
        details = adapter.request("GET", path)
        if details.get("enforcement") != "active" or details.get("bypass_actors"):
            raise ValueError("Native merge rules allow bypass or are not active.")
        actual = {
            (r["type"], digest(r.get("parameters") or {}))
            for r in details.get("rules", [])
        }
        if not all(
            (r["type"], digest(r.get("parameters") or {})) in actual
            for r in rules
            if r["ruleset_id"] == rid
        ):
            raise ValueError("Active rules changed during rule evidence collection.")
        summaries.append({"id": rid, "rules": details.get("rules", [])})
    if any(r.get("type") == "merge_queue" for r in rules):
        raise ValueError("Native queue execution is not enabled.")
    required = set(spec["required_checks"])
    rules_ready = False
    types = {r.get("type") for r in rules}
    for rule in rules:
        parameters = rule.get("parameters") or {}
        if rule.get("type") == "required_status_checks":
            names = {
                r.get("context") for r in parameters.get("required_status_checks", [])
            }
            rules_ready |= (
                bool(required)
                and required <= names
                and parameters.get("strict_required_status_checks_policy") is True
                and {"pull_request", "non_fast_forward"} <= types
            )
    protection = None
    try:
        protection = adapter.request(
            "GET", adapter.prefix + "/branches/" + spec["target_branch"] + "/protection"
        )
    except RemoteFailure as error:
        # A 404 cannot prove absence. A complete, non-bypassable active ruleset
        # can independently supply the required guarantee.
        if error.status != 404 or not rules_ready:
            raise
    legacy_ready = False
    if protection is not None:
        checks = protection.get("required_status_checks") or {}
        pull = protection.get("required_pull_request_reviews")
        bypass = (pull or {}).get("bypass_pull_request_allowances") or {}
        names = set(checks.get("contexts") or []) | {
            c.get("context") for c in checks.get("checks") or []
        }
        legacy_ready = (
            bool(required)
            and required <= names
            and checks.get("strict") is True
            and isinstance(pull, dict)
            and not any(bypass.get(k) for k in ("users", "teams", "apps"))
            and protection.get("enforce_admins", {}).get("enabled") is True
            and not protection.get("allow_force_pushes", {}).get("enabled", False)
        )
    value = {
        "allowed": allowed and (rules_ready or legacy_ready),
        "method": method,
        "rules": rules,
        "rulesets": summaries,
        "protection": protection,
    }
    value["digest"] = digest(value)
    if not value["allowed"]:
        raise ValueError(
            "Native merge requires enforced PR and current-base CI without bypass."
        )
    return value


def merge(adapter, plan):
    ref = validate_plan(plan, adapter.spec)
    if any(adapter.closing.search(plan.get(k, "")) for k in ("title", "body")):
        raise ValueError("Merge text cannot authorize implicit issue closing.")
    observed = metadata(adapter, ref.data())
    if (
        observed["head"] != plan["commit"]
        or observed["base_branch"] != adapter.spec["target_branch"]
        or observed["source_branch"] != plan["branch"]
    ):
        raise ValueError("Native merge candidate changed.")
    if observed["merged"]:
        return receipt(ref.data(), observed, plan["commit"], method=plan["method"])
    if (
        observed["state"] != "open"
        or observed["draft"]
        or observed["mergeable"] is not True
        or observed["mergeable_state"]
        in {"blocked", "behind", "dirty", "draft", "unknown"}
    ):
        raise ValueError("PR is not ready for native merge.")
    rule_evidence(adapter, plan["method"])
    result = adapter.request(
        "PUT",
        adapter.prefix + "/pulls/" + str(ref.number) + "/merge",
        {
            "sha": plan["commit"],
            "merge_method": plan["method"],
            "commit_title": plan["title"],
            "commit_message": plan["body"],
        },
    )
    if result.get("merged") is not True:
        # No final effect is established by a generic success response.
        raise RuntimeError("Merge response does not confirm completion.")
    result_commit = commit(result.get("sha"))
    current = metadata(adapter, ref.data())
    if not current["merged"] or current["result_commit"] != result_commit:
        raise RuntimeError("Native merge result is not yet confirmed.")
    return receipt(ref.data(), current, plan["commit"], method=plan["method"])
