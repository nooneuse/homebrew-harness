"""Platform-neutral change feedback ingestion and native execution fences."""

import json
import time
from homebrew_harness.platforms.contracts import (
    ObjectRef,
    ChangeFeedback,
    REFERENCE_ORIGIN,
)
from homebrew_harness.platforms.registry import change_request
from homebrew_harness.platforms.http import RemoteFailure
from homebrew_harness.storage.storage_gate import storage_gate
from .requests import digest
from .spec import TERMINAL


class ChangeRequestSource:
    def __init__(self, declaration, environment, *, roots=()):
        spec, self.adapter, self.identity = change_request(
            declaration, environment, roots=roots
        )
        self.subject = ObjectRef(
            spec["provider"],
            spec["instance"],
            spec["project"],
            "change_request",
            str(spec["number"]),
            spec["number"],
        )
        self.actor_ids, self.app_ids = (
            frozenset(spec["actor_ids"]),
            frozenset(spec["app_ids"]),
        )
        self.poll_seconds = spec["poll_seconds"]

    def close(self):
        self.adapter.close()

    def poll(self, store, run_id, deadline):
        from .controller import git_head

        run = store.get(run_id)
        if run["status"] in TERMINAL:
            return False
        identity = "src_" + digest({"run": run_id, "source": self.identity})[:32]
        prior = store.runtime.get(
            "run_sources", identity, {"revision": 0, "cursor": {}, "next_poll_at": 0}
        )
        if prior["next_poll_at"] > time.time():
            return False
        head = git_head(store.root)
        if not head:
            raise ValueError("Change feedback requires a local Git HEAD.")
        try:
            batch = self.adapter.fetch(self.subject, prior["cursor"], deadline)
            if not isinstance(batch, ChangeFeedback):
                raise ValueError("Invalid change feedback response.")
            batch.validate(self.subject)
            observed_head = batch.head
            selected = []
            for item in batch.events:
                kind = item.object.kind
                event = {
                    "run_id": run_id,
                    "checkpoint": "lead:" + run["session_id"],
                    "source": identity,
                    "subject": self.subject.data(),
                    "object": item.object.data(),
                    "kind": kind,
                    "object_id": item.object.id,
                    "actor_id": item.actor_id,
                    "head": item.head,
                    "observed_head": observed_head,
                    "object_version": item.version,
                    "body": item.body,
                    "status": item.status,
                    "conclusion": item.conclusion,
                }
                if len(json.dumps(event, ensure_ascii=False)) > 64000:
                    raise ValueError("Source object exceeds its body limit.")
                allowed = type(item.actor_id) is int and item.actor_id in (
                    self.app_ids if item.actor_kind == "app" else self.actor_ids
                )
                current = observed_head == head and item.head == head
                reason = (
                    None
                    if allowed and current
                    else "actor_not_allowed"
                    if not allowed
                    else "old_head"
                )
                if item.pending:
                    reason = "old_or_pending_review"
                event.update(
                    id="evt_"
                    + digest({k: v for k, v in event.items() if k != "checkpoint"})[
                        :32
                    ],
                    revision=0,
                    state="ignored" if reason else "queued",
                    ignore_reason=reason,
                )
                selected.append(event)
            if git_head(store.root) != head:
                raise ValueError("Local head changed while polling its source.")
            db = store.runtime.backend()
            with storage_gate(store.root), db.write() as conn:
                current = db._get(conn, "run_sources", identity, {"revision": 0})
                latest_run = db._get(conn, "runs", run_id)
                if current["revision"] != prior["revision"] or latest_run.get(
                    "control_identity"
                ) != run.get("control_identity"):
                    raise ValueError(
                        "Source ownership or policy changed before ingest."
                    )
                recipient = "reference_" + run_id
                count = conn.execute(
                    "SELECT count(*) FROM inbox WHERE recipient=?", (recipient,)
                ).fetchone()[0]
                for event in selected:
                    if db._get(conn, "run_events", event["id"]) is not None:
                        continue
                    if latest_run["status"] in TERMINAL:
                        event.update(state="ignored", ignore_reason="run_closed")
                    if event["state"] == "queued":
                        count += 1
                        if count > 64:
                            raise ValueError(
                                "Run reference inbox is full; no source cursor was advanced."
                            )
                        summary = {
                            k: v
                            for k, v in event.items()
                            if k
                            in {
                                "id",
                                "kind",
                                "head",
                                "actor_id",
                                "status",
                                "conclusion",
                            }
                        }
                        summary["body"] = json.dumps(event["body"], ensure_ascii=False)[
                            :4000
                        ]
                        db._send(
                            conn,
                            recipient,
                            {
                                "id": event["id"],
                                "from": REFERENCE_ORIGIN,
                                "to": recipient,
                                "type": "run_reference",
                                "content": json.dumps(summary, ensure_ascii=False),
                                "ts": time.time(),
                                "metadata": {
                                    "origin": REFERENCE_ORIGIN,
                                    "source": identity,
                                },
                            },
                        )
                    db._put(conn, "run_events", event["id"], event)
                db._put(
                    conn,
                    "run_sources",
                    identity,
                    {
                        "id": identity,
                        "run_id": run_id,
                        "checkpoint": "lead:" + run["session_id"],
                        "identity": self.identity,
                        "revision": prior["revision"] + 1,
                        "cursor": batch.cursor,
                        "failures": 0,
                        "next_poll_at": time.time() + self.poll_seconds,
                    },
                )
            return True
        except Exception as error:
            db = store.runtime.backend()
            with storage_gate(store.root), db.write() as conn:
                current = db._get(conn, "run_sources", identity, prior)
                failures = current.get("failures", 0) + 1
                db._put(
                    conn,
                    "run_sources",
                    identity,
                    {
                        **current,
                        "id": identity,
                        "run_id": run_id,
                        "checkpoint": "lead:" + run["session_id"],
                        "identity": self.identity,
                        "revision": current["revision"] + 1,
                        "failures": failures,
                        "last_error": type(error).__name__,
                        "last_error_status": error.status
                        if isinstance(error, RemoteFailure)
                        else None,
                        "next_poll_at": time.time()
                        + min(300, self.poll_seconds * 2 ** min(failures, 5)),
                    },
                )
            return False
