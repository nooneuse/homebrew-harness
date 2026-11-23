from homebrew_harness.runtime.limits import current_limits
"""Candidate validation and short transaction application for service jobs."""

from homebrew_harness.runtime.errors import boundary_error
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
        raise boundary_error(ValueError, "service_result_invalid", "Invalid triage proposal.")
    text(raw.get("reason"), "reason", 8000)
    text(raw.get("plan"), "plan", 16000)
    number(raw.get("priority", 0), "priority", 0, 100, integer=True)
    if (
        raw["disposition"] in {"accept", "decide"}
        and raw.get("scope") not in store.policy["scopes"]
    ):
        raise boundary_error(ValueError, "service_result_invalid", "Select a host execution scope before requesting work.")
    if raw.get("scope") is not None and raw["scope"] not in store.policy["scopes"]:
        raise boundary_error(ValueError, "service_result_invalid", "Unknown scope.")
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
        or (current_limits().communication_findings is not None and len(raw["findings"]) > current_limits().communication_findings)
    ):
        raise boundary_error(ValueError, "service_result_invalid", "Invalid independent review proposal.")
    text(raw["text"], maximum=16000)
    if "publication" in raw:
        from homebrew_harness.platforms.merges import CLOSING

        publication = raw["publication"]
        if (
            job["kind"] != "review"
            or not isinstance(publication, dict)
            or set(publication) != {"title", "body"}
        ):
            raise boundary_error(ValueError, "service_result_invalid", "Only a delivery review can propose PR publication text.")
        text(publication["title"], "PR title", 200)
        text(publication["body"], "PR body", 16000)
        if CLOSING.search(publication["title"]) or CLOSING.search(publication["body"]):
            raise boundary_error(ValueError, "service_result_invalid", "PR text cannot implicitly close issues.")
    for item in raw["findings"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"severity", "path", "message"}
            or item["severity"] not in {"blocking", "warning"}
        ):
            raise boundary_error(ValueError, "service_result_invalid", "Invalid review finding.")
        text(item["path"], maximum=1000)
        text(item["message"], maximum=4000)
    return raw


def publication(store, job, raw, conn=None):
    """Public wording is a proposal; neither this result nor a review publishes it."""
    from homebrew_harness.platforms.merges import CLOSING

    if isinstance(raw, dict) and set(raw) == {"human_required", "reason"} and raw["human_required"] is True:
        text(raw["reason"], "reason", 8000)
        return raw
    if not isinstance(raw, dict) or set(raw) != {"title", "body"}:
        raise boundary_error(ValueError, "service_result_invalid", "Publication needs title/body or an explicit human_required reason.")
    text(raw["title"], "PR title", 200)
    text(raw["body"], "PR body", 16000)
    if CLOSING.search(raw["title"]) or CLOSING.search(raw["body"]):
        raise boundary_error(ValueError, "service_result_invalid", "PR text cannot implicitly close issues.")
    return raw


def validation_plan(store, job, raw, conn=None):
    if not isinstance(raw, dict) or set(raw) - {"tests", "selections", "review", "reason", "draft"}:
        raise ValueError("Validation plan requires tests, review and a reason.")
    text(raw.get("reason"), maximum=8000)
    if 'draft' in raw:
        if not isinstance(raw['draft'],dict) or set(raw['draft'])!={'title','body'}:raise ValueError('Draft publication requires title/body.')
        text(raw['draft']['title']);text(raw['draft']['body'])
    if type(raw.get("review")) is not bool or not isinstance(raw.get("tests"), list):
        raise ValueError("Validation tests must be a list and review Boolean.")
    row = store.get("changesets", job["target"], conn)
    work = store.store.work(row["work_id"], store.id)
    options = store.delivery.validation_options(work)
    if any(not isinstance(name, str) for name in raw["tests"]) or set(raw["tests"]) - set(options["available_tests"]):
        raise ValueError("Choose only the Work's available and authorized test recipes.")
    if set(options["required_tests"]) - set(raw["tests"]) or options["required_review"] and not raw["review"]:
        raise ValueError("Explicit operator acceptance cannot be skipped; select the required tests/review.")
    selections = raw.get("selections", {})
    if not isinstance(selections, dict) or set(selections) - set(raw["tests"]):
        raise ValueError("Selections must belong to chosen tests.")
    if any(name in selections and selections[name] is not None for name in options["required_tests"]):
        raise ValueError("Required full-recipe acceptance cannot be replaced by a selection.")
    if len(raw["tests"]) != len(set(raw["tests"])):
        raise ValueError("Validation tests must be unique.")
    from homebrew_harness.execution.test_selection import validate_selection
    for name, selection in selections.items():
        registered = options["selections"][name]
        validate_selection(selection, registered["framework"], registered["roots"])
    return {**raw, "selections": selections}


def ci_triage(store, job, raw, conn=None):
    if (
        not isinstance(raw, dict)
        or not {"classification", "reason", "plan", "next_action"} <= set(raw)
        or set(raw) - {"classification", "reason", "plan", "next_action", "wait_seconds", "source_ref", "integration"}
        or raw["classification"]
        not in {"code", "baseline", "flaky", "infrastructure", "unknown"}
    ):
        raise boundary_error(ValueError, "service_result_invalid", "Invalid CI diagnosis.")
    text(raw["reason"], maximum=8000)
    text(raw["plan"], maximum=16000)
    if raw.get("next_action") not in {"repair", "recheck", "reobserve", "wait", "defer", "stop", "human_required", "reconsider_review", "plan_validation", "proceed", "retry_prepare", "assess_integration"}:
        raise boundary_error(ValueError, "service_result_invalid", "Unknown CI next_action.")
    if raw["next_action"] == "assess_integration":
        from .delivery.periodic import ci_integration_eligible

        row = store.get("changesets", job["target"], conn)
        if raw["classification"] != "baseline" or not ci_integration_eligible(store.delivery.spec, row):
            raise boundary_error(ValueError, "service_result_invalid", "Early integration needs a baseline CI diagnosis on a published periodic PR.")
    if "wait_seconds" in raw:
        from homebrew_harness.runtime.limits import positive
        positive(raw["wait_seconds"], "CI wait_seconds")
    if ("source_ref" in raw or "integration" in raw) and store.delivery is not None and store.delivery.spec.get("base_update") == "periodic":
        row = store.get("changesets", job["target"], conn)
        if ((row.get("integration_schedule") or {}).get("state") == "integrating"
                and (row.get("ci") or {}).get("kind") == "candidate_merge_conflict"):
            raise boundary_error(ValueError, "service_result_invalid", "Resolve this conflict on the selected candidate and target; do not select a new source.")
    if 'source_ref' in raw:
        if not isinstance(raw['source_ref'],str):raise ValueError('source_ref must be text.')
        from .work_actions import validate_action
        choice={'mode':'fork','source_ref':raw['source_ref'],'reason':raw['plan']}
        if 'integration' in raw:choice['integration']=raw['integration']
        validate_action(choice)
    return raw


def integration_assessment(store, job, raw, conn=None):
    if (
        not isinstance(raw, dict)
        or not {"action", "reason"} <= raw.keys()
        or raw.keys() - {"action", "reason", "defer_seconds"}
        or raw["action"] not in ("integrate", "defer", "blocked")
    ):
        raise boundary_error(
            ValueError, "service_result_invalid", "Invalid integration assessment."
        )
    text(raw["reason"], "reason", 8000)
    if raw["action"] == "defer":
        if job["input"].get("overdue"):
            raise boundary_error(
                ValueError, "service_result_invalid",
                "Integration assessment is overdue; choose integrate or blocked.",
            )
        if "defer_seconds" in raw:
            from homebrew_harness.runtime.limits import positive

            try:
                positive(raw["defer_seconds"], "defer_seconds")
            except (ValueError, OverflowError) as error:
                raise boundary_error(
                    ValueError, "service_result_invalid",
                    "defer_seconds must be a finite positive number.",
                ) from error
    elif "defer_seconds" in raw:
        raise boundary_error(
            ValueError, "service_result_invalid",
            "Only a defer action can include defer_seconds.",
        )
    return raw


def conversation(store, job, raw, conn=None):
    if not isinstance(raw, dict) or set(raw) - {"text", "work_id", "plan"}:
        raise boundary_error(ValueError, "service_result_invalid", "A conversation expects text and optional work_id/plan.")
    text(raw.get("text"), maximum=32000)
    message = store.get("messages", job["target"], conn)
    if "work_id" in raw:
        text(raw["work_id"], "work_id", 100)
        if raw["work_id"] != message.get("work_id"):
            raise boundary_error(ValueError, "service_result_invalid", "Conversation plan must target its explicitly bound Work.")
    if raw.get("plan") is not None:
        triage(store, job, raw["plan"], conn)
        if raw["plan"]["disposition"] not in {"accept", "decide"}:
            raise boundary_error(ValueError, "service_result_invalid",
                "Conversation execution proposals need an acceptance decision."
            )
        if not raw.get("work_id") or raw["work_id"] != message.get("work_id"):
            raise boundary_error(ValueError, "service_result_invalid", "Conversation plan must target its explicitly bound Work.")
        work = store.get("work", raw["work_id"], conn)
        if message["work_revision"] != work["requirements_revision"]:
            raise boundary_error(ValueError, "service_result_invalid", "Conversation plan target changed.")
    return raw


def report(store, job, raw, conn=None):
    return _text_only(raw, 32000)


def assessment(store, job, raw, conn=None):
    if job['kind']=='recovery_plan' and isinstance(raw,dict) and 'action' in raw:
        from .work_actions import validate_action
        if set(raw)!={'text','action'}:raise ValueError('Recovery expects text and an optional action.')
        text(raw['text']);validate_action(raw['action'])
        return raw
    return _text_only(raw, 16000)


def _text_only(raw, maximum):
    if not isinstance(raw, dict) or set(raw) != {"text"}:
        raise boundary_error(ValueError, "service_result_invalid", "This job expects text only.")
    text(raw["text"], maximum=maximum)
    return raw


def apply_recovery(store, conn, job, result):
    from .job_contracts import work_context
    from .protocol import fingerprint
    work = store.get("work", job["target"], conn)
    if (work.get("cancel_requested") or not work.get("pause_requested")
            or work["requirements_revision"] != job["revision"]
            or (work.get("recovery_request") or {}).get("job_id") != job["id"]
            or fingerprint(work_context(store, work, conn)) != job["input"]["basis"]):
        return "stale"
    work.update(recovery_plan={"job_id": job["id"], "basis": job["input"]["basis"], "text": result["text"],
                               "action":result.get('action'),"execute_requested":job['input'].get('execute_requested',False),
                               "proposed_at": time.time(), "execution_authorized": False}, revision=work["revision"] + 1)
    store.db._put(conn, "service_work", work["id"], work)
    store.changed(conn, work["id"])
    return "completed"


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
            raise boundary_error(ValueError, "service_result_invalid", "Cannot merge a work item with itself.")
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


def apply_integration_assessment(store, conn, job, result):
    from .delivery.periodic import finish

    return finish(store, conn, job, result)


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
