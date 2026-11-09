"""Persistent query references on the existing Session input and checkpoint loop."""

import uuid
from dataclasses import asdict

from homebrew_harness.runtime.input_router import ApprovalBroker, approval_scope
from homebrew_harness.runtime.output import output_scope
from homebrew_harness.runtime.capabilities import Capabilities
from homebrew_harness.runtime.task_routing import TaskRoute
from homebrew_harness.execution.execution_control import cleanup_scope
from homebrew_harness.storage.api_state import stable_id, cancel_inputs
from .base import Handle, host_operation
from .config import SessionOptions
from .types import (
    ExecutionRef,
    ExecutionSnapshot,
    ExecutionResult,
    SubmissionReceipt,
    StaleReference,
    SDKError,
    UnsupportedOperation,
)


class TurnHandle(Handle):
    def submission(self):
        row = self.core.store.get("api_turns", self.id)
        if not row:
            raise StaleReference("Unknown query reference.", ref=self.ref)
        return SubmissionReceipt(
            self.ref,
            row["request_id"],
            True,
            "started" if row["turn_id"] else "accepted",
        )

    def snapshot(self):
        row = self.core.store.get("api_turns", self.id)
        if not row or row["session_id"] != self.ref.owner_id:
            raise StaleReference("Unknown query reference.", ref=self.ref)
        session = self.core.sessions.live.get(row["session_id"])
        requests = self.requests()
        state = row["state"]
        if requests and state == "running":
            state = "waiting_input"
        if row["cancel_requested"] and state not in {
            "cancelled",
            "completed",
            "failed",
        }:
            state = "cancelling"
        pending = state in {"queued", "running", "waiting_input", "cancelling"}
        if session is not None and state in {"completed", "failed", "cancelled"}:
            native = session.runtime
            pending = bool(
                row.get("turn_id") is not None
                and native.inputs.active_id == row["turn_id"]
                or getattr(native, "_api_turn_id", None) == self.id
                and native._user_waiting.is_set()
                or native.gateway.inflight_cancel(
                    native.inputs.api_cancel_events.get(self.id)
                )
            )
            pending = (
                pending
                or getattr(native, "_api_pending_turn_id", None) == self.id
                and bool(native._api_pending_work())
            )
        reason = row.get("error") or ""
        if session is None and state in {"running", "cancelling"}:
            state = "unattached"
            reason = "Resume the owning Session and inspect its retained checkpoint."
        elif (
            session is not None
            and self.id in session.recovered_ids
            and state == "running"
            and not session.runtime.inputs.active_id
            and not session.runtime._user_waiting.is_set()
            and getattr(session.runtime, "_api_pending_turn_id", None) != self.id
        ):
            state = "blocked"
            reason = "Interrupted query has no confirmed final result. Inspect recovery before submitting an explicit continuation."
        return ExecutionSnapshot(
            self.ref,
            state,
            row["revision"],
            reason,
            row.get("input_revision", 0),
            tuple(requests),
            row.get("confirmed", False)
            and row["state"] in {"completed", "failed", "cancelled", "blocked"}
            and (row.get("output") is not None or state in {"cancelled", "failed"}),
            row.get("confirmed", False),
            pending,
            {"scope": "turn", "session_retained": True},
            {"turn_id": row["turn_id"], "request_id": row["request_id"]},
        )

    def result(self):
        self._require_result()
        row = self.core.store.get("api_turns", self.id)
        return ExecutionResult(
            self.ref,
            row["state"],
            row.get("output"),
            error=row.get("error"),
            evidence={"checkpoint": row["checkpoint"]},
        )

    def steer(self, text, *, request_id=None, target="lead"):
        session = self.core.sessions.live.get(self.ref.owner_id)
        if session is None or session.runtime._closed:
            raise StaleReference(
                "Resume the owning Session before steering.", ref=self.ref
            )
        return session._amend(self, text, "steer", request_id, target)

    def interrupt(self, text, *, request_id=None):
        session = self.core.sessions.live.get(self.ref.owner_id)
        if session is None or session.runtime._closed:
            raise StaleReference(
                "Resume the owning Session before interrupting.", ref=self.ref
            )
        return session._amend(self, text, "interrupt", request_id, "lead")

    @host_operation
    def cancel(self):
        session = self.core.sessions.live.get(self.ref.owner_id)
        if session is None or session.runtime._closed:
            raise StaleReference(
                "Only the owning live Session can cancel this query.", ref=self.ref
            )
        runtime = session.runtime
        with (
            runtime.inputs.lock,
            cleanup_scope(),
            self.core.store.backend().write() as conn,
        ):
            db = self.core.store.backend()
            row = db._get(conn, "api_turns", self.id)
            if not row or row["session_id"] != runtime.session_id:
                raise StaleReference("Query owner changed.", ref=self.ref)
            if row["state"] in {"completed", "cancelled", "failed"}:
                return self.snapshot()
            if (
                row["turn_id"] is not None
                and row["turn_id"] != runtime.inputs.active_id
            ):
                raise StaleReference("The active query changed.", ref=self.ref)
            row = {**row, "cancel_requested": True, "revision": row["revision"] + 1}
            cancel_inputs(db, conn, runtime.inputs.recipient, {self.id})
            if row["turn_id"] is None:
                row.update(
                    state="cancelled",
                    confirmed=True,
                    error="Query cancelled before dispatch.",
                )
                conn.execute(
                    "DELETE FROM inbox WHERE recipient=? AND id=?",
                    (runtime.inputs.recipient, self.id),
                )
            else:
                db._put(
                    conn,
                    "input_epochs",
                    runtime.inputs.recipient,
                    db._get(conn, "input_epochs", runtime.inputs.recipient, 0) + 1,
                )
            db._put(conn, "api_turns", self.id, row)
        if row["turn_id"]:
            runtime.inputs.cancel.set()
            session.broker.supersede_turn(row["turn_id"])
        self.core.changed.set()
        return self.snapshot()


class SessionHandle(Handle):
    def __init__(self, core, runtime, options):
        super().__init__(core, ExecutionRef("session", runtime.session_id), owned=True)
        self.runtime, self.options = runtime, options
        self.recovered_ids = set()
        self.broker = ApprovalBroker(
            trusted=True, owner=runtime, announce=lambda _text: core.changed.set()
        )

    @host_operation
    def submit(self, text, *, request_id=None, goal=None):
        self.core.check_open()
        if goal is not None:
            from homebrew_harness.orchestration.hard_conditions import validate_spec

            goal = validate_spec(goal)
        request_id = request_id or "sdk_" + uuid.uuid4().hex
        identity = "usr_" + stable_id("turn", self.id, request_id)
        with output_scope(), approval_scope(self.broker):
            self.runtime.submit_user_input(
                text,
                mode="queued",
                input_id=identity,
                literal=True,
                sdk_turn=True,
                request_id=request_id,
                goal=goal,
            )
        self.core.changed.set()
        return TurnHandle(
            self.core, ExecutionRef("turn", identity, self.id), owned=True
        )

    def execute(self, text, *, timeout=None, request_id=None, goal=None):
        return self.submit(text, request_id=request_id, goal=goal).wait(timeout)

    @host_operation
    def approve_test(self, name):
        return self._test_control("approve", name)

    @host_operation
    def revoke_test(self, name):
        return self._test_control("revoke", name)

    def _test_control(self, action, name):
        if not isinstance(name, str) or not name:
            raise ValueError("A registered test name is required.")
        from homebrew_harness.runtime.host_commands import HostCommand

        with output_scope():
            return self.runtime.control(
                HostCommand("", "tests", action, name, mode="manage")
            )

    def goal_status(self):
        return self.runtime.goal.status()

    def recovery_status(self):
        return self.runtime.recovery_status()

    @host_operation
    def continue_recovery(self):
        from homebrew_harness.runtime.host_commands import HostCommand

        with output_scope():
            return self.runtime.control(
                HostCommand("", "recovery", "continue", mode="manage")
            )

    @host_operation
    def _amend(self, turn, text, mode, request_id, target):
        row = self.core.store.get("api_turns", turn.id)
        if not row or row["session_id"] != self.id:
            raise StaleReference("Steering belongs to another Session.", ref=turn.ref)
        request_id = request_id or "sdk_" + uuid.uuid4().hex
        identity = "usr_" + stable_id(mode, turn.id, request_id)
        with output_scope(), approval_scope(self.broker):
            try:
                receipt = self.runtime.inputs.submit(
                    text,
                    mode=mode,
                    target=target,
                    input_id=identity,
                    literal=True,
                    expected_turn_id=row["turn_id"],
                    target_input_id=turn.id,
                )
            except ValueError as error:
                if (
                    "target" in str(error).lower()
                    or "turn changed" in str(error).lower()
                ):
                    raise StaleReference(str(error), ref=turn.ref) from error
                raise
        self.core.changed.set()
        return SubmissionReceipt(turn.ref, request_id, True, receipt["state"])

    def snapshot(self):
        status = self.runtime.shutdown_status()
        return ExecutionSnapshot(
            self.ref,
            status["state"],
            input_revision=self.runtime.inputs.revision,
            persistence_confirmed=status["persistence_ok"],
            execution_pending=self.runtime.inputs.active_id is not None,
            cleanup=status,
            details=self.runtime.input_status(),
        )

    def close(self):
        db = self.core.store.backend()
        with self.runtime.inputs.lock, cleanup_scope(), db.write() as conn:
            for row in self.core.store.items("api_turns").values():
                if row["session_id"] != self.id or row["state"] in {
                    "completed",
                    "failed",
                    "cancelled",
                }:
                    continue
                current = db._get(conn, "api_turns", row["id"])
                value = {
                    **current,
                    "cancel_requested": True,
                    "revision": current["revision"] + 1,
                }
                if value["turn_id"] is None or (
                    getattr(self.runtime, "_api_turn_id", None) != value["id"]
                    and getattr(self.runtime, "_api_pending_turn_id", None)
                    != value["id"]
                    and self.runtime.inputs.active_id != value["turn_id"]
                ):
                    value.update(
                        state="cancelled",
                        confirmed=True,
                        error="Owning Session closed before dispatch.",
                    )
                cancel_inputs(db, conn, self.runtime.inputs.recipient, {value["id"]})
                db._put(conn, "api_turns", row["id"], value)
        self.broker.close()
        with output_scope():
            self.runtime.close()
        return self.runtime.shutdown_status()

    def wait_closed(self, timeout=5):
        return self.runtime.wait_closed(timeout)

    def __enter__(self):
        return self

    def __exit__(self, kind, error, tb):
        self.close()
        self.core.finish_scope(self, kind)


class Sessions:
    def __init__(self, core):
        self.core, self.live = core, {}

    @host_operation
    def open(self, options=None, *, resume=None):
        self.core.check_open()
        options = options or SessionOptions()
        if not isinstance(options, SessionOptions):
            raise ValueError("Expected SessionOptions.")
        settings = options.settings(self.core.config.settings)
        enabled = set(options.enabled) | ({"mcp"} if options.mcp else set())
        capabilities = Capabilities(enabled, settings.memory_mode)
        if capabilities.enabled - set(settings.allowed):
            raise SDKError(
                "Session capabilities exceed the host catalog.",
                code="capability_unavailable",
            )
        with output_scope():
            builder = self.core.config.builder(self.core.workdir, settings=settings)
            try:
                runtime = builder.session(
                    TaskRoute(
                        capabilities=tuple(capabilities.enabled), mcp=options.mcp
                    ),
                    resume=resume,
                    hooks=self.core.config.components.hooks,
                )
            finally:
                builder.close()
            handle = SessionHandle(self.core, runtime, options)
            provisional = handle.id
            self.live[provisional] = handle
            try:
                runtime._ensure_runtime()
                if options.mcp:
                    from homebrew_harness.tools.mcp.selection import prepare_runtime

                    runtime.mcp.expected_manifest = prepare_runtime(
                        runtime.mcp, workdir=runtime.workdir
                    )
                    runtime.mcp.prepared = True
            except BaseException:
                handle.close()
                if handle.wait_closed(0):
                    self.live.pop(provisional, None)
                raise
        self.live.pop(provisional, None)
        handle.ref = ExecutionRef("session", runtime.session_id)
        if resume:
            handle.recovered_ids = {
                row["id"]
                for row in self.core.store.items("api_turns").values()
                if row["session_id"] == runtime.session_id
                and row["state"] in {"running", "cancelling"}
            }
        self.live[handle.id] = handle
        self.core.store.put(
            "api_sessions",
            handle.id,
            {
                "id": handle.id,
                "options": asdict(options),
                "config": self.core.config.identity(),
                "checkpoint": runtime.checkpoint_key,
            },
        )
        return handle

    def get(self, identity):
        if identity in self.live:
            return self.live[identity]
        row = self.core.store.get("api_sessions", identity)
        if not row:
            raise StaleReference("Unknown SDK Session.")
        return SessionView(self.core, identity)

    @host_operation
    def resume(self, identity):
        handle = self.live.get(identity)
        if handle is not None and not handle.runtime._closed:
            return handle
        row = self.core.store.get("api_sessions", identity)
        if not row:
            raise StaleReference("Unknown SDK Session.")
        return self.open(SessionOptions(**row["options"]), resume=identity)

    def turn(self, identity):
        row = self.core.store.get("api_turns", identity)
        if not row:
            raise StaleReference("Unknown SDK query.")
        return TurnHandle(self.core, ExecutionRef("turn", identity, row["session_id"]))

    def list(self):
        return tuple(self.core.store.items("api_sessions").values())


class SessionView(Handle):
    def __init__(self, core, identity):
        super().__init__(core, ExecutionRef("session", identity))

    def snapshot(self):
        meta = self.core.store.get("session_meta", self.id, {})
        return ExecutionSnapshot(
            self.ref,
            "closed" if meta.get("close_complete") else "unattached",
            meta.get("revision", 0),
            persistence_confirmed=meta.get("close_complete", False),
            details={"lifecycle": meta.get("lifecycle", "retained")},
        )

    def resume(self):
        return self.core.sessions.resume(self.id)

    def submit(self, *_args, **_kw):
        raise UnsupportedOperation(
            "Resume the Session explicitly before submitting.", ref=self.ref
        )

    def close(self):
        pass

    def wait_closed(self, timeout=5):
        return True
