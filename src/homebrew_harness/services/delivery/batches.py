from homebrew_harness.runtime.limits import current_limits
"""Frozen request groups; decisions remain per-item and independently replayable."""

import time
from homebrew_harness.contracts import StaleReference
from homebrew_harness.services.maintenance_spec import text
from homebrew_harness.services.protocol import request_key, fingerprint
from homebrew_harness.services.request_views import project


def validate_decisions(decisions):
    limits = current_limits()
    if (
        not isinstance(decisions, list)
        or limits.batch_decisions is not None and len(decisions) > limits.batch_decisions
    ):
        raise ValueError("Batch expects bounded approve/deny/answer decisions.")
    for decision in decisions:
        if (
            not isinstance(decision, dict)
            or not {"id", "decision"} <= decision.keys()
            or decision.keys() - {"id", "decision", "allow_extra_attempt", "text"}
            or not isinstance(decision["id"], str)
            or not isinstance(decision["decision"], str)
            or decision["decision"] not in {"approve", "deny", "answer"}
            or ("allow_extra_attempt" in decision and type(decision["allow_extra_attempt"]) is not bool)
            or (decision.get("allow_extra_attempt") and decision["decision"] != "approve")
        ):
            raise ValueError("Batch expects bounded approve/deny/answer decisions.")
        if decision["decision"] == "answer":
            if "allow_extra_attempt" in decision:
                raise ValueError("A batch answer cannot include allow_extra_attempt.")
            if len(text(decision.get("text"), "batch answer")) > limits.input_text_chars:
                raise ValueError("Batch answer must be nonempty bounded text.")
        elif "text" in decision:
            raise ValueError("Batch approvals and denials cannot include answer text.")
    if len({d["id"] for d in decisions}) != len(decisions):
        raise ValueError("Duplicate batch decisions.")


class DecisionBatches:
    def __init__(self, runtime, maintenance):
        self.runtime, self.ms = runtime, maintenance

    def create(self, refs, request_id):
        if not isinstance(refs, list) or not refs or current_limits().delivery_batch_items is not None and len(refs) > current_limits().delivery_batch_items:
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
            if not isinstance(ref, dict) or not isinstance(ref.get("id"), str):
                raise ValueError("Refresh request references before creating a batch.")
            identity = ref["id"]
            current = available.get(identity)
            if current is None and identity.startswith("request_"):
                # The inbox can fill from later pages beyond the legacy requests list.
                raw = self.ms.db.get("service_decisions", identity)
                if raw and raw.get("service_id") == self.runtime.id and raw["state"] == "pending":
                    candidate = project(self.runtime.maintenance, raw, None)
                    if candidate and candidate["state"] == "pending":
                        current = candidate
            if current is None:
                raise ValueError("Refresh request references before creating a batch.")
            exact = current["ref"]
            allowed = [exact]
            if exact["target"]["kind"] == "run":
                # ServiceControls attaches the owning Service to its Run refs.
                allowed.append({**exact, "target": {**exact["target"], "owner_id": self.runtime.id}})
            if ref not in allowed:
                raise ValueError("Refresh request references before creating a batch.")
            normalized.append(ref)
        if len({r["id"] for r in normalized}) != len(normalized):
            raise ValueError("Duplicate batch members.")
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
        validate_decisions(decisions)
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
            choice = {k: decision[k] for k in ("decision", "text") if k in decision}
            try:
                if ref["target"]["kind"] == "service":
                    result = self.runtime.dispatch(
                        self.runtime.id,
                        "service_decide",
                        {
                            "id": ref["id"],
                            "revision": ref["revision"],
                            **choice,
                            **({"allow_extra_attempt": decision["allow_extra_attempt"]}
                               if "allow_extra_attempt" in decision else {}),
                        },
                        control_id,
                    )
                else:
                    if "allow_extra_attempt" in decision:
                        raise ValueError("Extra-attempt authority applies only to a current CI repair decision.")
                    result = self.runtime.dispatch(
                        self.runtime.id,
                        "decide",
                        {"ref": ref, **choice},
                        control_id,
                    )
                item = {"state": "confirmed", "result": result, "decision_id": child_id}
            except (ValueError, StaleReference) as error:
                from homebrew_harness.runtime.errors import error_info, error_message
                item = {
                    "state": "expired" if "expired" in str(error).lower() else "stale",
                    "error": type(error).__name__,
                    "error_info": error_info(error), "error_detail": error_message(error),
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
