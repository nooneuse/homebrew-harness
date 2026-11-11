"""Filter current decision references before returning a bounded cursor page."""

import json
import time
from .maintenance_spec import number
from .protocol import MAX_FRAME


def project(maintenance, value, work_id):
    if work_id and value["work_id"] != work_id:
        return None
    store = maintenance.store
    if value["kind"] == "delivery_authority":
        action = store.get("actions", value["action_id"])
        if action["state"] not in {"awaiting_approval", "queued", "unknown"}:
            return None
        try:
            maintenance.delivery.store.current(
                store.get("changesets", value["changeset_id"])
            )
        except ValueError:
            return None
    if value["kind"] in {
        "communication_authority",
        "discussion_information",
        "discussion_requirements",
    }:
        if maintenance.communication is None:
            return None
        c = maintenance.communication.store
        discussion = c.get("discussions", value["discussion_id"])
        if value["basis"] != c.basis(discussion):
            return None
        if value["kind"] == "communication_authority" and c.get(
            "communication_actions", value["action_id"]
        )["state"] not in {"awaiting_approval", "queued"}:
            return None
        arguments = {
            "discussion_id": value["discussion_id"],
            "action_id": value.get("action_id"),
            "candidate": value["candidate"],
        }
        question = value.get("reason", "")
    else:
        work = maintenance.runtime.store.work(value["work_id"], store.id)
        if value["requirements_revision"] != work["requirements_revision"]:
            return None
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
    }


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
