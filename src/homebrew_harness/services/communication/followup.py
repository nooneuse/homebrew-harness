"""Same-Work review repairs over the existing discussion and Delivery records."""

import copy
import json
import time

from homebrew_harness.services.protocol import fingerprint
from .transitions import request_analysis


def context(store, row, conn):
    """An external PR or a discussion link alone never grants repair authority."""
    if (row["subject"]["kind"] != "change_request" or not row.get("work_id")
            or store.delivery_work(conn, row["subject"]) != row["work_id"]):
        return None
    work = store.db._get(conn, "service_work", row["work_id"])
    if not work or not work.get("changeset_id"):
        return None
    candidate = store.db._get(conn, "service_changesets", work["changeset_id"])
    if (not candidate or candidate.get("change_request") != row["subject"]
            or candidate["requirements_revision"] != work["requirements_revision"]):
        return None
    from homebrew_harness.services.job_contracts import work_context

    return {
        "work": work_context(store.ms, work, conn),
        "changeset_id": candidate["id"],
        "head": (candidate.get("candidate") or {}).get("commit"),
        "delivery_state": candidate["state"],
        "repair": copy.deepcopy(row.get("repair")),
    }


def validate_repair(store, job, proposal, conn):
    if proposal.get("next_step") != "repair":
        return
    row = store.get("discussions", job["target"], conn)
    current = context(store, row, conn)
    expected = job["input"].get("own_pr")
    if not current or not expected:
        raise ValueError("Repair is available only for this Service's published PR and existing Work.")
    if proposal.get("draft") != job["input"]["projection"].get("draft"):
        raise ValueError("PR repair cannot replace the accepted requirements draft.")
    if (current["head"] != expected["head"]
            or current["changeset_id"] != expected["changeset_id"]
            or current["work"]["requirements_revision"] != expected["work"]["requirements_revision"]):
        raise ValueError("The PR or accepted requirements changed; reassess the current candidate.")
    if (current["work"]["state"] != "review"
            or current["work"]["authority"].get("revoked")
            or (row.get("repair") or {}).get("state") == "admitted"):
        raise ValueError("The Work is not ready for another PR repair; preserve feedback and wait for its current execution.")
    observed = row.get("observed_basis") or {}
    if observed.get("state") != "open" or observed.get("head") != current["head"]:
        raise ValueError("PR repair requires an open observation of the current published head.")
    for identity in proposal["repair"]["sources"]:
        event = store.db._get(conn, "service_events", identity)
        if (not event or event.get("discussion_id") != row["id"]
                or event["sequence"] > job["input"]["seq"] or event.get("echo")):
            raise ValueError("PR repair must cite actual feedback from this frozen discussion.")


def record(store, row, job, proposal, round_id):
    own = job["input"]["own_pr"]
    row["repair"] = {
        "round_id": round_id,
        "state": "pending",
        "changeset_id": own["changeset_id"],
        "head": own["head"],
        "requirements_revision": own["work"]["requirements_revision"],
        "plan": proposal["repair"]["plan"],
        "sources": list(proposal["repair"]["sources"]),
        "reason": proposal["reason"],
        "basis": store.basis(row),
        "requested_at": time.time(),
    }


def promote(store, candidate):
    """Admit a settled feedback repair using the existing scoped repair policy."""
    delivery = store.ms.delivery
    if delivery is None:
        return False
    with store.db.write() as conn:
        store.runtime.store.guard(conn, store.runtime.header)
        work = store.db._get(conn, "service_work", candidate["work_id"])
        if (not work or work.get("pause_requested") or work.get("cancel_requested")
                or work.get("changeset_id") != candidate["id"] or work["state"] != "review"):
            return False
        current = delivery.store.get("changesets", candidate["id"], conn)
        if (current["state"] not in {"ci_waiting", "review_waiting", "merge_ready", "integration_required"}
                or current.get("merge") or delivery.preparing):
            return False
        unsettled = conn.execute(
            "SELECT 1 FROM records WHERE section='service_actions' AND owner=? "
            "AND json_extract(value,'$.changeset_id')=? "
            "AND json_extract(value,'$.state') IN ('started','unknown') LIMIT 1",
            ("service:" + store.id, candidate["id"]),
        ).fetchone()
        if unsettled:
            return False
        discussions = conn.execute(
            "SELECT value FROM records WHERE section='service_discussions' AND owner=? "
            "AND json_extract(value,'$.work_id')=? "
            "AND json_extract(value,'$.repair.state')='pending' ORDER BY rowid",
            ("service:" + store.id, work["id"]),
        ).fetchall()
        for (raw,) in discussions:
            row = json.loads(raw)
            if row["state"] == "paused" or row["subject"] != current.get("change_request"):
                continue
            repair = row["repair"]
            if (repair["basis"] != store.basis(row)
                    or repair["head"] != current["candidate"]["commit"]
                    or repair["requirements_revision"] != work["requirements_revision"]):
                repair["state"] = "needs_reassessment"
                request_analysis(row)
                store.put(conn, "discussions", row)
                store.supersede(conn, row)
                return True
            delivery.store.current(current, conn)
            diagnosis = {
                "classification": "code", "next_action": "repair",
                "reason": repair["reason"],
                "plan": repair["plan"] + "\n\nOriginal PR feedback: "
                    + ", ".join(repair["sources"]) + ". Keep the accepted Work scope and PR.",
            }
            current.update(
                ci_diagnosis=diagnosis,
                pr_feedback={"discussion_id": row["id"], **copy.deepcopy(repair)},
                state="repair_approved" if delivery.store.repair_allowed(
                    {**current, "ci_diagnosis": diagnosis}, work, conn
                ) else "repair_waiting",
                revision=current["revision"] + 1,
            )
            delivery.store.put(conn, "changesets", current)
            if current["state"] == "repair_waiting":
                delivery.store.repair_request(conn, current)
            repair.update(state="admitted", admitted_at=time.time())
            work["events"] = list(dict.fromkeys([*work.get("events", []), *repair["sources"]]))
            work["revision"] += 1
            store.db._put(conn, "service_work", work["id"], work)
            row["revision"] += 1
            store.put(conn, "discussions", row)
            store.ms.changed(conn, work["id"])
            return True
    return False


def publication(store, conn, row, action):
    """Wake the discussion from a confirmed new head, including after restart."""
    if action["kind"] != "ensure_change_request" or action.get("state") != "confirmed":
        return
    previous = row.get("delivery_publication")
    published = {"action_id": action["id"], "head": action["plan"].get("commit"),
                 "changeset_id": action.get("changeset_id")}
    if previous == published:
        return
    row["delivery_publication"] = published
    row["next_poll"] = 0
    repair = row.get("repair")
    if repair and repair["state"] == "admitted":
        candidate = store.db._get(conn, "service_changesets", published["changeset_id"] or "")
        previous = candidate.get("previous") if candidate else None
        seen = set()
        while previous and previous != repair["changeset_id"] and previous not in seen:
            seen.add(previous)
            ancestor = store.db._get(conn, "service_changesets", previous)
            if (not ancestor or ancestor["work_id"] != row["work_id"]
                    or ancestor["requirements_revision"] != repair["requirements_revision"]):
                previous = None
                break
            previous = ancestor.get("previous")
        if (candidate and previous == repair["changeset_id"]
                and candidate["work_id"] == row["work_id"]
                and candidate["requirements_revision"] == repair["requirements_revision"]):
            repair.update(state="published", published_head=published["head"],
                          publication_action=action["id"])
            if row["state"] != "paused":
                request_analysis(row)
    store.put(conn, "discussions", row)
    store.supersede(conn, row)


def pending(store, candidate, conn):
    for (raw,) in conn.execute(
        "SELECT value FROM records WHERE section='service_discussions' AND owner=? "
        "AND json_extract(value,'$.work_id')=?",
        ("service:" + store.id, candidate["work_id"]),
    ):
        row = json.loads(raw)
        if row["subject"] == candidate.get("change_request") and (
            row["input_revision"] > row.get("assessed_input_revision", 0)
            or row["analysis_epoch"] > row.get("assessed_epoch", 0)
            or (row.get("repair") or {}).get("state") in {"pending", "admitted", "needs_reassessment"}
            or any(item.get("status", "open") in {"open", "needs_explanation"}
                   for kind in ("questions", "findings") for item in row["projection"].get(kind, []))
        ):
            return True
        if row["subject"] == candidate.get("change_request") and conn.execute(
            "SELECT 1 FROM records WHERE section='service_communication_actions' AND owner=? "
            "AND json_extract(value,'$.discussion_id')=? "
            "AND json_extract(value,'$.state') IN ('awaiting_approval','queued','started','unknown') LIMIT 1",
            ("service:" + store.id, row["id"]),
        ).fetchone():
            return True
    return False


def basis(store, candidate, conn):
    """Bind merge admission to handled feedback, not just the Git head."""
    values = []
    for (raw,) in conn.execute(
        "SELECT value FROM records WHERE section='service_discussions' AND owner=? "
        "AND json_extract(value,'$.work_id')=? ORDER BY key",
        ("service:" + store.id, candidate["work_id"]),
    ):
        row = json.loads(raw)
        if row["subject"] == candidate.get("change_request"):
            action = store.db._get(conn, "service_communication_actions", row.get("last_action") or "")
            values.append({"id": row["id"], "basis": store.basis(row), "projection": row["projection"],
                           "assessed": [row.get("assessed_input_revision"), row.get("assessed_epoch")],
                           "repair": row.get("repair"),
                           "reply": {k: action.get(k) for k in ("id", "state", "result")} if action else None})
    return fingerprint(values) if values else None
