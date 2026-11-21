"""Filter current decision references before returning a bounded cursor page."""

import json
import time
from .maintenance_spec import number
from .protocol import MAX_FRAME


def _project(maintenance, value, work_id):
    if work_id and value["work_id"] != work_id:
        return None, "different_work"
    store = maintenance.store
    if value["kind"] == "delivery_authority":
        if maintenance.delivery is None:
            return None, "delivery_not_configured"
        action = store.get("actions", value["action_id"])
        if action["state"] not in {"awaiting_approval", "queued", "unknown"}:
            return None, "action_no_longer_pending"
        try:
            maintenance.delivery.store.current(
                store.get("changesets", value["changeset_id"])
            )
        except ValueError:
            return None, "candidate_changed"
    if value["kind"] in {
        "communication_authority",
        "discussion_information",
        "discussion_requirements",
    }:
        if maintenance.communication is None:
            return None, "communication_not_configured"
        c = maintenance.communication.store
        discussion = c.get("discussions", value["discussion_id"])
        if value["basis"] != c.basis(discussion):
            return None, "basis_changed"
        if value["kind"] == "communication_authority" and c.get(
            "communication_actions", value["action_id"]
        )["state"] not in {"awaiting_approval", "queued"}:
            return None, "action_no_longer_pending"
        arguments = {
            "discussion_id": value["discussion_id"],
            "action_id": value.get("action_id"),
            "candidate": value["candidate"],
        }
        question = value.get("reason", "")
    else:
        work = maintenance.runtime.store.work(value["work_id"], store.id)
        if value["requirements_revision"] != work["requirements_revision"]:
            return None, "requirements_changed"
        arguments = {
            "work_id": value["work_id"],
            "scope": value["candidate"].get("scope"),
            "plan": value["candidate"]["plan"],
        }
        question = (
            value["candidate"]["plan"] if value["kind"] == "work_information" else ""
        )
        if value["kind"] == "delivery_authority":
            arguments.update(
                action_id=value["action_id"],
                changeset_id=value["changeset_id"],
                subject=action["plan"].get("subject"),
                head=action["plan"]["commit"],
                method=action["plan"].get("method"),
            )
        elif value["kind"] == "ci_repair":
            delivery = maintenance.delivery
            if delivery is None:
                return None, "delivery_not_configured"
            row = store.get("changesets", value["changeset_id"])
            try:
                delivery.store.current(row)
            except ValueError:
                return None, "candidate_changed"
            if (row["state"] != "repair_waiting"
                    or value["candidate"]["plan"] != (row.get("ci_diagnosis") or {}).get("plan")
                    or (delivery.store.repair_decision(row) or {}).get("id") != value["id"]):
                return None, "repair_changed"
            used, limit = work.get("ci_fix_attempts", 0), delivery.spec["ci_fix_limit"]
            arguments.update(changeset_id=row["id"], repair_attempts={
                "used": used, "limit": limit, "extra_required": used >= limit})
            if used >= limit:
                question = "This scoped repair needs one additional attempt beyond ci_fix_limit=" + str(limit) + ". Explicitly allow that attempt when approving."
    expired = value.get("expires_at") is not None and value["expires_at"] <= time.time()
    return {
        "ref": {
            "id": value["id"],
            "revision": value["revision"],
            "target": {"kind": "service", "id": store.id, "owner_id": None},
            "durable": True,
        },
        "kind": value["kind"],
        "question": question,
        "state": "expired" if expired else "pending",
        "tool": None,
        "arguments": arguments,
        "reason": value.get("reason", ""),
        "expires_at": value.get("expires_at"),
    }, "expired" if expired else None


def project(maintenance, value, work_id):
    return _project(maintenance, value, work_id)[0]


def summary(maintenance):
    result = {"actionable": 0, "unavailable": 0, "unavailable_examples": [], "oldest_actionable": None}
    cursor = 0
    while True:
        with maintenance.store.db.connection() as conn:
            rows = conn.execute(
                "SELECT rowid,value FROM records WHERE section='service_decisions' AND owner=? AND rowid>? AND json_extract(value,'$.state')='pending' ORDER BY rowid LIMIT 100",
                ("service:" + maintenance.store.id, cursor)).fetchall()
        for cursor, raw in rows:
            value = json.loads(raw)
            projected, reason = _project(maintenance, value, None)
            if projected and reason is None:
                result["actionable"] += 1
                created = value.get("created_at")
                if created is not None:
                    result["oldest_actionable"] = min(created, result["oldest_actionable"] or created)
            else:
                result["unavailable"] += 1
                if len(result["unavailable_examples"]) < 10:
                    result["unavailable_examples"].append({"id": value["id"], "kind": value["kind"], "reason": reason})
        if len(rows) < 100:
            return result


def page(maintenance, *, work_id=None, cursor=0, limit=20, expired=False):
    number(cursor, "cursor", 0, 2**63 - 1, integer=True)
    number(limit, "limit", 1, 100, integer=True)
    if type(expired) is not bool:
        raise ValueError("expired must be Boolean.")
    items = []
    after = cursor
    while len(items) <= limit:
        with maintenance.store.db.connection() as conn:
            rows = conn.execute(
                "SELECT rowid,value FROM records WHERE section='service_decisions' AND owner=? "
                "AND rowid>? AND json_extract(value,'$.state')='pending' "
                "AND (? IS NULL OR json_extract(value,'$.work_id')=?) "
                "AND (CASE WHEN json_extract(value,'$.expires_at') IS NOT NULL "
                "AND json_extract(value,'$.expires_at')<=? THEN 1 ELSE 0 END)=? ORDER BY rowid LIMIT 100",
                (
                    "service:" + maintenance.store.id,
                    after,
                    work_id,
                    work_id,
                    time.time(),
                    int(expired),
                ),
            ).fetchall()
        if not rows:
            break
        for position, raw in rows:
            after = position
            value = project(maintenance, json.loads(raw), work_id)
            if value:
                items.append((position, value))
                if len(items) > limit:
                    break
        if len(rows) < 100:
            break
    selected = []
    total = 0
    more = len(items) > limit
    for position, value in items[:limit]:
        size = len(json.dumps(value, ensure_ascii=False).encode())
        if selected and total + size > MAX_FRAME - 16384:
            more = True
            break
        total += size
        selected.append((position, value))
    return {
        "items": [value for _, value in selected],
        "next_cursor": selected[-1][0] if more and selected else None,
    }
