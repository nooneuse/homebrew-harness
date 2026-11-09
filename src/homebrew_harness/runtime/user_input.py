"""Host-only durable input; one Lead applies changes at closed boundaries."""

import copy
import threading
import time
import uuid
from contextlib import contextmanager

from homebrew_harness.execution.execution_control import cleanup_scope
from homebrew_harness.runtime.host_commands import HostCommand
from homebrew_harness.storage.durable_runtime import active_writer
from homebrew_harness.storage.control_boundary import ProposalSuperseded
from homebrew_harness.context.task_intent import TaskIntent


class UserInputController:
    def __init__(self, session):
        self.session = session
        self.consumer = "input_" + uuid.uuid4().hex
        self.lock = threading.RLock()
        self.active_id = None
        self.api_cancel_events = {}
        self.cancel = threading.Event()
        self.phase = "idle"
        self.revision = 0
        self.turn_texts = []
        self.changed = threading.Event()

    @property
    def recipient(self):
        if getattr(self.session, "run_control", None):
            return "user_" + self.session.run_control.id
        return "user_" + self.session.session_id

    def data(self):
        with self.lock:
            return {"revision": self.revision}

    def restore(self, value):
        with self.lock:
            self.revision = value["revision"]
        self.session.runtime_store.mailbox("recover", self.recipient)
        self.session.runtime_store.put("input_turns", self.recipient, None)

    @contextmanager
    def turn(self):
        with self.lock:
            self.active_id = "turn_" + uuid.uuid4().hex
            self.cancel = threading.Event()
            self.phase = "starting"
            self.turn_texts = []
            turn_id = self.active_id
            db = self.session.runtime_store.backend()
            try:
                with db.write() as conn:
                    db._put(conn, "input_turns", self.recipient, turn_id)
                    from homebrew_harness.storage.api_state import bind_turn

                    bind_turn(
                        db,
                        conn,
                        getattr(self.session, "_api_turn_id", None),
                        self.session.session_id,
                        turn_id,
                    )
            except BaseException:
                self.active_id = None
                self.phase = "idle"
                raise
        query = getattr(self.session, "_api_turn_id", None)
        if query:
            self.api_cancel_events = {
                identity: event
                for identity, event in self.api_cancel_events.items()
                if self.session.gateway.inflight_cancel(event)
            }
            self.api_cancel_events[query] = self.cancel
        try:
            yield self
        finally:
            with self.lock, cleanup_scope():
                if self.active_id == turn_id:
                    self.session.runtime_store.put("input_turns", self.recipient, None)
                    self.active_id = None
                    self.phase = "idle"
            self.changed.set()

    def submit(
        self,
        text,
        *,
        mode="steer",
        target="lead",
        expected_turn_id=None,
        input_id=None,
        literal=False,
        sdk_turn=False,
        request_id=None,
        goal=None,
        target_input_id=None,
    ):
        managed = getattr(self.session, "run_control", None)
        if managed is not None:
            if target != "lead":
                raise ValueError("Managed team supervision is not enabled.")
            receipt = managed.store.submit(
                managed.id,
                text,
                mode=mode,
                expected_turn_id=expected_turn_id,
                input_id=input_id,
            )
            if mode == "interrupt" and self.active_id is not None:
                self.cancel.set()
            return {**receipt, "mode": mode, "target": target}
        if not isinstance(text, str) or not text.strip() or len(text) > 64000:
            raise ValueError(
                "User input must be nonempty text within 64,000 characters."
            )
        if mode not in {"steer", "queued", "interrupt"}:
            raise ValueError("User input mode must be steer, queued or interrupt.")
        command = HostCommand(text) if literal else HostCommand.parse(text)
        if command.name and mode != "queued":
            raise ValueError(
                "Host control commands use queued input; they cannot become model steering text."
            )
        with self.lock:
            if self.session._closed:
                raise ValueError("Session is closing; no new user input is accepted.")
            if expected_turn_id is not None and expected_turn_id != self.active_id:
                raise ValueError("The targeted turn changed; refresh the input status.")
            if target != "lead" and (
                not self.session.team or target not in self.session.team.members
            ):
                raise ValueError(
                    "User input target must be Lead or an existing teammate."
                )
            if mode == "interrupt" and target != "lead":
                raise ValueError(
                    "Interrupt targets the active Lead work; use steer or explicit shutdown for teammates."
                )
            identity = input_id or "usr_" + uuid.uuid4().hex
            import re

            if not isinstance(identity, str) or not re.fullmatch(
                r"usr_[0-9a-f]{32}", identity
            ):
                raise ValueError("Invalid stable user input ID.")
            if len(self.pending()) >= 64:
                raise ValueError(
                    "User input queue is full; wait for accepted inputs to be consumed."
                )
            record = {
                "id": identity,
                "checkpoint": self.session.checkpoint_key,
                "from": "user",
                "to": self.recipient,
                "type": "user_input",
                "content": text,
                "ts": time.time(),
                "metadata": {
                    "origin": "host_user",
                    "mode": mode,
                    "target": target,
                    "turn_id": self.active_id,
                    **({"literal": True} if literal else {}),
                    **(
                        {
                            "sdk_turn": identity,
                            "sdk_request_id": request_id,
                            "sdk_goal": goal,
                        }
                        if sdk_turn
                        else {}
                    ),
                    **({"target_input_id": target_input_id} if target_input_id else {}),
                },
            }
            accepted_turn = self.session.runtime_store.submit_input(
                record, expected_turn_id
            )
            if mode == "interrupt" and accepted_turn is not None:
                self.cancel.set()
            self.changed.set()
            self.session._drain_changed.set()
            if mode != "queued" and target == "lead" and accepted_turn is not None:
                from homebrew_harness.runtime.input_router import current_broker

                broker = current_broker()
                if broker is not None and broker.owner is self.session:
                    broker.supersede_turn(self.active_id)
        return {"input_id": identity, "state": "queued", "mode": mode, "target": target}

    def pending(self, *, active_only=False):
        with cleanup_scope():
            values = self.session.runtime_store.mailbox("inspect", self.recipient)
        query = getattr(self.session, "_api_turn_id", None) or getattr(
            self.session, "_api_pending_turn_id", None
        )
        return [
            r
            for r in values
            if not active_only
            or r["metadata"]["mode"] != "queued"
            and (
                not r["metadata"].get("target_input_id")
                or r["metadata"]["target_input_id"] == query
            )
        ]

    def status(self):
        with self.lock:
            return {
                "active_turn_id": self.active_id,
                "phase": self.phase,
                "revision": self.revision,
                "pending": [
                    {
                        "input_id": r["id"],
                        "mode": r["metadata"]["mode"],
                        "target": r["metadata"]["target"],
                    }
                    for r in self.pending()
                ],
                "applied": [
                    {k: v for k, v in r.items() if k not in {"record", "checkpoint"}}
                    for r in self.applied[-20:]
                ],
                "results": [
                    {k: v for k, v in r.items() if k not in {"record", "checkpoint"}}
                    for r in self.session.runtime_store.items(
                        "input_receipts", owner=self.session.checkpoint_key
                    ).values()
                ][-64:],
            }

    @property
    def applied(self):
        values = self.session.runtime_store.items(
            "input_receipts", owner=self.session.checkpoint_key
        ).values()
        return sorted(
            (r for r in values if r["state"] in {"applied", "delivered"}),
            key=lambda r: r["revision"],
        )[-256:]

    def token(self):
        return self.session.runtime_store.input_guard(self.recipients)

    @property
    def recipients(self):
        managed = getattr(self.session, "run_control", None)
        return (
            [self.recipient, "reference_" + managed.id] if managed else [self.recipient]
        )

    def superseded(self, token=None):
        return (
            bool(self.pending(active_only=True))
            or token is not None
            and token != self.token()
        )

    def apply(self, messages, todo, compactor):
        if self.session._closed or not self.pending(active_only=True):
            return False
        records = self.session.runtime_store.mailbox(
            "claim", self.recipient, owner=self.consumer
        )
        chosen = [
            r
            for r in records
            if r["metadata"]["mode"] != "queued"
            and (
                not r["metadata"].get("target_input_id")
                or r["metadata"]["target_input_id"]
                == getattr(self.session, "_api_turn_id", None)
            )
        ]
        self.release([r for r in records if r not in chosen])
        if not chosen:
            return False
        try:
            self.commit_records(chosen, messages, todo, compactor, append=True)
            return True
        except BaseException:
            self.release(chosen)
            raise

    def commit_records(self, records, messages, todo, compactor, *, append):
        writer = active_writer()
        if writer is None:
            raise RuntimeError("User input requires its owning checkpoint writer.")
        staged = list(messages)
        intent = TaskIntent()
        intent.restore(self.session.intent.to_dict())
        local_intent = TaskIntent()
        local_intent.restore(compactor.intent.to_dict())
        revision, additions, receipts, deliveries = self.revision, [], [], []
        for record in records:
            meta = record["metadata"]
            if record.get("from") != "user" or meta.get("origin") != "host_user":
                raise ValueError("Untrusted input cannot amend the user task.")
            existing = self.session.runtime_store.get("input_receipts", record["id"])
            if existing and existing["state"] in {"applied", "delivered"}:
                continue
            text, target = record["content"], meta["target"]
            amendment = (
                text if target == "lead" else "For teammate " + target + ": " + text
            )
            revision += 1
            if append:
                intent.update(amendment)
                local_intent.update(amendment)
                staged.append(
                    {
                        "role": "user",
                        "content": amendment,
                        "user_input_id": record["id"],
                    }
                )
            else:
                staged[-1] = {**staged[-1], "user_input_id": record["id"]}
                if target != "lead":
                    staged[-1]["content"] = amendment
            additions.append(amendment)
            receipts.append(
                {
                    "input_id": record["id"],
                    "checkpoint": writer.key,
                    "revision": revision,
                    "state": "applied" if target == "lead" else "delivered",
                    "target": target,
                    "record": record,
                }
            )
            if target != "lead":
                deliveries.append(
                    (
                        target,
                        {
                            "id": record["id"],
                            "from": "lead",
                            "to": target,
                            "type": "user_steer",
                            "content": text,
                            "ts": record["ts"],
                            "metadata": {
                                "origin": "host_user",
                                **(
                                    {"run_id": self.session.run_control.id}
                                    if self.session.run_control
                                    else {}
                                ),
                                "input_id": record["id"],
                                "revision": revision,
                                "checkpoint": writer.key,
                            },
                        },
                    )
                )
        boundary = {"receipts": receipts, "deliveries": deliveries}
        goal = self.session.goal
        goal_state = None
        if goal.pending:
            goal_state = {
                **copy.deepcopy(goal.state),
                "task_intent": intent.to_dict(),
                "revision": goal.state["revision"] + 1,
            }
            boundary.update(goal=goal_state, goal_expected=goal.state["revision"])
        ack = [(self.recipient, self.consumer, [r["id"] for r in records])]
        commit_id = uuid.uuid4().hex
        with cleanup_scope():
            try:
                writer.save(
                    ack=ack,
                    boundary=boundary,
                    overrides={
                        "messages": staged,
                        "intent": intent.to_dict(),
                        "context_intent": local_intent.to_dict(),
                        "input_control": {"revision": revision},
                        "boundary_id": commit_id,
                    },
                )
            except BaseException:
                # COMMIT may have succeeded before the caller lost its result.
                if (
                    self.session.runtime_store.get("checkpoints", writer.key, {}).get(
                        "boundary_id"
                    )
                    != commit_id
                ):
                    raise
        messages[:] = staged
        self.session.intent.restore(intent.to_dict())
        compactor.intent.restore(local_intent.to_dict())
        self.session.user_request = self.session.intent.original
        with self.lock:
            self.revision = revision
            self.turn_texts.extend(additions)
            if self.cancel.is_set():
                self.cancel = threading.Event()
        if goal_state is not None:
            goal.accept_candidate(goal_state)
        if deliveries:
            try:
                self.session.team.bus.wake()
            except Exception:
                pass  # Durable recipients also poll; wakeup is not the delivery commit.
        if append:
            for text in additions:
                self.session.hooks.trigger_hooks(
                    "UserPromptSubmit", text, self.session.workspace.lead_cwd()
                )

    def commit_end(self, messages, todo, compactor, token, goal=None, candidate=None):
        decision, state, expected = candidate if candidate else (None, None, None)
        ended = decision is None or decision.action != "block"
        boundary = {"guard": token}
        if ended:
            boundary["end_turn"] = (self.recipient, self.active_id)
            identity = getattr(self.session, "_api_turn_id", None)
            if identity:
                output = next(
                    (
                        m.get("content", "")
                        for m in reversed(messages)
                        if m.get("role") == "assistant" and not m.get("tool_calls")
                    ),
                    "",
                )
                boundary["api_turn"] = {
                    "id": identity,
                    "session_id": self.session.session_id,
                    "turn_id": self.active_id,
                    "state": "running"
                    if self.session._api_pending_work() or self.session.goal.pending
                    else "completed",
                    "output": output,
                    "input_revision": self.revision,
                }
        if state is not None:
            state = {**state, "revision": expected + 1}
            boundary.update(goal=state, goal_expected=expected)
        writer = active_writer()
        commit_id = uuid.uuid4().hex
        with self.lock, cleanup_scope():
            try:
                writer.messages, writer.todo, writer.intent = (
                    messages,
                    todo,
                    compactor.intent,
                )
                writer.save(
                    "executed" if ended else "running",
                    boundary=boundary,
                    overrides={"boundary_id": commit_id},
                )
            except ProposalSuperseded:
                return False
            except BaseException:
                if (
                    self.session.runtime_store.get("checkpoints", writer.key, {}).get(
                        "boundary_id"
                    )
                    != commit_id
                ):
                    raise
            if state is not None:
                goal.accept_candidate(state)
            if ended:
                self.active_id = None
                self.phase = "idle"
        return True

    def fail(self, records, error):
        from homebrew_harness.execution.model_protocol import ProviderError

        db = self.session.runtime_store.backend()
        with cleanup_scope(), db.write() as conn:
            for record in records:
                old = db._get(conn, "input_receipts", record["id"], {})
                attempts = old.get("attempts", 0) + 1
                transient = isinstance(error, ProviderError) and error.transient
                retry = transient and attempts < 3
                value = {
                    "input_id": record["id"],
                    "checkpoint": self.session.checkpoint_key,
                    "record": record,
                    "state": "retry_wait"
                    if retry
                    else "failed"
                    if isinstance(error, ValueError) or transient
                    else "blocked",
                    "error": type(error).__name__,
                    "attempts": attempts,
                    "next_retry": time.time() + 2**attempts if retry else None,
                    "retryable": isinstance(error, ValueError) or transient,
                }
                db._put(conn, "input_receipts", record["id"], value)
                if retry:
                    conn.execute(
                        "UPDATE inbox SET state='pending',owner=NULL WHERE recipient=? AND id=? AND owner=?",
                        (self.recipient, record["id"], self.consumer),
                    )
                else:
                    db._ack(conn, self.recipient, self.consumer, [record["id"]])

    def manage(self, action, identity):
        if action == "retry":
            previous = self.session.runtime_store.get("input_receipts", identity)
            if (
                not previous
                or previous["state"] != "failed"
                or not previous.get("retryable")
            ):
                raise ValueError(
                    "Only a failed input with confirmed retry eligibility can be retried."
                )
            record = previous["record"]
            return self.submit(
                record["content"],
                mode=record["metadata"]["mode"],
                target=record["metadata"]["target"],
            )
        db = self.session.runtime_store.backend()
        with cleanup_scope(), db.write() as conn:
            row = conn.execute(
                "SELECT record FROM inbox WHERE recipient=? AND id=? AND state='pending'",
                (self.recipient, identity),
            ).fetchone()
            if row is None:
                previous = db._get(conn, "input_receipts", identity)
                if (
                    previous
                    and previous.get("checkpoint") == self.session.checkpoint_key
                    and previous.get("state") == "blocked"
                ):
                    db._put(
                        conn,
                        "input_receipts",
                        identity,
                        {**previous, "state": "cancelled"},
                    )
                    return {
                        "input_id": identity,
                        "state": "cancelled",
                        "effects": "Unchanged; recovery operations still require inspection.",
                    }
                raise ValueError(
                    "Only pending, unclaimed or blocked input can be cancelled."
                )
            import json

            record = json.loads(row[0])
            db._put(
                conn,
                "input_receipts",
                identity,
                {
                    "input_id": identity,
                    "checkpoint": self.session.checkpoint_key,
                    "record": record,
                    "state": "cancelled",
                },
            )
            db._ack(conn, self.recipient, None, [identity])
        return {"input_id": identity, "state": "cancelled"}

    def release(self, records):
        if records:
            with cleanup_scope():
                self.session.runtime_store.mailbox(
                    "release", self.recipient, owner=self.consumer, records=records
                )
