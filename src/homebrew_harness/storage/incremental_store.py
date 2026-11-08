"""SQLite immutable messages/components and versioned logical documents."""

import copy
import hashlib
import json
import uuid
from contextlib import contextmanager

from homebrew_harness.context.tracked_history import encoded_message
from homebrew_harness.runtime.runtime_state import RecoveryRequired
from homebrew_harness.storage.sqlite_store import SQLiteDatabase, encode

DYNAMIC_NAMES = {
    "task_intent",
    "inherited_task_constraints",
    "runtime_context",
    "goal_context",
    "memory_context",
    "memory_recall",
}
DOCUMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS inc_epoch(token TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS inc_messages(owner TEXT,id TEXT,value TEXT NOT NULL,PRIMARY KEY(owner,id));
CREATE TABLE IF NOT EXISTS inc_components(doc TEXT,name TEXT,revision INTEGER,value TEXT NOT NULL,hash TEXT NOT NULL,PRIMARY KEY(doc,name,revision));
CREATE TABLE IF NOT EXISTS inc_views(epoch TEXT,seq INTEGER,owner TEXT,message TEXT,PRIMARY KEY(epoch,seq),FOREIGN KEY(owner,message) REFERENCES inc_messages(owner,id));
CREATE INDEX IF NOT EXISTS inc_view_messages ON inc_views(owner,message);
CREATE TABLE IF NOT EXISTS inc_versions(doc TEXT,revision INTEGER,body TEXT NOT NULL,hash TEXT NOT NULL,PRIMARY KEY(doc,revision));
CREATE TABLE IF NOT EXISTS inc_heads(doc TEXT PRIMARY KEY,revision INTEGER NOT NULL,root INTEGER NOT NULL,FOREIGN KEY(doc,revision) REFERENCES inc_versions(doc,revision));
CREATE TABLE IF NOT EXISTS inc_edges(doc TEXT,revision INTEGER,target TEXT,target_revision INTEGER,
 PRIMARY KEY(doc,revision,target,target_revision),FOREIGN KEY(doc,revision) REFERENCES inc_versions(doc,revision) ON DELETE CASCADE,
 FOREIGN KEY(target,target_revision) REFERENCES inc_versions(doc,revision));
CREATE TABLE IF NOT EXISTS inc_results(id TEXT PRIMARY KEY,context TEXT NOT NULL,hash TEXT NOT NULL);
"""


def loads(raw):
    return json.loads(raw)


class IncrementalStore(SQLiteDatabase):
    def schema_statements(self):
        from homebrew_harness.storage.sqlite_store import SCHEMA

        statements = [
            statement.strip()
            for statement in (SCHEMA + DOCUMENT_SCHEMA).split(";")
            if statement.strip()
        ]
        statements.append("INSERT INTO inc_epoch VALUES('" + uuid.uuid4().hex + "')")
        return statements

    def __init__(self, root):
        super().__init__(root)
        self.cache = {}
        self.epoch = None

    @contextmanager
    def write(self):
        try:
            with super().write() as conn:
                yield conn
        except BaseException:
            self.cache.clear()
            raise

    def _meta(self, conn, doc, revision=None):
        row = conn.execute("SELECT token FROM inc_epoch").fetchall()
        if len(row) != 1:
            raise RecoveryRequired("Invalid incremental storage epoch.")
        if row[0][0] != self.epoch:
            self.cache.clear()
            self.epoch = row[0][0]
        if revision is None:
            row = conn.execute(
                "SELECT revision FROM inc_heads WHERE doc=?", (doc,)
            ).fetchone()
            if row is None:
                return None
            revision = row[0]
        cached = self.cache.get(doc)
        row = conn.execute(
            "SELECT body,hash FROM inc_versions WHERE doc=? AND revision=?",
            (doc, revision),
        ).fetchone()
        if row is None:
            raise RecoveryRequired("Incremental document version is missing.")
        if hashlib.sha256(row[0].encode()).hexdigest() != row[1]:
            raise RecoveryRequired("Incremental document manifest is corrupt.")
        if cached and cached["revision"] == revision:
            if cached["manifest_hash"] != row[1]:
                raise RecoveryRequired("Immutable document manifest was changed.")
            return cached
        meta = loads(row[0])
        meta["revision"] = revision
        meta["manifest_hash"] = row[1]
        self.metrics.add(reads=1, bytes_read=len(row[0].encode()))
        self.cache[doc] = meta
        return meta

    def revision(self, key):
        if not self.exists():
            return 0
        with self.connection() as conn:
            row = conn.execute(
                "SELECT revision FROM inc_heads WHERE doc=?", ("checkpoint:" + key,)
            ).fetchone()
            return row[0] if row else 0

    def _component(self, conn, doc, name, ref):
        row = conn.execute(
            "SELECT value,hash FROM inc_components WHERE doc=? AND name=? AND revision=?",
            (doc, name, ref["revision"]),
        ).fetchone()
        if (
            row is None
            or hashlib.sha256(row[0].encode()).hexdigest() != row[1]
            or row[1] != ref["hash"]
        ):
            raise RecoveryRequired("Incremental state component is missing or corrupt.")
        self.metrics.add(reads=1, bytes_read=len(row[0].encode()))
        return loads(row[0])

    def _core_ids(self, conn, meta):
        if "core_ids" in meta:
            return meta["core_ids"]
        rows = conn.execute(
            "SELECT seq,message FROM inc_views WHERE epoch=? AND seq<? ORDER BY seq",
            (meta["epoch"], meta["count"]),
        ).fetchall()
        if [r[0] for r in rows] != list(range(meta["count"])):
            raise RecoveryRequired("Incremental context sequence is incomplete.")
        meta["core_ids"] = [r[1] for r in rows]
        return meta["core_ids"]

    def _message(self, conn, owner, mid):
        row = conn.execute(
            "SELECT value FROM inc_messages WHERE owner=? AND id=?", (owner, mid)
        ).fetchone()
        if row is None or hashlib.sha256(row[0].encode()).hexdigest() != mid:
            raise RecoveryRequired("Persisted message is missing or corrupt.")
        self.metrics.add(reads=1, bytes_read=len(row[0].encode()), history_body_reads=1)
        return loads(row[0])

    def _load_doc(self, conn, doc, revision=None, seen=()):
        meta = self._meta(conn, doc, revision)
        if meta is None:
            return None
        identity = (doc, meta["revision"])
        if identity in seen or len(seen) > 32:
            raise RecoveryRequired("Invalid incremental document reference cycle.")
        seen = (*seen, identity)
        value = {}
        for name, ref in meta["fields"].items():
            if ref["kind"] == "value":
                value[name] = self._component(conn, doc, name, ref)
            elif ref["kind"] == "document":
                value[name] = self._load_doc(conn, ref["doc"], ref["revision"], seen)
            elif ref["kind"] == "members":
                value[name] = {
                    key: self._load_doc(conn, item["doc"], item["revision"], seen)
                    for key, item in ref["items"].items()
                }
            else:
                raise RecoveryRequired("Unknown incremental component type.")
        if meta["has_messages"]:
            ids = self._core_ids(conn, meta)
            headers = meta["headers"]
            size = len(ids) + len(headers)
            messages = [None] * size
            for position, mid in headers:
                if (
                    type(position) is not int
                    or not 0 <= position < size
                    or messages[position] is not None
                ):
                    raise RecoveryRequired("Invalid dynamic context layout.")
                messages[position] = self._message(conn, meta["owner"], mid)
            core = iter(ids)
            for position in range(size):
                if messages[position] is None:
                    messages[position] = self._message(conn, meta["owner"], next(core))
            value["messages"] = messages
        return value

    def _save_doc(
        self, conn, doc, value, owner, *, root=False, refs=None, expected=None
    ):
        if value.get("managed_run"):
            from homebrew_harness.runs.store import owner_guard

            owner_guard(self, conn, value["managed_run"])
        old = self._meta(conn, doc)
        old_revision = old["revision"] if old else 0
        if expected is not None and expected != old_revision:
            raise RecoveryRequired(
                "Checkpoint revision conflict; tool effects must not be replayed."
            )
        refs = refs or {}
        fields = {}
        edges = []
        for name, item in value.items():
            if name == "messages" or name in refs:
                continue
            raw = encode(item)
            hashed = hashlib.sha256(raw.encode()).hexdigest()
            previous = old["fields"].get(name) if old else None
            if previous and previous["kind"] == "value" and previous["hash"] == hashed:
                fields[name] = previous
                continue
            row = conn.execute(
                "SELECT max(revision) FROM inc_components WHERE doc=? AND name=?",
                (doc, name),
            ).fetchone()
            revision = (row[0] or 0) + 1
            conn.execute(
                "INSERT INTO inc_components VALUES(?,?,?,?,?)",
                (doc, name, revision, raw, hashed),
            )
            self.metrics.add(
                rows_written=1,
                bytes_written=len(raw.encode()),
                component_versions_created=1,
            )
            fields[name] = {"kind": "value", "revision": revision, "hash": hashed}
        for name, ref in refs.items():
            fields[name] = ref
            if ref["kind"] == "document":
                edges.append((ref["doc"], ref["revision"]))
            elif ref["kind"] == "members":
                edges.extend((v["doc"], v["revision"]) for v in ref["items"].values())
        has_messages = "messages" in value
        if has_messages and not isinstance(value["messages"], list):
            raise RecoveryRequired("Conversation messages must be a list.")
        old_core = self._core_ids(conn, old) if old else []
        from homebrew_harness.context.tracked_history import ConversationHistory
        from homebrew_harness.security.data_boundary import private_revision

        history = value.get("messages", [])
        delta = None
        if old and old.get("private_revision") == private_revision():
            if old.get("history") is history and isinstance(
                history, ConversationHistory
            ):
                delta = history.append_delta(old.get("history_revision", -1))
            elif getattr(history, "origin", None) is old.get("history") and getattr(
                history, "origin_revision", None
            ) == old.get("history_revision"):
                delta = getattr(history, "extra", None)
            elif (
                isinstance(history, ConversationHistory)
                and getattr(old.get("history"), "origin", None) is history
                and old["history"].version == old.get("history_revision")
            ):
                # record_result already persisted a virtual append. When the
                # actual loop appends that exact result object, consume it once.
                virtual = old["history"]
                appended = history.append_delta(virtual.origin_revision)
                expected = virtual.extra
                if (
                    appended is not None
                    and len(appended) >= len(expected)
                    and all(a is b for a, b in zip(appended, expected))
                ):
                    delta = appended[len(expected) :]
        if delta is not None and any(m.get("name") in DYNAMIC_NAMES for m in delta):
            delta = None
        core = list(old_core) if delta is not None else []
        headers = list(old["headers"]) if delta is not None else []
        payloads = {}
        iterable = (
            enumerate(history)
            if delta is None
            else (
                (len(old_core) + len(headers) + index, message)
                for index, message in enumerate(delta)
            )
        )
        for position, message in iterable:
            self.metrics.add(messages_examined=1)
            if not isinstance(message, dict):
                raise RecoveryRequired("Conversation message must be an object.")
            raw, mid = encoded_message(message)
            payloads[mid] = raw
            if message.get("name") in DYNAMIC_NAMES and message.get("role") in {
                "system",
                "developer",
                "user",
            }:
                headers.append([position, mid])
            else:
                core.append(mid)
        known = set(old_core) | {mid for _, mid in old["headers"]} if old else set()
        for mid in set(core) | {mid for _, mid in headers}:
            if mid in known:
                continue
            raw = payloads[mid]
            cur = conn.execute(
                "INSERT OR IGNORE INTO inc_messages VALUES(?,?,?)", (owner, mid, raw)
            )
            if cur.rowcount:
                self.metrics.add(
                    rows_written=1, bytes_written=len(raw.encode()), messages_created=1
                )
        append = (
            old is not None
            and old["has_messages"] == has_messages
            and core[: len(old_core)] == old_core
        )
        epoch = old["epoch"] if append else uuid.uuid4().hex
        start = len(old_core) if append else 0
        if not append:
            self.metrics.add(views_created=1)
        conn.executemany(
            "INSERT INTO inc_views VALUES(?,?,?,?)",
            [(epoch, index, owner, core[index]) for index in range(start, len(core))],
        )
        body = {
            "fields": fields,
            "owner": owner,
            "epoch": epoch,
            "count": len(core),
            "headers": headers,
            "has_messages": has_messages,
        }
        if old and all(old[key] == body[key] for key in body):
            if root:
                conn.execute("UPDATE inc_heads SET root=1 WHERE doc=?", (doc,))
            old.update(
                history=history,
                history_revision=getattr(history, "version", None),
                private_revision=private_revision(),
            )
            return {"kind": "document", "doc": doc, "revision": old_revision}, False
        row = conn.execute(
            "SELECT max(revision) FROM inc_versions WHERE doc=?", (doc,)
        ).fetchone()
        revision = (row[0] or 0) + 1
        raw = encode(body)
        manifest_hash = hashlib.sha256(raw.encode()).hexdigest()
        conn.execute(
            "INSERT INTO inc_versions VALUES(?,?,?,?)",
            (doc, revision, raw, manifest_hash),
        )
        conn.executemany(
            "INSERT INTO inc_edges VALUES(?,?,?,?)",
            [(doc, revision, *edge) for edge in set(edges)],
        )
        conn.execute(
            "INSERT INTO inc_heads VALUES(?,?,?) ON CONFLICT(doc) DO UPDATE SET revision=excluded.revision,root=max(root,excluded.root)",
            (doc, revision, int(root)),
        )
        self.metrics.add(rows_written=1, bytes_written=len(raw.encode()))
        self.cache[doc] = {
            **body,
            "revision": revision,
            "core_ids": core,
            "manifest_hash": manifest_hash,
            "history": history,
            "history_revision": getattr(history, "version", None),
            "private_revision": private_revision(),
        }
        return {"kind": "document", "doc": doc, "revision": revision}, True

    def _team_ref(self, conn, sid, snapshot, *, document=None, merge=True):
        doc = document or "team:" + sid
        old = self._meta(conn, doc)
        members = {}
        old_revision = -1
        if old:
            members = (
                dict(old["fields"].get("members", {}).get("items", {})) if merge else {}
            )
            if "revision" in old["fields"]:
                old_revision = self._component(
                    conn, doc, "revision", old["fields"]["revision"]
                )
        for name, item in snapshot.get("members", {}).items():
            previous = members.get(name)
            previous_revision = -1
            if previous:
                meta = self._meta(conn, previous["doc"], previous["revision"])
                if "revision" in meta["fields"]:
                    previous_revision = self._component(
                        conn, previous["doc"], "revision", meta["fields"]["revision"]
                    )
            if not merge or item.get("revision", 0) >= previous_revision:
                members[name], _ = self._save_doc(
                    conn,
                    "team-member:" + sid + ":" + name,
                    item,
                    "member:" + sid + ":" + name,
                )
        if merge and snapshot.get("revision", 0) < old_revision:
            # Keep the winning protocol/header fields without reading member message bodies.
            fields = {
                key: self._component(conn, doc, key, ref)
                for key, ref in old["fields"].items()
                if key != "members"
            }
        else:
            fields = {key: value for key, value in snapshot.items() if key != "members"}
        ref, changed = self._save_doc(
            conn,
            doc,
            fields,
            doc,
            root=document is None,
            refs={"members": {"kind": "members", "items": members}},
        )
        if document is None:
            SQLiteDatabase._put(
                self, conn, "team", sid, {"document": doc, "revision": ref["revision"]}
            )
        return ref, changed

    def _snapshot_team(self, conn, key, snapshot):
        sid = snapshot["session_id"]
        ref, _ = self._team_ref(
            conn, sid, snapshot["team"], document="snapshot-team:" + key, merge=False
        )
        self._team_ref(conn, sid, snapshot["team"])
        return ref

    def _get(self, conn, section, key, default=None):
        if section in {"checkpoints", "team"}:
            row = conn.execute(
                "SELECT 1 FROM records WHERE section=? AND key=?", (section, key)
            ).fetchone()
            if row is None:
                return copy.deepcopy(default)
            doc = ("checkpoint:" if section == "checkpoints" else "team:") + key
            if self._meta(conn, doc) is None:
                raise RecoveryRequired("Current document manifest is missing.")
            return self._load_doc(conn, doc)
        return super()._get(conn, section, key, default)

    def get(self, section, key, default=None):
        if not self.exists():
            return copy.deepcopy(default)
        with self.connection() as conn:
            conn.execute("BEGIN")
            try:
                return self._get(conn, section, key, default)
            finally:
                conn.execute("ROLLBACK")

    def _put(self, conn, section, key, value):
        if section == "checkpoints":
            refs = {}
            if value.get("team") is not None:
                refs["team"] = self._snapshot_team(conn, key, value)
            ref, _ = self._save_doc(
                conn, "checkpoint:" + key, value, key, root=True, refs=refs
            )
            return SQLiteDatabase._put(
                self,
                conn,
                section,
                key,
                {"document": ref["doc"], "revision": ref["revision"]},
            )
        if section == "team":
            return self._team_ref(conn, key, value)
        return super()._put(conn, section, key, value)

    def items(self, section, *, owner=None, prefix=None, owner_prefix=None):
        if not self.exists():
            return {}
        if section not in {"checkpoints", "team"}:
            return super().items(
                section, owner=owner, prefix=prefix, owner_prefix=owner_prefix
            )
        with self.connection() as conn:
            conn.execute("BEGIN")
            try:
                query = "SELECT key FROM records WHERE section=?"
                parameters = [section]
                if prefix is not None:
                    query += " AND key>=? AND key<?"
                    parameters.extend([prefix, prefix + "\uffff"])
                return {
                    key: self._get(conn, section, key)
                    for (key,) in conn.execute(query, parameters).fetchall()
                }
            finally:
                conn.execute("ROLLBACK")

    def load_session(self, requested):
        state = {
            "format": 1,
            **{
                key: {}
                for key in (
                    "checkpoints",
                    "team",
                    "operations",
                    "inbox",
                    "background",
                    "cron",
                    "session_meta",
                )
            },
        }
        if not requested or not self.exists():
            return state
        with self.connection() as conn:
            conn.execute("BEGIN")
            try:
                row = conn.execute(
                    "SELECT value FROM metadata WHERE key='latest_session'"
                ).fetchone()
                latest = loads(row[0]) if row else None
                sid = latest if requested == "latest" else requested
                state["latest_session"] = latest
                meta = super()._get(conn, "session_meta", sid)
                from homebrew_harness.storage.checkpoint_lifecycle import (
                    require_retained,
                )

                require_retained(meta)
                if (
                    meta is None
                    and conn.execute(
                        "SELECT 1 FROM records WHERE section='checkpoints' AND key=?",
                        ("lead:" + str(sid),),
                    ).fetchone()
                ):
                    raise RecoveryRequired(
                        "Current session lifecycle record is missing; no field adoption is supported."
                    )
                state["session_meta"][sid] = meta
                saved = self._get(conn, "checkpoints", "lead:" + str(sid))
                if saved is not None:
                    state["checkpoints"]["lead:" + sid] = saved
                prefix = "member:" + str(sid) + ":"
                for key, value in conn.execute(
                    "SELECT key,value FROM records WHERE section='operations' AND (owner=? OR (owner>=? AND owner<?))",
                    ("lead:" + str(sid), prefix, prefix + "\uffff"),
                ):
                    state["operations"][key] = loads(value)
                return state
            finally:
                conn.execute("ROLLBACK")

    def checkpoint(
        self, key, snapshot, *, ack=(), expected_revision=None, boundary=None
    ):
        from homebrew_harness.storage.checkpoint_lifecycle import (
            checkpoint_session,
            updated_meta,
        )

        with self.write() as conn:
            from homebrew_harness.storage.control_boundary import apply_boundary

            apply_boundary(self, conn, boundary)
            self._meta(conn, "checkpoint:" + key)
            sid = checkpoint_session(key, snapshot)
            refs = {}
            if snapshot.get("team") is not None:
                refs["team"] = self._snapshot_team(conn, key, snapshot)
            ref, changed = self._save_doc(
                conn,
                "checkpoint:" + key,
                snapshot,
                key,
                root=True,
                refs=refs,
                expected=expected_revision,
            )
            if sid is not None:
                meta = updated_meta(
                    super()._get(conn, "session_meta", sid), key, snapshot, changed
                )
                if meta is not None:
                    super()._put(conn, "session_meta", sid, meta)
            SQLiteDatabase._put(
                self,
                conn,
                "checkpoints",
                key,
                {"document": ref["doc"], "revision": ref["revision"]},
            )
            for name, owner, ids in ack:
                if name == "background":
                    for bid in ids:
                        value = super()._get(conn, "background", bid)
                        if value is not None:
                            value["ready"] = False
                            super()._put(conn, "background", bid, value)
                else:
                    self._ack(conn, name, owner, ids)
            conn.execute(
                "DELETE FROM records WHERE section='operations' AND owner=? AND json_extract(value,'$.state')='done'",
                (key,),
            )
        return ref["revision"]

    def team_checkpoint(self, sid, snapshot, *, ack_name=None, owner=None, records=()):
        from homebrew_harness.storage.checkpoint_lifecycle import updated_meta

        with self.write() as conn:
            _, changed = self._team_ref(conn, sid, snapshot)
            meta = updated_meta(
                super()._get(conn, "session_meta", sid), "lead:" + sid, {}, changed
            )
            if meta is not None:
                super()._put(conn, "session_meta", sid, meta)
            if ack_name:
                member = snapshot.get("members", {}).get(ack_name, {})
                for record in records:
                    if record.get("type") != "user_steer" or record[
                        "id"
                    ] not in member.get("user_input_ids", []):
                        continue
                    receipt = self._get(conn, "input_receipts", record["id"])
                    if receipt and receipt.get("target") == ack_name:
                        self._put(
                            conn,
                            "input_receipts",
                            record["id"],
                            {**receipt, "state": "applied", "applied_by": ack_name},
                        )
                self._ack(conn, ack_name, owner, [r["id"] for r in records])

    def _read(self, conn):
        state = super()._read(conn)
        for section in ("checkpoints", "team"):
            for key in state[section]:
                state[section][key] = self._get(conn, section, key)
        return state

    def _release_heads(self, conn):
        for prefix, section in (("checkpoint:", "checkpoints"), ("team:", "team")):
            existing = {
                prefix + key
                for (key,) in conn.execute(
                    "SELECT key FROM records WHERE section=?", (section,)
                )
            }
            for (doc,) in conn.execute(
                "SELECT doc FROM inc_heads WHERE root=1"
            ).fetchall():
                if doc.startswith(prefix) and doc not in existing:
                    conn.execute("UPDATE inc_heads SET root=0 WHERE doc=?", (doc,))

    def _replace(self, conn, old, new):
        super()._replace(conn, old, new)
        self._release_heads(conn)

    def remove(self, section, key):
        with self.write() as conn:
            conn.execute(
                "DELETE FROM records WHERE section=? AND key=?", (section, key)
            )
            self._release_heads(conn)

    def record_result(self, key, operation_id, output, snapshot):
        from homebrew_harness.tools.tool_outcome import needs_inspection

        hashed = hashlib.sha256(output.encode()).hexdigest()
        with self.write() as conn:
            previous = conn.execute(
                "SELECT context,hash FROM inc_results WHERE id=?", (operation_id,)
            ).fetchone()
            if previous:
                if previous != (key, hashed):
                    raise RecoveryRequired(
                        "Repeated tool result conflicts with committed output."
                    )
                return
            operation = super()._get(conn, "operations", operation_id)
            if operation is None or operation["checkpoint"] != key:
                raise RecoveryRequired(
                    "Tool attempt is not registered to this context."
                )
            operation.update(
                state="unknown" if needs_inspection(output) else "done",
                output=output,
                revision=operation.get("revision", 0) + 1,
            )
            super()._put(conn, "operations", operation_id, operation)
            # Same connection/transaction: result, message view and checkpoint head become durable together.
            refs = {}
            if snapshot.get("team") is not None:
                refs["team"] = self._snapshot_team(conn, key, snapshot)
            ref, changed = self._save_doc(
                conn, "checkpoint:" + key, snapshot, key, root=True, refs=refs
            )
            from homebrew_harness.storage.checkpoint_lifecycle import (
                checkpoint_session,
                updated_meta,
            )

            sid = checkpoint_session(key, snapshot)
            if sid is not None:
                meta = updated_meta(
                    super()._get(conn, "session_meta", sid), key, snapshot, changed
                )
                if meta is not None:
                    super()._put(conn, "session_meta", sid, meta)
            SQLiteDatabase._put(
                self,
                conn,
                "checkpoints",
                key,
                {"document": ref["doc"], "revision": ref["revision"]},
            )
            conn.execute(
                "INSERT INTO inc_results VALUES(?,?,?)", (operation_id, key, hashed)
            )

    def health(self):
        result = super().health()
        if not self.exists():
            return {**result, "messages": 0, "document_versions": 0}
        with self.connection() as conn:
            result.update(
                messages=conn.execute("SELECT count(*) FROM inc_messages").fetchone()[
                    0
                ],
                document_versions=conn.execute(
                    "SELECT count(*) FROM inc_versions"
                ).fetchone()[0],
            )
        return result
