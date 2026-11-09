"""SDK projections committed by existing input/checkpoint transactions."""

import hashlib
import json
import time

from homebrew_harness.security.data_boundary import redact


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()


def stable_id(kind, scope, request_id):
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 256:
        raise ValueError("SDK request_id must contain 1 to 256 characters.")
    return digest([kind, scope, request_id])[:32]


def cancel_inputs(db, conn, recipient, identities):
    """Discard amendments with their cancelled original input, in one transaction."""
    for identity, raw in conn.execute(
        "SELECT id,record FROM inbox WHERE recipient=?", (recipient,)
    ).fetchall():
        record = json.loads(raw)
        if (
            identity in identities
            or record.get("metadata", {}).get("target_input_id") in identities
        ):
            conn.execute(
                "DELETE FROM inbox WHERE recipient=? AND id=?", (recipient, identity)
            )
            db._put(
                conn,
                "input_receipts",
                identity,
                {
                    "input_id": identity,
                    "checkpoint": record["checkpoint"],
                    "state": "cancelled",
                    "revision": 0,
                    "target": "lead",
                    "record": record,
                },
            )


def accept_turn(db, conn, record):
    meta = record.get("metadata", {})
    if not meta.get("sdk_turn"):
        return
    if (
        meta["sdk_turn"] != record["id"]
        or meta["mode"] != "queued"
        or meta["target"] != "lead"
    ):
        raise ValueError("SDK turn acceptance requires an original queued Lead input.")
    db._put(
        conn,
        "api_turns",
        record["id"],
        {
            "id": record["id"],
            "session_id": record["checkpoint"].split(":")[1],
            "checkpoint": record["checkpoint"],
            "request_id": meta["sdk_request_id"],
            "prompt": record["content"],
            "goal": meta.get("sdk_goal"),
            "state": "queued",
            "revision": 0,
            "turn_id": None,
            "input_revision": 0,
            "created_at": time.time(),
            "output": None,
            "cancel_requested": False,
        },
    )


def bind_turn(db, conn, identity, session_id, turn_id):
    if not identity:
        return
    row = db._get(conn, "api_turns", identity)
    if row and row.get("cancel_requested"):
        from homebrew_harness.execution.model_gateway import ModelCancelled

        raise ModelCancelled("SDK query was cancelled before dispatch.")
    if not row or row["session_id"] != session_id or row["state"] != "queued":
        raise ValueError("SDK turn is cancelled, changed or already bound.")
    db._put(
        conn,
        "api_turns",
        identity,
        {
            **row,
            "state": "running",
            "turn_id": turn_id,
            "revision": row["revision"] + 1,
        },
    )


def commit_turn(db, conn, outcome):
    row = db._get(conn, "api_turns", outcome["id"])
    if (
        not row
        or row["session_id"] != outcome["session_id"]
        or row["turn_id"] != outcome["turn_id"]
    ):
        raise ValueError("SDK result does not own the original turn.")
    if row["state"] in {"completed", "cancelled", "failed"}:
        if row.get("output") != outcome.get("output"):
            raise ValueError("SDK result conflicts with an already committed turn.")
        return
    state = "cancelled" if row["cancel_requested"] else outcome["state"]
    db._put(
        conn,
        "api_turns",
        row["id"],
        {
            **row,
            **redact(outcome),
            "state": state,
            "revision": row["revision"] + 1,
            "confirmed": True,
        },
    )
