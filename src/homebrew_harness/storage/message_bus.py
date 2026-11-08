"""Private durable mailboxes with explicit claim/ACK and thread notification."""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path


def validate_agent_name(name: str, *, teammate: bool = False) -> str:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        raise ValueError(
            "Agent name requires 1..64 ASCII letters, digits, underscores or dashes."
        )
    if teammate and name.lower() in {"lead", "agent"}:
        raise ValueError("lead and agent are reserved identities.")
    return name


class MessageBus:
    """Durable claim/ACK; read_inbox explicitly claims and acknowledges messages."""

    def __init__(self, workdir: Path, *, store=None):
        from homebrew_harness.storage.durable_runtime import DurableStore

        self.store = store or DurableStore(workdir)
        self.changed = threading.Condition(threading.RLock())
        self.run_id = None
        self.consumer = __import__("uuid").uuid4().hex

    def send(
        self,
        sender,
        recipient,
        content,
        msg_type="message",
        metadata=None,
        *,
        message_id=None,
    ):
        import uuid

        from homebrew_harness.security.data_boundary import redact

        validate_agent_name(sender)
        validate_agent_name(recipient)
        if (
            not isinstance(content, str)
            or not isinstance(msg_type, str)
            or (metadata is not None and not isinstance(metadata, dict))
        ):
            raise ValueError("Invalid message payload.")
        from homebrew_harness.runs.controller import current_run

        active = current_run()
        mid = message_id or uuid.uuid4().hex
        record = redact(
            {
                "id": mid,
                "from": sender,
                "to": recipient,
                "content": content,
                "type": msg_type,
                "metadata": {
                    **(metadata or {}),
                    **(
                        {"run_id": self.run_id or active.id}
                        if self.run_id or active is not None
                        else {}
                    ),
                },
                "ts": time.time(),
            }
        )
        self.store.mailbox("send", recipient, records=[record])
        self.wake()
        return mid

    def peek(self, name):
        validate_agent_name(name)
        return (
            bool(self._scoped(name))
            if self.run_id
            else self.store.mailbox("peek", name)
        )

    def available(self, name):
        validate_agent_name(name)
        return (
            bool(self._scoped(name, pending=True))
            if self.run_id
            else self.store.mailbox("available", name)
        )

    def claim(self, name, owner=None):
        validate_agent_name(name)
        if self.run_id:
            return self._scoped(
                name, pending=True, action="claim", owner=owner or self.consumer
            )
        if not self.store.mailbox("available", name):
            return []
        return self.store.mailbox("claim", name, owner=owner or self.consumer)

    def recover(self, name, owner=None):
        if self.run_id:
            return self._scoped(name, action="recover")
        return self.store.mailbox("recover", name, owner=owner or self.consumer)

    def proposal_recipient(self, name):
        return self.run_id + ":" + name if self.run_id else name

    def _scoped(self, name, *, pending=False, action=None, owner=None):
        import json
        from contextlib import nullcontext
        from homebrew_harness.storage.storage_gate import storage_gate

        db = self.store.backend()
        if not db.exists():
            return []
        with storage_gate(self.store.workdir) if action else nullcontext():
            with db.write() if action else db.connection() as conn:
                rows = conn.execute(
                    "SELECT id,record FROM inbox WHERE recipient=?"
                    + (" AND state='pending'" if pending else ""),
                    (name,),
                ).fetchall()
                selected = [
                    (identity, json.loads(raw))
                    for identity, raw in rows
                    if json.loads(raw).get("metadata", {}).get("run_id") == self.run_id
                ]
                if action == "claim":
                    conn.executemany(
                        "UPDATE inbox SET state='inflight',owner=? WHERE recipient=? AND id=? AND state='pending'",
                        [(owner, name, key) for key, _ in selected],
                    )
                elif action == "recover":
                    conn.executemany(
                        "UPDATE inbox SET state='pending',owner=NULL WHERE recipient=? AND id=? AND state='inflight'",
                        [(name, key) for key, _ in selected],
                    )
                return [record for _, record in selected]

    def acknowledge(self, name, records, owner=None):
        return self.store.mailbox(
            "ack", name, owner=owner or self.consumer, records=records
        )

    def release(self, name, records, owner=None):
        result = self.store.mailbox(
            "release", name, owner=owner or self.consumer, records=records
        )
        self.wake()
        return result

    def read_inbox(self, name):
        records = self.claim(name)
        self.acknowledge(name, records)
        return records

    def wait_for_messages(self, name, timeout=0.2, *, durable=False):
        with self.changed:
            self.changed.wait_for(lambda: self.peek(name), timeout)
        return self.claim(name) if durable else self.read_inbox(name)

    def wake(self):
        with self.changed:
            self.changed.notify_all()
