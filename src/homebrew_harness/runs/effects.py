"""Fixed local wakeups and explicitly configured notification delivery."""

import hashlib
import json
import time


def enqueue(db, conn, run, kind, payload, key):
    if kind not in {"wake", "notification"}:
        raise ValueError("Unknown managed effect kind.")
    if kind == "notification" and not run.get("notify_enabled"):
        return None
    identity = (
        "fx_" + hashlib.sha256((run["id"] + kind + key).encode()).hexdigest()[:32]
    )
    if db._get(conn, "run_outbox", identity) is None:
        db._put(
            conn,
            "run_outbox",
            identity,
            {
                "id": identity,
                "run_id": run["id"],
                "checkpoint": "lead:" + run["session_id"],
                "kind": kind,
                "payload": payload,
                "target": run.get("control_identity"),
                "generation": run["generation"],
                "revision": 0,
                "state": "queued",
                "attempts": 0,
                "next_retry_at": 0,
                "created_at": time.time(),
            },
        )
    return identity


def notice(db, conn, run, reason, request_id=None):
    payload = {"run_id": run["id"], "status": run["status"], "reason_code": reason}
    if request_id:
        payload["request_id"] = request_id
    return enqueue(
        db,
        conn,
        run,
        "notification",
        payload,
        json.dumps(payload, sort_keys=True) + ":" + str(run["revision"]),
    )
