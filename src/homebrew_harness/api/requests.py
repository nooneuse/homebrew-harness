"""Explicit host decisions over instantaneous and durable request controllers."""

from .base import host_operation

import uuid
import threading
from homebrew_harness.runs.requests import HumanRequests
from homebrew_harness.storage.api_state import digest, stable_id
from .types import ExecutionRef, RequestRef, HumanRequest, StaleReference


class Requests:
    def __init__(self, core):
        self.core = core
        self.lock = threading.RLock()

    def _session(self, sid):
        session = self.core.sessions.live.get(sid)
        if session:
            return session.runtime, session.broker
        for handle in self.core.interactions.live.values():
            if (
                handle.host.session is not None
                and handle.host.session.session_id == sid
            ):
                return handle.host.session, handle.host.broker
        return None, None

    def list(self, *, target):
        if hasattr(target, "ref"):
            target = target.ref
        if not isinstance(target, ExecutionRef):
            raise ValueError("Expected an execution reference.")
        if target.kind == "service":
            return self.core.services.get(target.id).requests()
        if target.kind == "work":
            return self.core.services.get(target.owner_id).requests(target.id)
        if target.kind == "interaction":
            task = self.core.store.get("interaction_tasks", target.id)
            if not task or not task.get("target"):
                return ()
            target = ExecutionRef(task["target"]["kind"], task["target"]["id"])
        if target.kind == "run":
            rows = HumanRequests(self.core.run_store).records(target.id)
            return tuple(
                self._view(row, target, True)
                for row in rows
                if row["state"] in {"pending", "approved"}
            )
        if target.kind in {"session", "turn"}:
            sid = target.id if target.kind == "session" else target.owner_id
            session, broker = self._session(sid)
            if session is None:
                return ()
            rows = broker.records()
            if target.kind == "turn":
                turn = self.core.store.get("api_turns", target.id)
                rows = [
                    row for row in rows if turn and row["turn_id"] == turn["turn_id"]
                ]
            return tuple(self._view(row, target, False) for row in rows)
        return ()

    def _view(self, row, target, durable):
        return HumanRequest(
            RequestRef(row["id"], row["revision"], target, durable),
            row["kind"],
            row.get("state", "pending"),
            row.get("tool"),
            row.get("arguments"),
            row.get("question", ""),
            row.get("reason", ""),
            row.get("expires_at"),
            row.get("context", {}),
        )

    @host_operation
    def _decide(
        self, ref, decision, *, answer=None, runs=None, seconds=None, decision_id=None
    ):
        with self.lock:
            return self._apply(
                ref,
                decision,
                answer=answer,
                runs=runs,
                seconds=seconds,
                decision_id=decision_id,
            )

    def _apply(
        self, ref, decision, *, answer=None, runs=None, seconds=None, decision_id=None
    ):
        self.core.check_open()
        if isinstance(ref, HumanRequest):
            ref = ref.ref
        if not isinstance(ref, RequestRef):
            raise ValueError("Expected RequestRef with target and revision.")
        decision_id = decision_id or "decision_" + uuid.uuid4().hex
        stable_id("decision", ref.id, decision_id)
        if ref.target.kind == "run" and ref.target.owner_id:
            service = self.core.services.get(ref.target.owner_id, control=True)
            action = (
                "answer"
                if decision == "answer"
                else "renew_tests"
                if runs is not None or seconds is not None
                else decision
            )
            values = {}
            if answer is not None:
                values["text"] = answer
            if runs is not None:
                values["runs"] = runs
            if seconds is not None:
                values["seconds"] = seconds
            return service.decide(ref, action, decision_id=decision_id, **values)
        if ref.durable and ref.target.kind != "run":
            raise ValueError("Durable decisions require a Run request reference.")
        if not ref.durable and ref.target.kind not in {"session", "turn"}:
            raise ValueError(
                "Immediate decisions require a Session or turn request reference."
            )
        key = digest([ref.target.data(), ref.id, ref.revision, decision_id])
        payload = {
            "decision": decision,
            "answer": answer,
            "runs": runs,
            "seconds": seconds,
        }
        prior = self.core.store.get("api_decisions", key)
        if prior:
            if prior["payload"] != payload:
                raise StaleReference(
                    "Decision ID already has another payload.", ref=ref.target
                )
            if ref.durable or prior["result"].get("delivery") == "delivered":
                return prior["result"]
        request = next((r for r in self.list(target=ref.target) if r.ref == ref), None)
        if ref.durable:
            row = HumanRequests(self.core.run_store).get(ref.target.id, ref.id)
            if row.get("decision_from") != ref.revision and request is None:
                raise StaleReference(
                    "Request revision changed or no longer waits for a decision.",
                    ref=ref.target,
                )
            if (
                decision == "answer"
                and row["kind"] != "attention"
                or runs is not None
                and row["kind"] != "test_grant"
            ):
                raise ValueError("Decision type does not match the request kind.")
            result = HumanRequests(self.core.run_store).decide(
                ref.target.id,
                ref.id,
                ref.revision,
                decision,
                answer,
                runs=runs,
                seconds=seconds,
            )
        else:
            if request is None:
                if prior:
                    return {**prior["result"], "delivery": "unknown"}
                raise StaleReference(
                    "Instantaneous request expired or belongs to an old turn.",
                    ref=ref.target,
                )
            if (
                decision not in {"approve", "deny"}
                or answer is not None
                or runs is not None
                or seconds is not None
            ):
                raise ValueError("Instantaneous tool requests require approve or deny.")
            sid = ref.target.id if ref.target.kind == "session" else ref.target.owner_id
            result = {"request_id": ref.id, "state": decision, "delivery": "accepted"}

            def accepted():
                self.core.store.put(
                    "api_decisions",
                    key,
                    {
                        "payload": payload,
                        "result": result,
                        "checkpoint": self._checkpoint(ref.target),
                    },
                )

            try:
                self._session(sid)[1].respond(
                    ref.id,
                    decision == "approve",
                    revision=ref.revision,
                    before=accepted,
                )
            except ValueError as error:
                raise StaleReference(str(error), ref=ref.target) from error
            result = {**result, "delivery": "delivered"}
        self.core.store.put(
            "api_decisions",
            key,
            {
                "payload": payload,
                "result": result,
                "checkpoint": self._checkpoint(ref.target),
            },
        )
        self.core.changed.set()
        return result

    def _checkpoint(self, target):
        if target.kind == "run":
            return self.core.run_store.get(target.id).get("checkpoint")
        return "lead:" + (target.owner_id or target.id)

    def approve(self, ref, *, decision_id=None):
        return self._decide(ref, "approve", decision_id=decision_id)

    def deny(self, ref, *, decision_id=None):
        return self._decide(ref, "deny", decision_id=decision_id)

    def answer(self, ref, *, text, decision_id=None):
        return self._decide(ref, "answer", answer=text, decision_id=decision_id)

    def renew_tests(self, ref, *, runs, seconds, decision_id=None):
        return self._decide(
            ref, "approve", runs=runs, seconds=seconds, decision_id=decision_id
        )
