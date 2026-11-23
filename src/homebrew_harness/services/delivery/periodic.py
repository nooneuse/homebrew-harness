"""Persist periodic integration decisions independently of CI and PR polling."""

from copy import deepcopy
from time import time as now

from homebrew_harness.services.protocol import fingerprint

from .git import StaleDelivery
from .spec import periodic_settings

WAITING_STATES = {"ci_waiting", "review_waiting", "merge_ready", "integration_required", "diagnosis_waiting"}


def waiting_for_delivery(row):
    return row["state"] in WAITING_STATES or (
        row["state"] == "blocked" and row.get("error") == "ci_wait_expired"
    )


def _target_drift(candidate, observed):
    return observed.get("base") is not None and observed["base"] != candidate.get("target_base")


def needs_assessment(candidate, observed):
    # A clean PR can be merged under the platform policy even if main moved.
    # Unknown mergeability is an observation, not proof of content conflicts.
    return (_target_drift(candidate, observed)
            and (observed.get("mergeable") is not True
                 or observed.get("mergeable_state") in {"behind", "dirty", "unknown"}))


def has_risk(spec, row, observed=None):
    if observed is None:
        observed = row.get("observed") or {}
    candidate = row.get("candidate") or {}
    ci = row.get("ci") or {}
    return needs_assessment(candidate, observed) or (
        _target_drift(candidate, observed) and row.get("integration_ci_basis") is not None
        and ci.get("head") == candidate.get("commit") and "checks" in ci
        and row["integration_ci_basis"] == ci_failure_basis(spec, ci)
    )


def eligible(delivery, row, work):
    observed = row.get("observed") or {}
    candidate = row.get("candidate") or {}
    return bool(
        delivery.spec.get("base_update") == "periodic"
        and work and work.get("state") == "review"
        and not work.get("pause_requested") and not work.get("cancel_requested")
        and work.get("changeset_id") == row["id"]
        and work["requirements_revision"] == row["requirements_revision"]
        and not row.get("merge") and row.get("change_request") and candidate
        and waiting_for_delivery(row)
        and observed.get("state") == "open" and not observed.get("merged")
        and observed.get("head") == row.get("review_commit") == candidate["commit"]
        and observed.get("source_branch") == row["branch"]
        and observed.get("base_branch") == delivery.spec["target_branch"]
    )


def _save(delivery, conn, row, schedule):
    # A target observation does not invalidate an unrelated CI or review job.
    row["integration_schedule"] = schedule
    delivery.store.put(conn, "changesets", row)


def _read(delivery, conn, identity):
    row = delivery.store.get("changesets", identity, conn)
    work = delivery.ms.db._get(conn, "service_work", row["work_id"])
    return row, work


def ci_failure_basis(spec, ci):
    """Key a baseline diagnosis to its actual checks, not a changing target SHA."""
    return fingerprint([ci["head"], sorted(
        fingerprint([str(check["id"]), check["name"], check["app_id"], check["status"], check["conclusion"]])
        for check in ci["checks"] if check["head"] == ci["head"]
        and check["app_id"] in spec["check_app_ids"] and check["name"] in spec["required_checks"]
    )])


def ci_integration_eligible(spec, row):
    from .merge_flow import gate

    ci = row.get("ci") or {}
    candidate = row.get("candidate") or {}
    return (spec.get("base_update") == "periodic" and row.get("change_request")
            and ci.get("head") == row.get("review_commit") == candidate.get("commit")
            and ci.get("state") == "open" and not ci.get("merged") and not ci.get("draft")
            and "checks" in ci and _target_drift(candidate, ci) and gate(ci, spec)["failed"])


def _signals(delivery, conn, row):
    from .merge_flow import gate

    ci = row.get("ci") or {}
    same_head = ci.get("head") == row["candidate"]["commit"] and "checks" in ci
    result = gate(ci, delivery.spec) if same_head else None
    pending = False
    if delivery.ms.communication is not None:
        from homebrew_harness.services.communication.followup import pending as has_feedback

        pending = has_feedback(delivery.ms.communication, row, conn)
    return {
        "ci_ready": bool(result and result["ci_ready"]),
        "ci_failed": bool(result and result["failed"]),
        "ci_baseline_block": bool(result and result["failed"]
                                  and row.get("integration_ci_basis") == ci_failure_basis(delivery.spec, ci)),
        "review_ready": bool(result and result["review_gate"]["ready"]),
        "feedback_pending": pending,
    }


def _due(schedule, signals, observed, policy, clock):
    last_integration = schedule.get("last_integration_at")
    earliest = 0 if last_integration is None else last_integration + policy["min_integration_seconds"]
    ready = (signals["ci_ready"] and signals["review_ready"] and not signals["feedback_pending"]
             and not observed.get("draft") and observed.get("mergeable_state") in {"dirty", "behind"})
    # Take one early look when integration is the known remaining merge obstacle.
    # A deliberate deferral is not overridden by a stream of target commits.
    confirmed_ci_block = signals["ci_baseline_block"] and not observed.get("draft")
    early = (ready or confirmed_ci_block) and not schedule.get("last_decision") and not schedule.get("assessment_job_id")
    return clock >= earliest and (schedule.get("force_assessment") or early or clock >= schedule["next_assessment_at"])


def request_assessment(delivery, conn, identity, revision):
    row, work = _read(delivery, conn, identity)
    risk = row.get("integration_schedule") or {}
    if (type(revision) is not int or revision != row["revision"]
            or not eligible(delivery, row, work) or not has_risk(delivery.spec, row)
            or risk.get("state") not in {"waiting", "blocked"}):
        raise ValueError("Request an early assessment for the current waiting periodic integration.")
    _save(delivery, conn, row, {**risk, "state": "waiting", "force_assessment": True,
                                "next_assessment_at": now(), "retry_at": 0, "waiting_on": None})
    delivery.ms.changed(conn, row["work_id"])
    return {"id": identity, "state": "waiting", "assessment_requested": True}


def tick(delivery, identity):
    """Advance a due assessment or execute an admitted plan without waiting in a Run."""
    policy = periodic_settings(delivery.spec)
    with delivery.ms.db.write() as conn:
        meta = delivery.runtime.store.guard(conn, delivery.runtime.header)
        row, work = _read(delivery, conn, identity)
        if meta["state"] != "running" or not eligible(delivery, row, work):
            return False
        observed, candidate = row["observed"], row["candidate"]
        risk = row.get("integration_schedule") or {}
        clock = now()
        if not has_risk(delivery.spec, row):
            if risk and risk["state"] != "clear":
                _save(delivery, conn, row, {**risk, "state": "clear", "next_assessment_at": None,
                                            "cleared_at": clock})
            return False
        if risk.get("state") in {None, "clear", "integrated"}:
            risk = {
                "state": "waiting", "risk_since": clock,
                "next_assessment_at": clock + policy["assessment_seconds"],
                "deferral_deadline_at": clock + policy["max_deferral_seconds"],
                "last_integration_at": risk.get("last_integration_at"),
                "previous_decision": risk.get("last_decision"),
                "source_head": candidate["commit"],
                "observed_target": observed["base"],
            }
            _save(delivery, conn, row, risk)
        elif risk.get("source_head") != candidate["commit"]:
            # A different published candidate supersedes only the fixed plan;
            # it does not buy a new window for an unresolved integration risk.
            risk = {**risk, "source_head": candidate["commit"], "observed_target": observed["base"],
                    "state": "waiting", "next_assessment_at": min(clock, risk["deferral_deadline_at"]),
                    "selection": None, "assessment_job_id": None, "retry_at": 0,
                    "waiting_on": None, "force_assessment": False}
            _save(delivery, conn, row, risk)
        elif risk.get("observed_target") != observed["base"]:
            # Keep the original risk clock, pending decision and fixed plan.
            risk = {**risk, "observed_target": observed["base"]}
            if (risk["state"] == "blocked"
                    and (risk.get("error") or {}).get("code") == "integration_target_rewritten"):
                risk.update(state="waiting", next_assessment_at=clock, retry_at=0,
                            attempt_failures=0, waiting_on=None)
            _save(delivery, conn, row, risk)
        if risk["state"] == "assessing":
            job = delivery.ms.db._get(conn, "service_jobs_model", risk["assessment_job_id"])
            if job and job["state"] not in {"stale", "failed", "completed", "cancelled"}:
                return False
            risk = {**risk, "state": "waiting", "assessment_job_id": None,
                    "next_assessment_at": clock}
            _save(delivery, conn, row, risk)
        if risk["state"] == "blocked":
            return False
        if risk["state"] == "selected":
            if clock < risk.get("retry_at", 0):
                return False
            selection = deepcopy(risk["selection"])
            if selection["head"] != candidate["commit"]:
                _save(delivery, conn, row, {**risk, "state": "waiting", "next_assessment_at": clock,
                                            "selection": None})
                return False
        else:
            signals = _signals(delivery, conn, row)
            if clock < risk.get("retry_at", 0) or not _due(risk, signals, observed, policy, clock):
                return False
            selection = None
        if not delivery.ms.valid_authority(work):
            if risk.get("waiting_on") != "work_authority":
                _save(delivery, conn, row, {**risk, "waiting_on": "work_authority"})
            return False
        if delivery.preparing or _unsettled(delivery, conn, row):
            return False
    if selection is not None:
        from .merge_flow import refresh

        try:
            refresh(delivery, identity, periodic_plan=selection)
        except StaleDelivery:
            raise
        except Exception as error:
            _retry(delivery, identity, risk, error, selection=selection)
        return True

    from .integration import probe_candidate

    try:
        conflicts = probe_candidate(delivery.git, candidate, observed)
    except Exception as error:
        _retry(delivery, identity, risk, error)
        return True
    with delivery.ms.db.write() as conn:
        meta = delivery.runtime.store.guard(conn, delivery.runtime.header)
        current, work = _read(delivery, conn, identity)
        saved = current.get("integration_schedule")
        # A newer target is fine; the decision concerns the immutable pair we probed.
        if (meta["state"] != "running" or not eligible(delivery, current, work)
                or current["candidate"]["commit"] != candidate["commit"]
                or not saved or saved["state"] != "waiting" or saved["risk_since"] != risk["risk_since"]
                or not has_risk(delivery.spec, current)):
            return True
        clock = now()
        payload = {
            "work_id": current["work_id"], "requirements_revision": current["requirements_revision"],
            "source_head": candidate["commit"], "candidate_base": candidate["target_base"],
            "target_commit": observed["base"], "conflicts": conflicts,
            "platform_mergeability": observed.get("mergeable_state"), **signals,
            "risk_since": saved["risk_since"], "assessed_at": clock,
            "last_integration_at": saved.get("last_integration_at"),
            "operator_requested": bool(saved.get("force_assessment")),
            "deferral_deadline_at": saved["deferral_deadline_at"],
            "overdue": clock >= saved["deferral_deadline_at"],
            "previous_decision": saved.get("last_decision") or saved.get("previous_decision"),
        }
        sequence = saved.get("assessment_sequence", 0) + 1
        job = delivery.ms.job(conn, "integration_assessment", identity, current["revision"],
                              payload, identity + ":integration-assessment:" + fingerprint([payload, sequence]))
        # An unavailable/invalid assessment must become actionable instead of
        # retrying a fixed decision forever at the end of its window.
        attempts = delivery.ms.policy["job_attempts"]
        job["attempt_limit"] = min(attempts, 3) if attempts is not None else 3
        delivery.ms.put(conn, "jobs", job)
        _save(delivery, conn, current, {**saved, "state": "assessing", "assessment_sequence": sequence,
                                        "assessment_job_id": job["id"], "next_assessment_at": None,
                                        "retry_at": 0, "waiting_on": None, "error": None,
                                        "force_assessment": False})
        delivery.ms.changed(conn, current["work_id"])
    return True


def _unsettled(delivery, conn, row):
    return conn.execute(
        "SELECT 1 FROM records WHERE section='service_actions' AND owner=? "
        "AND json_extract(value,'$.work_id')=? "
        "AND json_extract(value,'$.state') IN ('started','unknown') LIMIT 1",
        ("service:" + delivery.ms.id, row["work_id"]),
    ).fetchone() is not None


def _retry(delivery, identity, risk, error, *, selection=None):
    from homebrew_harness.execution.execution_control import cleanup_scope
    from homebrew_harness.runtime.errors import error_info, error_message

    if delivery.stop.is_set():
        return
    with cleanup_scope(), delivery.ms.db.write() as conn:
        delivery.runtime.store.guard(conn, delivery.runtime.header)
        row, _ = _read(delivery, conn, identity)
        current = row.get("integration_schedule")
        if not current or current["state"] != risk["state"] or current["risk_since"] != risk["risk_since"]:
            return
        if selection is not None and current.get("selection") != selection:
            return
        count = current.get("attempt_failures", 0) + 1
        diagnostic = error_info(error)
        # Retry reads/preparation, not publication. Repeated overdue failures
        # become a visible block, not a reason to extend the original window.
        blocked = count >= 3 and now() >= current["deferral_deadline_at"]
        if diagnostic["code"] == "integration_target_rewritten":
            # The probed or admitted target is no longer in its allowed history.
            # Refresh the observation without extending the deferral deadline.
            row["next_merge"] = 0
            _save(delivery, conn, row, {**current, "state": "blocked" if blocked else "waiting",
                                        "selection": None, "next_assessment_at": None if blocked else now(),
                                        "waiting_on": "integration_evidence" if blocked else None,
                                        "error": diagnostic, "reason": error_message(error), "attempt_failures": count,
                                        "retry_at": now() + max(1, delivery.spec["ci_poll_seconds"])})
            return
        _save(delivery, conn, row, {**current, "state": "blocked" if blocked else current["state"],
                                    "waiting_on": "integration_evidence" if blocked else None,
                                    "error": diagnostic, "reason": error_message(error),
                                    "attempt_failures": count,
                                    "retry_at": now() + max(1, delivery.spec["ci_poll_seconds"])})


def job_current(store, conn, job):
    if store.delivery is None:
        return False
    row, work = _read(store.delivery, conn, job["target"])
    risk = row.get("integration_schedule") or {}
    return (eligible(store.delivery, row, work) and risk.get("state") == "assessing"
            and risk.get("assessment_job_id") == job["id"]
            and row["candidate"]["commit"] == job["input"]["source_head"]
            and row["requirements_revision"] == job["input"]["requirements_revision"]
            and has_risk(store.delivery.spec, row))


def finish(store, conn, job, result):
    if not job_current(store, conn, job):
        return "stale"
    delivery = store.delivery
    row, _ = _read(delivery, conn, job["target"])
    risk = row["integration_schedule"]
    clock = now()
    if result["action"] == "defer":
        wait = result.get("defer_seconds", periodic_settings(delivery.spec)["assessment_seconds"])
        schedule = {"state": "waiting", "next_assessment_at": min(clock + wait, risk["deferral_deadline_at"])}
    elif result["action"] == "integrate":
        schedule = {"state": "selected", "selection": {
            "job_id": job["id"], "head": job["input"]["source_head"],
            "target": job["input"]["target_commit"],
        }}
    else:
        schedule = {"state": "blocked", "waiting_on": "integration_decision", "next_assessment_at": None}
    _save(delivery, conn, row, {**risk, **schedule, "attempt_failures": 0, "retry_at": 0,
                                "last_decision": {**result, "job_id": job["id"], "at": clock,
                                                  "target": job["input"]["target_commit"]}})
    store.changed(conn, row["work_id"])
    return "completed"


def fail(store, conn, job):
    if job_current(store, conn, job):
        row, _ = _read(store.delivery, conn, job["target"])
        _save(store.delivery, conn, row, {**row["integration_schedule"], "state": "blocked",
                                         "waiting_on": "integration_assessment",
                                         "error": job.get("error_info"), "reason": job.get("error_detail")})
        store.changed(conn, row["work_id"])


def after_preparation(row, clock):
    """Keep the selected window through conflict repair, validation and publication."""
    if not row.get("integration_schedule"):
        return None
    return {**row["integration_schedule"], "state": "integrating", "selection": None,
            "last_integration_at": clock, "next_assessment_at": None, "waiting_on": None,
            "error": None, "attempt_failures": 0, "retry_at": 0}


def after_publication(row):
    schedule = row.get("integration_schedule")
    if schedule and schedule["state"] == "integrating":
        return {**schedule, "state": "integrated"}
    return schedule
