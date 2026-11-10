"""Frozen request groups; decisions remain per-item and independently replayable."""

import time
from homebrew_harness.services.protocol import request_key, fingerprint


class DecisionBatches:
    def __init__(self, runtime, maintenance):
        self.runtime, self.ms = runtime, maintenance

    def create(self, refs, request_id):
        if not isinstance(refs, list) or not 1 <= len(refs) <= 100:
            raise ValueError("Batch requires bounded exact request references.")
        key = "batch_" + request_key("batch", self.runtime.id, request_id)
        prior = (
            self.ms.db.get("service_batches", key)
            if hasattr(self.ms.db, "get")
            else self.ms.store.runtime.get("service_batches", key)
        )
        if prior:
            if prior["items"] != refs:
                raise ValueError("Batch request ID has another membership.")
            return prior
        available = {r["ref"]["id"]: r for r in self.runtime.requests()}
        normalized = []
        for ref in refs:
            if (
                not isinstance(ref, dict)
                or ref.get("id") not in available
                or ref != available[ref["id"]]["ref"]
            ):
                raise ValueError("Refresh request references before creating a batch.")
            normalized.append(ref)
        if len({r["id"] for r in normalized}) != len(normalized):
            raise ValueError("Duplicate batch members.")
        key = "batch_" + request_key("batch", self.runtime.id, request_id)
        with self.ms.db.write() as conn:
            self.runtime.store.guard(conn, self.runtime.header)
            old = self.ms.db._get(conn, "service_batches", key)
            if old:
                if old["items"] != normalized:
                    raise ValueError("Batch request ID has another membership.")
                return old
            return self.ms.put(
                conn,
                "batches",
                {
                    "id": key,
                    "items": normalized,
                    "revision": 0,
                    "state": "open",
                    "created_at": time.time(),
                },
            )

    def apply(self, identity, revision, decisions, request_id):
        if (
            not isinstance(decisions, list)
            or len(decisions) > 100
            or any(
                not isinstance(d, dict)
                or set(d) != {"id", "decision"}
                or not isinstance(d["id"], str)
                or not isinstance(d["decision"], str)
                or d["decision"] not in {"approve", "deny"}
                for d in decisions
            )
        ):
            raise ValueError("Batch expects bounded approve/deny decisions.")
        if len({d["id"] for d in decisions}) != len(decisions):
            raise ValueError("Duplicate batch decisions.")
        batch = self.ms.get("batches", identity)
        refs = {r["id"]: r for r in batch["items"]}
        if (
            type(revision) is not int
            or revision != batch["revision"]
            or any(d["id"] not in refs for d in decisions)
        ):
            raise ValueError("Batch membership revision changed.")
        key = request_key("batch-decision", self.runtime.id, request_id)
        signature = fingerprint([identity, revision, decisions])
        with self.ms.db.write() as conn:
            self.runtime.store.guard(conn, self.runtime.header)
            prior = self.ms.db._get(conn, "service_batch_decisions", key)
            if prior and prior["payload"] != signature:
                raise ValueError("Batch decision ID has another payload.")
            receipt = prior or {
                "payload": signature,
                "batch_id": identity,
                "results": {},
                "checkpoint": "service:" + self.runtime.id,
            }
            self.ms.db._put(conn, "service_batch_decisions", key, receipt)
        for decision in decisions:
            if (
                decision["id"] in receipt["results"]
                and receipt["results"][decision["id"]]["state"] != "unknown"
            ):
                continue
            ref = refs[decision["id"]]
            child_id = "batch_" + request_key(
                "batch-child", identity, request_id + ":" + decision["id"]
            )
            control_id = "decision_" + request_key("decision", str(ref), child_id)
            try:
                if ref["target"]["kind"] == "service":
                    result = self.runtime.dispatch(
                        self.runtime.id,
                        "service_decide",
                        {
                            "id": ref["id"],
                            "revision": ref["revision"],
                            "decision": decision["decision"],
                        },
                        control_id,
                    )
                else:
                    result = self.runtime.dispatch(
                        self.runtime.id,
                        "decide",
                        {"ref": ref, "decision": decision["decision"]},
                        control_id,
                    )
                item = {"state": "confirmed", "result": result, "decision_id": child_id}
            except ValueError as error:
                item = {
                    "state": "expired" if "expired" in str(error).lower() else "stale",
                    "error": type(error).__name__,
                    "decision_id": child_id,
                }
            except Exception as error:
                item = {
                    "state": "unknown",
                    "error": type(error).__name__,
                    "decision_id": child_id,
                }
            with self.ms.db.write() as conn:
                self.runtime.store.guard(conn, self.runtime.header)
                receipt["results"][decision["id"]] = item
                self.ms.db._put(conn, "service_batch_decisions", key, receipt)
        return {
            "batch_id": identity,
            "results": receipt["results"],
            "complete": len(receipt["results"]) == len(decisions),
            "request_id": request_id,
        }
