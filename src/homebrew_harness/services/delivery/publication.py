"""Publication wording over an already reviewed candidate and its real action history."""

import json

from homebrew_harness.runtime.errors import boundary_error
from homebrew_harness.services.job_contracts import work_context
from homebrew_harness.services.maintenance_spec import instructions_for
from homebrew_harness.services.protocol import fingerprint
from .git import StaleDelivery


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
    return fingerprint({"commit": row["candidate"]["commit"], "frozen": row["frozen"],
                        "work": work_context(store.ms, store.current(row, conn), conn),
                        "instructions": instructions_for(store.ms.policy, "review"),
                        "publication": content(row), "rejection": rejected})


def prepare(delivery, row):
    """Accept a writing job; never restart code validation or replay a denied plan."""
    store = delivery.store
    with store.db.write() as conn:
        store.runtime.store.guard(conn, store.runtime.header)
        current = store.get("changesets", row["id"], conn)
        store.current(current, conn)
        if current["revision"] != row["revision"]:
            raise StaleDelivery("Candidate changed before publication preparation.")
        if current.get('change_request') and current['state'] not in {'publication_denied','publication_attention'}:
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
        if not needs_draft and current["state"] != "publication_attention":
            return False
        if rejected and rejected["review_state"] == "reviewing":
            return True  # The native decision has landed; wait for its audit verdict.
        signature = basis(store, current, rejected, conn)
        if current["state"] == "publication_attention" and current.get("publication_basis") == signature:
            return True
        existing = actions(store, current, conn)
        if any(action["state"] in {"started", "unknown"} for action in existing):
            raise boundary_error(ValueError, "delivery_contract", "Publication effects must settle before changing their wording.")
        for action in existing:
            if action["state"] in {"queued", "awaiting_approval"}:
                action.update(state="superseded", error="publication_redraft", revision=action["revision"] + 1)
                store.put(conn, "actions", action)
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
           for action in actions(store,row,conn)):
        raise boundary_error(ValueError, "delivery_contract", "Publication already has an accepted external effect.")
    rejected = job["input"].get("feedback")
    if result.get("human_required"):
        row.update(state="publication_attention", error="publication_needs_operator",
                   error_detail=result["reason"], publication_basis=job["input"]["basis"])
        return
    if same_content(result, rejected):
        raise boundary_error(ValueError, "service_result_invalid", "This exact title/body was already denied. Correct the draft or return human_required with a reason.")
    row["publication_history"] = [*row.get("publication_history", []),
                                  {"publication": content(row), "feedback": rejected,
                                   "job_id": job["id"]}]
    row.update(publication=result, state="review_branch_ready", error=None,
               error_detail=None, error_info=None)
    if row["frozen"] != store.freeze(row):
        raise StaleDelivery("Code quality changed while drafting publication.")
