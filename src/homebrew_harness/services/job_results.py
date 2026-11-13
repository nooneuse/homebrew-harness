"""Candidate validation and short transaction application for service jobs."""

import time

from .maintenance_spec import CATEGORIES, DISPOSITIONS, number, text


def triage(store, job, raw, conn=None):
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
        and raw.get("scope") not in store.policy["scopes"]
    ):
        raise ValueError("Select a host execution scope before requesting work.")
    if raw.get("scope") is not None and raw["scope"] not in store.policy["scopes"]:
        raise ValueError("Unknown scope.")
    return raw


def discussion(store, job, raw, conn=None):
    from .communication.spec import validate_proposal

    result = validate_proposal(raw, store.policy)
    if store.communication is not None:
        store.communication.validate_evidence(job, result, conn)
    return result


def review(store, job, raw, conn=None):
    if (
        not isinstance(raw, dict)
        or not {"text", "findings"} <= set(raw)
        or set(raw) - {"text", "findings", "publication"}
        or not isinstance(raw["findings"], list)
        or len(raw["findings"]) > 32
    ):
        raise ValueError("Invalid independent review proposal.")
    text(raw["text"], maximum=16000)
    if "publication" in raw:
        from homebrew_harness.platforms.merges import CLOSING

        publication = raw["publication"]
        if (
            job["kind"] != "review"
            or not isinstance(publication, dict)
            or set(publication) != {"title", "body"}
        ):
            raise ValueError("Only a delivery review can propose PR publication text.")
        text(publication["title"], "PR title", 200)
        text(publication["body"], "PR body", 16000)
        if CLOSING.search(publication["title"]) or CLOSING.search(publication["body"]):
            raise ValueError("PR text cannot implicitly close issues.")
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


def ci_triage(store, job, raw, conn=None):
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


def conversation(store, job, raw, conn=None):
    if not isinstance(raw, dict) or set(raw) - {"text", "work_id", "plan"}:
        raise ValueError("A conversation expects text and optional work_id/plan.")
    text(raw.get("text"), maximum=32000)
    message = store.get("messages", job["target"], conn)
    if "work_id" in raw:
        text(raw["work_id"], "work_id", 100)
        if raw["work_id"] != message.get("work_id"):
            raise ValueError("Conversation plan must target its explicitly bound Work.")
    if raw.get("plan") is not None:
        triage(store, job, raw["plan"], conn)
        if raw["plan"]["disposition"] not in {"accept", "decide"}:
            raise ValueError(
                "Conversation execution proposals need an acceptance decision."
            )
        if not raw.get("work_id") or raw["work_id"] != message.get("work_id"):
            raise ValueError("Conversation plan must target its explicitly bound Work.")
        work = store.get("work", raw["work_id"], conn)
        if message["work_revision"] != work["requirements_revision"]:
            raise ValueError("Conversation plan target changed.")
    return raw


def report(store, job, raw, conn=None):
    return _text_only(raw, 32000)


def assessment(store, job, raw, conn=None):
    return _text_only(raw, 16000)


def _text_only(raw, maximum):
    if not isinstance(raw, dict) or set(raw) != {"text"}:
        raise ValueError("This job expects text only.")
    text(raw["text"], maximum=maximum)
    return raw


def apply_triage(store, conn, job, result):
    work = store.get("work", job["target"], conn)
    if work["requirements_revision"] != job["revision"] or work["cancel_requested"]:
        return "stale"
    work.update(
        triage=result,
        triage_revision=job["revision"],
        revision=work["revision"] + 1,
        priority=result.get("priority", 0),
    )
    disposition = result["disposition"]
    active = work["run_id"] is not None and work["state"] == "in_progress"
    if disposition == "merge":
        related = store.get("work", result.get("related_work"), conn)
        if related["id"] == work["id"]:
            raise ValueError("Cannot merge a work item with itself.")
    if not active:
        work["state"] = {
            "accept": "proposed",
            "decide": "proposed",
            "needs_info": "needs_info",
            "defer": "deferred",
            "reject": "rejected",
            "merge": "merged",
        }[disposition]
    store.db._put(conn, "service_work", work["id"], work)
    if disposition in {"accept", "decide", "needs_info"}:
        if (
            disposition == "accept"
            and result["category"] in store.policy["auto_categories"]
            and not active
            and not work.get("pause_requested")
            and not work.get("cancel_requested")
            and not work.get("run_history")
            and not work["run_id"]
        ):
            store.grant(conn, work, result, "host_policy")
        else:
            store.work_request(conn, work, result)
    store.changed(conn, work["id"])
    return "completed"


def apply_discussion(store, conn, job, result):
    return store.communication.finish(conn, job, result)


def apply_delivery(store, conn, job, result):
    return store.delivery.store.finish_model(conn, job, result)


def apply_audit(store, conn, job, result):
    from .operations.audit import finish

    return finish(store, conn, job, result)


def apply_assessment(store, conn, job, result):
    from .job_contracts import HANDLERS

    section = HANDLERS[job["kind"]].section
    target = store.get(section, job["target"], conn)
    if target["revision"] != job["revision"]:
        return "stale"
    target.update(assessment=result, revision=target["revision"] + 1)
    store.put(conn, section, target)
    store.changed(conn, detail={"assessment": job["target"]})
    return "completed"


def apply_conversation(store, conn, job, result):
    message = store.get("messages", job["target"], conn)
    message.update(state="answered", result=result)
    store.put(conn, "messages", message)
    if result.get("plan"):
        work = store.get("work", result["work_id"], conn)
        store.work_request(conn, work, result["plan"])
        store.changed(conn, work["id"])
    return "completed"


def apply_report(store, conn, job, result):
    report = store.get("reports", job["target"], conn)
    report.update(state="ready", text=result["text"])
    store.put(conn, "reports", report)
    meta = store.store.guard(conn, store.header)
    meta.update(
        report_activity=max(meta.get("report_activity", 0), report["activity"]),
        report_next=time.time()
        + (0 if report.get("more_activity") else store.policy["report_seconds"]),
    )
    store.db._put(conn, "service_meta", store.id, meta)
    return "completed"
