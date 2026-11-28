"""Publication wording over an already reviewed candidate and its real action history."""

import json

from homebrew_harness.runtime.errors import boundary_error
from homebrew_harness.services.job_contracts import work_context
from homebrew_harness.services.maintenance_spec import instructions_for
from homebrew_harness.services.protocol import fingerprint
from .git import StaleDelivery


class StalePublication(StaleDelivery):
    """The code is still current, but its queued automatic wording is not."""


def context_hash(work):
    return fingerprint(work.get("execution_context", []))


def actions(store, row, conn):
    return [json.loads(value) for value, in conn.execute(
        "SELECT value FROM records WHERE section='service_actions' AND owner=? "
        "AND json_extract(value,'$.changeset_id')=? "
        "AND json_extract(value,'$.kind')='ensure_change_request' ORDER BY rowid",
        ("service:" + store.id, row["id"]))]


def feedback(store, row, conn):
    denied = next((action for action in reversed(actions(store, row, conn))
                   if action["state"] == "denied"), None)
    if denied is None:
        return None
    decision = conn.execute(
        "SELECT value FROM records WHERE section='service_decisions' AND owner=? "
        "AND json_extract(value,'$.action_id')=? ORDER BY rowid DESC LIMIT 1",
        ("service:" + store.id, denied["id"])).fetchone()
    decision = json.loads(decision[0]) if decision else {}
    review = conn.execute(
        "SELECT value FROM records WHERE section='approval_reviews' AND owner=? "
        "AND json_extract(value,'$.request_id')=? ORDER BY rowid DESC LIMIT 1",
        ("service:" + store.id, decision.get("id"))).fetchone()
    review = json.loads(review[0]) if review else {}
    return {"action_id": denied["id"], "request_id": decision.get("id"),
            "plan": denied["plan"], "decision": decision.get("state"),
            "review_state": review.get("state"), "verdict": review.get("verdict")}


def content(row):
    return row.get("publication") or (row.get("review") or {}).get("publication")


def same_content(publication, rejected):
    return bool(publication and rejected
                and all(publication.get(key) == rejected["plan"].get(key)
                        for key in ("title", "body")))


def basis(store, row, rejected, conn):
    evidence = {"commit": row["candidate"]["commit"], "frozen": row["frozen"],
                "work": work_context(store.ms, store.current(row, conn), conn),
                "instructions": instructions_for(store.ms.policy, "review"),
                "publication": content(row), "rejection": rejected}
    if row.get("clarifications"):
        evidence["clarifications"] = row["clarifications"]
    return fingerprint(evidence)


def mark_redraft(store, row, conn, action_id):
    """Restart only the current, unpublished wording lane, not code validation."""
    if (row["state"] != "review_branch_ready" or row.get("observation_terminal")
            or (row.get("candidate") or {}).get("artifacts_retired")
            or not row.get("frozen") or row["frozen"] != store.freeze(row)
            or store.merged_pr(conn, row)):
        return False
    row.update(publication=None, publication_context_hash=None, publication_basis=None, publication_redraft=True,
               publication_redraft_action=action_id, error=None, error_detail=None, error_info=None,
               revision=row["revision"] + 1)
    store.put(conn, "changesets", row)
    store.ms.changed(conn, row["work_id"])
    return True


def prepare(delivery, row):
    """Accept a writing job; never restart code validation or replay a denied plan."""
    store = delivery.store
    with store.db.write() as conn:
        store.runtime.store.guard(conn, store.runtime.header)
        current = store.get("changesets", row["id"], conn)
        store.current(current, conn)
        if current["revision"] != row["revision"] or current["state"] != row["state"]:
            return True  # A concurrent answer or transition already advanced this candidate.
        redraft = current.get("publication_redraft")
        if current.get("change_request") and not current.get("draft_published") and not redraft and current["state"] not in {
            "publication_denied", "publication_attention",
        }:
            return False
        rejected = feedback(store, current, conn)
        publication = content(current)
        needs_draft = (
            not publication and store.spec["require_review"]
            and ("ensure_change_request" not in store.spec["auto_actions"]
                 or store.spec.get("auto_publish_pr_text"))
        ) or same_content(publication, rejected)
        if rejected and not publication:
            needs_draft = True
        if not needs_draft and not redraft and current["state"] != "publication_attention":
            return False
        if rejected and rejected["review_state"] == "reviewing":
            return True  # The native decision has landed; wait for its audit verdict.
        signature = basis(store, current, rejected, conn)
        if current["state"] == "publication_attention" and current.get("publication_basis") == signature:
            return True
        existing = actions(store, current, conn)
        if any(action["state"] in {"started", "unknown"} for action in existing):
            return True  # Reconcile the external write before admitting new wording.
        for action in existing:
            store.supersede_unstarted(conn, action, "publication_redraft")
        current.pop("publication_redraft", None)
        current.update(state="publication_drafting", publication_basis=signature,
                       publication_feedback=rejected, error=None, error_detail=None, error_info=None,
                       revision=current["revision"] + 1)
        job = store.ms.job(conn, "publication", current["id"], current["revision"],
                           {"basis": signature, "feedback": rejected},
                           current["id"] + ":publication:" + signature)
        current["reasoning_job_id"] = job["id"]
        store.put(conn, "changesets", current)
        store.ms.changed(conn, current["work_id"])
        return True


def accept(store, row, job, result, conn):
    if row["state"] != "publication_drafting":
        raise StaleDelivery("Publication job is no longer current.")
    if any(action["state"] in {"started", "unknown"}
           for action in actions(store, row, conn)):
        raise boundary_error(ValueError, "delivery_contract", "Publication already has an accepted external effect.")
    if row["frozen"] != store.freeze(row):
        raise StaleDelivery("Code quality changed while drafting publication.")
    rejected = job["input"].get("feedback")
    if result.get("human_required"):
        row.update(state="publication_attention", error="publication_needs_operator",
                   error_detail=result["reason"], publication_basis=job["input"]["basis"])
        store.information_request(conn, row, stage="publication", basis=job["input"]["basis"],
                                  reason=result["reason"])
        return
    if same_content(result, rejected):
        raise boundary_error(ValueError, "service_result_invalid", "This exact title/body was already denied. Correct the draft or return human_required with a reason.")
    row["publication_history"] = [*row.get("publication_history", []),
                                  {"publication": content(row), "feedback": rejected,
                                   "job_id": job["id"]}]
    row.update(publication=result, publication_context_hash=context_hash(store.current(row, conn)),
               state="review_branch_ready", error=None, error_detail=None, error_info=None)
