"""Record-oriented SQLite/WAL adapter for private local runtime metadata."""

import copy
import hashlib
import json
import os
import sqlite3
import stat
import time
from contextlib import contextmanager
from pathlib import Path

from homebrew_harness.execution.execution_control import check, operation_deadline
from homebrew_harness.runtime.runtime_state import RecoveryRequired
from homebrew_harness.security.data_boundary import redact
from homebrew_harness.security.safe_files import SafeFile, canonical_root
from homebrew_harness.storage.storage_metrics import metrics_for

SECTIONS = ("checkpoints", "operations", "background", "team", "cron")
RECORD_SECTIONS = (
    *SECTIONS,
    "artifacts",
    "session_meta",
    "artifact_bundles",
    "goals",
    "input_epochs",
    "input_turns",
    "input_receipts",
    "runs",
    "run_attempts",
    "run_episodes",
    "run_controls",
    "run_heartbeats",
    "run_requests",
    "run_decisions",
    "run_progress",
    "run_sources",
    "run_events",
    "run_outbox",
    "tasks",
    "test_receipts",
    "stage_submissions",
    "stage_handoffs",
    "run_handoffs",
    "run_transfers",
    "run_members",
    "run_member_outcomes",
    "run_input_applied",
    "stage_inputs",
    "run_workspaces",
    "mcp_resources",
    "api_turns",
    "api_sessions",
    "api_submissions",
    "api_decisions",
)
DB_PATH = ".harness/runtime/database/runtime.sqlite3"
SCHEMA = """
CREATE TABLE IF NOT EXISTS records(section TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
 owner TEXT, revision INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(section,key));
CREATE INDEX IF NOT EXISTS records_owner ON records(section,owner);
CREATE TABLE IF NOT EXISTS inbox(recipient TEXT NOT NULL,id TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('pending','inflight')),
 owner TEXT,record TEXT NOT NULL,PRIMARY KEY(recipient,id));
CREATE INDEX IF NOT EXISTS inbox_delivery ON inbox(recipient,state);
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS recipients(name TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS sections(name TEXT PRIMARY KEY);

"""


def artifact_partition(value):
    fields = [
        value.get(key)
        for key in ("owner", "task_id", "workspace", "workspace_identity", "policy")
    ]
    return hashlib.sha256(
        json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def encode(value):
    raw = json.dumps(
        redact(value), ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )
    if len(raw.encode()) > 64 * 1024 * 1024:
        raise RecoveryRequired("A runtime record exceeds the host size limit.")
    return raw


class BoundCursor(sqlite3.Cursor):
    def execute(self, *args, **kwargs):
        self.connection.binding()
        return super().execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        self.connection.binding()
        return super().executemany(*args, **kwargs)


class BoundConnection(sqlite3.Connection):
    binding = staticmethod(lambda: None)

    def execute(self, *args, **kwargs):
        self.binding()
        return super().execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        self.binding()
        return super().executemany(*args, **kwargs)

    def cursor(self, *args, **kwargs):
        kwargs.setdefault("factory", BoundCursor)
        self.binding()
        return super().cursor(*args, **kwargs)


class SQLiteDatabase:
    def __init__(self, workdir):
        self.workdir = canonical_root(Path(workdir))
        self.path = self.workdir / DB_PATH
        self.metrics = metrics_for(self.workdir)

    def exists(self):
        for reference in (
            ".harness/runtime/state.json",
            ".harness/runtime/backend.json",
        ):
            try:
                with SafeFile(self.workdir, reference) as target, target.open_read():
                    raise RecoveryRequired(
                        "Unsupported prerelease runtime files; archive or reset them with the host before starting."
                    )
            except FileNotFoundError:
                pass
        try:
            with SafeFile(self.workdir, DB_PATH) as target:
                self._database_info(target)
                return True
        except FileNotFoundError:
            return False

    @staticmethod
    def _database_info(target):
        # Closing an unrelated descriptor to a SQLite database drops the
        # process's POSIX locks, including those held by its live connections.
        # Check the anchored path without opening another database descriptor.
        target.verify_binding()
        info = os.stat(target.parts[-1], dir_fd=target.fds[-1], follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            raise RecoveryRequired("Runtime database must be a regular file.")
        target.verify_binding()
        return info

    def schema_statements(self):
        raise NotImplementedError(
            "Only the current document runtime initializes a database."
        )

    @staticmethod
    def begin(connection, deadline):
        while True:
            check(deadline, gateway=False)
            try:
                connection.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as error:
                if not any(word in str(error).lower() for word in ("locked", "busy")):
                    raise
                if deadline.remaining() == 0:
                    raise RecoveryRequired("Runtime writer deadline reached.") from None
                time.sleep(min(0.02, deadline.remaining()))

    @contextmanager
    def connection(self, *, initialize=False):
        self.exists()
        with SafeFile(self.workdir, DB_PATH, create_parents=initialize) as target:
            if initialize:
                try:
                    fd = os.open(
                        target.parts[-1],
                        os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=target.fds[-1],
                    )
                    os.close(fd)
                except FileExistsError:
                    pass
            before = self._database_info(target)
            parent = os.fstat(target.fds[-1])
            if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o077:
                raise RecoveryRequired(
                    "Runtime database requires a private owner-only directory."
                )
            for name in (
                target.parts[-1],
                target.parts[-1] + "-wal",
                target.parts[-1] + "-shm",
            ):
                try:
                    info = os.stat(name, dir_fd=target.fds[-1], follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_mode & 0o077
                ):
                    raise RecoveryRequired(
                        "Runtime database files must be private regular files."
                    )
            target.verify_binding()
            connection = sqlite3.connect(
                self.path.as_uri() + "?mode=rw",
                uri=True,
                timeout=0,
                isolation_level=None,
                factory=BoundConnection,
            )

            def binding():
                target.verify_binding()
                current = os.stat(
                    target.parts[-1], dir_fd=target.fds[-1], follow_symlinks=False
                )
                if not stat.S_ISREG(current.st_mode) or (
                    current.st_dev,
                    current.st_ino,
                ) != (before.st_dev, before.st_ino):
                    raise RecoveryRequired(
                        "Runtime database binding changed; SQL operation refused."
                    )

            connection.binding = binding
            try:
                binding()
                deadline = operation_deadline(2)
                while True:
                    check(deadline, gateway=False)
                    try:
                        connection.execute("PRAGMA foreign_keys=ON")
                        connection.execute("PRAGMA trusted_schema=OFF")
                        connection.execute("PRAGMA synchronous=FULL")
                        connection.execute("PRAGMA busy_timeout=0")
                        schema = connection.execute("PRAGMA user_version").fetchone()[0]
                        if schema == 0 and initialize:
                            if connection.execute(
                                "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                            ).fetchone():
                                raise RecoveryRequired(
                                    "Unrecognized database without a current schema identifier."
                                )
                            connection.execute("PRAGMA journal_mode=WAL")
                        break
                    except sqlite3.OperationalError as error:
                        if not any(
                            word in str(error).lower() for word in ("locked", "busy")
                        ):
                            raise
                        if deadline.remaining() == 0:
                            raise RecoveryRequired(
                                "Runtime database open deadline reached."
                            ) from None
                        time.sleep(min(0.02, deadline.remaining()))
                if schema == 0 and initialize:
                    self.begin(connection, deadline)
                    try:
                        schema = connection.execute("PRAGMA user_version").fetchone()[0]
                        if schema == 0:
                            if connection.execute(
                                "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                            ).fetchone():
                                raise RecoveryRequired(
                                    "Unrecognized database without a current schema identifier."
                                )
                            for statement in self.schema_statements():
                                connection.execute(statement)
                            connection.execute("PRAGMA user_version=1")
                            schema = 1
                        connection.execute("COMMIT")
                    except BaseException:
                        if connection.in_transaction:
                            connection.execute("ROLLBACK")
                        raise
                if schema != 1:
                    raise RecoveryRequired(
                        "Unsupported runtime schema; no prerelease compatibility or automatic conversion."
                    )
                target.verify_binding()
                current = os.stat(
                    target.parts[-1], dir_fd=target.fds[-1], follow_symlinks=False
                )
                if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
                    raise RecoveryRequired("Runtime database changed during open.")
                yield connection
                target.verify_binding()
                current = os.stat(
                    target.parts[-1], dir_fd=target.fds[-1], follow_symlinks=False
                )
                if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
                    raise RecoveryRequired(
                        "Runtime database changed during the transaction."
                    )
            except sqlite3.DatabaseError as error:
                raise RecoveryRequired(
                    "Runtime database unavailable ("
                    + type(error).__name__
                    + "); no automatic replay."
                ) from error
            finally:
                connection.close()

    @contextmanager
    def write(self):
        with self.metrics.transaction(), self.connection(initialize=True) as conn:
            deadline = operation_deadline(2)
            while True:
                check(deadline, gateway=False)
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    break
                except sqlite3.OperationalError as error:
                    if (
                        "locked" not in str(error).lower()
                        and "busy" not in str(error).lower()
                    ):
                        raise
                    if deadline.remaining() == 0:
                        raise RecoveryRequired(
                            "Runtime database busy; transaction deadline reached."
                        ) from None
                    time.sleep(min(0.02, deadline.remaining()))
            changed = conn.total_changes
            try:
                yield conn
                check(deadline, gateway=False)
                conn.execute("COMMIT")
                self.metrics.add(
                    **({"commits": 1} if conn.total_changes > changed else {"noops": 1})
                )
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

    def _get(self, conn, section, key, default=None):
        row = conn.execute(
            "SELECT value FROM records WHERE section=? AND key=?", (section, key)
        ).fetchone()
        if row is None:
            return copy.deepcopy(default)
        self.metrics.add(reads=1, bytes_read=len(row[0].encode()))
        return json.loads(row[0])

    def get(self, section, key, default=None):
        if not self.exists():
            return copy.deepcopy(default)
        with self.connection() as conn:
            return self._get(conn, section, key, default)

    def artifact_page(self, scope=None, *, offset=0, limit=20):
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 51
        ):
            raise ValueError("Invalid artifact page bounds.")
        if not self.exists():
            return []
        with self.connection() as conn:
            # records_owner carries rowid as its final key. Updates preserve rowid,
            # so this is creation order without loading and sorting every bundle.
            clause = "section='artifact_bundles'" + (
                " AND owner=?" if scope is not None else ""
            )
            parameters = [artifact_partition(scope)] if scope is not None else []
            rows = conn.execute(
                "SELECT value FROM records WHERE "
                + clause
                + " ORDER BY rowid DESC LIMIT ? OFFSET ?",
                (*parameters, limit, offset),
            ).fetchall()
            for (raw,) in rows:
                self.metrics.add(reads=1, bytes_read=len(raw.encode()))
            return [json.loads(raw) for (raw,) in rows]

    def get_meta(self, key, default=None):
        if not self.exists():
            return copy.deepcopy(default)
        with self.connection() as conn:
            row = conn.execute(
                "SELECT value FROM metadata WHERE key=?", (key,)
            ).fetchone()
            return json.loads(row[0]) if row else copy.deepcopy(default)

    def start_session(self, sid, instance):
        from homebrew_harness.storage.checkpoint_lifecycle import (
            session_id,
            started_meta,
        )

        session_id(sid)
        with self.write() as conn:
            self._put(
                conn,
                "session_meta",
                sid,
                started_meta(self._get(conn, "session_meta", sid), sid, instance),
            )

    def finish_session(self, sid, instance, *, complete):
        from homebrew_harness.storage.checkpoint_lifecycle import (
            finished_meta,
            session_id,
        )

        session_id(sid)
        with self.write() as conn:
            self._put(
                conn,
                "session_meta",
                sid,
                finished_meta(self._get(conn, "session_meta", sid), instance, complete),
            )

    def _put(self, conn, section, key, value):
        conn.execute("INSERT OR IGNORE INTO sections VALUES(?)", (section,))
        raw = encode(value)
        owner = (
            (
                artifact_partition(value)
                if section == "artifact_bundles"
                else value.get("checkpoint")
            )
            if isinstance(value, dict)
            else None
        )
        revision = value.get("revision", 0) if isinstance(value, dict) else 0
        cur = conn.execute(
            "INSERT INTO records(section,key,value,owner,revision) VALUES(?,?,?,?,?) "
            "ON CONFLICT(section,key) DO UPDATE SET value=excluded.value,owner=excluded.owner,revision=excluded.revision WHERE value<>excluded.value",
            (section, key, raw, owner, revision),
        )
        if cur.rowcount:
            self.metrics.add(rows_written=1, bytes_written=len(raw.encode()))

    def put(self, section, key, value):
        with self.write() as conn:
            self._put(conn, section, key, value)

    def compare_put(self, section, key, value, expected_revision):
        """Compare a record revision and replace it in one indexed transaction."""
        with self.write() as conn:
            previous = self._get(conn, section, key)
            if (
                not isinstance(previous, dict)
                or previous.get("revision") != expected_revision
            ):
                return False
            self._put(conn, section, key, value)
            return True

    def remove(self, section, key):
        with self.write() as conn:
            conn.execute(
                "DELETE FROM records WHERE section=? AND key=?", (section, key)
            )

    def update_fields(self, section, key, fields):
        with self.write() as conn:
            value = self._get(conn, section, key)
            if value is None:
                raise RecoveryRequired("Runtime record not found.")
            value.update(fields)
            self._put(conn, section, key, value)

    def items(self, section, *, owner=None, prefix=None, owner_prefix=None):
        if not self.exists():
            return {}
        with self.connection() as conn:
            query = "SELECT key,value FROM records WHERE section=?"
            parameters = [section]
            if owner is not None:
                query += " AND owner=?"
                parameters.append(owner)
            if owner_prefix is not None:
                query += " AND owner>=? AND owner<?"
                parameters.extend([owner_prefix, owner_prefix + "\uffff"])
            if prefix is not None:
                query += " AND key>=? AND key<?"
                parameters.extend([prefix, prefix + "\uffff"])
            rows = conn.execute(query, parameters).fetchall()
            self.metrics.add(reads=1, bytes_read=sum(len(r[1].encode()) for r in rows))
            return {key: json.loads(value) for key, value in rows}

    def set_meta(self, key, value):
        with self.write() as conn:
            raw = encode(value)
            cur = conn.execute(
                "INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE value<>excluded.value",
                (key, raw),
            )
            if cur.rowcount:
                self.metrics.add(rows_written=1, bytes_written=len(raw.encode()))

    def clear_operations(self, keys):
        with self.write() as conn:
            conn.executemany(
                "DELETE FROM records WHERE section='operations' AND key=?",
                [(k,) for k in keys],
            )

    def mailbox(self, action, name, *, owner=None, records=()):
        if action == "inspect":
            if not self.exists():
                return []
            with self.connection() as conn:
                rows = conn.execute(
                    "SELECT record FROM inbox WHERE recipient=? AND state='pending' ORDER BY rowid LIMIT 65",
                    (name,),
                ).fetchall()
                return [json.loads(row[0]) for row in rows]
        if action in {"ack", "release"} and not records:
            return []
        if action in {"claim", "recover"} and not self.exists():
            return []
        if action in {"peek", "available"}:
            if not self.exists():
                return False
            with self.connection() as conn:
                return (
                    conn.execute(
                        "SELECT 1 FROM inbox WHERE recipient=?"
                        + (" AND state='pending'" if action == "available" else "")
                        + " LIMIT 1",
                        (name,),
                    ).fetchone()
                    is not None
                )
        with self.write() as conn:
            conn.execute("INSERT OR IGNORE INTO recipients VALUES(?)", (name,))
            if action == "send":
                for record in records:
                    self._send(conn, name, record)
            elif action == "claim":
                rows = conn.execute(
                    "SELECT record FROM inbox WHERE recipient=? AND state='pending' ORDER BY rowid",
                    (name,),
                ).fetchall()
                conn.execute(
                    "UPDATE inbox SET state='inflight',owner=? WHERE recipient=? AND state='pending'",
                    (owner, name),
                )
                return [json.loads(row[0]) for row in rows]
            elif action == "recover":
                conn.execute(
                    "UPDATE inbox SET state='pending',owner=NULL WHERE recipient=? AND state='inflight'",
                    (name,),
                )
            elif action == "ack":
                self._ack(conn, name, owner, [record["id"] for record in records])
            elif action == "release":
                conn.executemany(
                    "UPDATE inbox SET state='pending',owner=NULL WHERE recipient=? AND id=? AND owner=?",
                    [(name, r["id"], owner) for r in records],
                )
            else:
                raise ValueError("Unknown mailbox action.")
        return []

    def _send(self, conn, name, record):
        conn.execute("INSERT OR IGNORE INTO recipients VALUES(?)", (name,))
        raw = encode(record)
        cur = conn.execute(
            "INSERT OR IGNORE INTO inbox VALUES(?,?,'pending',NULL,?)",
            (name, record["id"], raw),
        )
        if cur.rowcount:
            self.metrics.add(rows_written=1, bytes_written=len(raw.encode()))
            meta = record.get("metadata", {})
            if meta.get("run_id") and record.get("type") == "user_steer":
                scoped = meta["run_id"] + ":" + name
                self._put(
                    conn,
                    "input_epochs",
                    scoped,
                    self._get(conn, "input_epochs", scoped, 0) + 1,
                )
            if (
                record.get("type") in {"user_steer", "run_reference"}
                or record.get("type") == "user_input"
                and meta.get("mode") != "queued"
                and (not meta.get("target_input_id") or meta.get("turn_id") is not None)
            ):
                self._put(
                    conn,
                    "input_epochs",
                    name,
                    self._get(conn, "input_epochs", name, 0) + 1,
                )
                if (
                    record.get("type") == "user_input"
                    and meta.get("target", "lead") != "lead"
                ):
                    target = meta["target"]
                    self._put(
                        conn,
                        "input_epochs",
                        target,
                        self._get(conn, "input_epochs", target, 0) + 1,
                    )

    def submit_input(self, record, expected_turn=None):
        name = record["to"]
        with self.write() as conn:
            prior = self._get(conn, "input_submissions", record["id"])
            if prior is not None:
                if any(
                    prior.get(k) != record.get(k) for k in ("to", "content", "type")
                ) or any(
                    prior["metadata"].get(k) != record["metadata"].get(k)
                    for k in (
                        "mode",
                        "target",
                        "literal",
                        "sdk_goal",
                        "target_input_id",
                        "sdk_turn",
                    )
                ):
                    raise ValueError(
                        "Input ID was already accepted with another payload or target."
                    )
                return prior["metadata"].get("turn_id")
            turn = self._get(conn, "input_turns", name)
            target_input = record.get("metadata", {}).get("target_input_id")
            if target_input:
                target = self._get(conn, "api_turns", target_input)
                if (
                    not target
                    or target["checkpoint"] != record["checkpoint"]
                    or target["state"] not in {"queued", "running"}
                    or target["cancel_requested"]
                ):
                    raise ValueError("SDK input target ended or changed.")
                if target["state"] == "queued":
                    turn = None
            if expected_turn is not None and expected_turn != turn:
                raise ValueError("The targeted turn changed; refresh the input status.")
            if (
                conn.execute(
                    "SELECT count(*) FROM inbox WHERE recipient=?", (name,)
                ).fetchone()[0]
                >= 64
            ):
                raise ValueError("User input queue is full.")
            record["metadata"]["turn_id"] = turn
            self._send(conn, name, record)
            self._put(conn, "input_submissions", record["id"], record)
            from homebrew_harness.storage.api_state import accept_turn

            accept_turn(self, conn, record)
        return turn

    def input_guard(self, names):
        if not self.exists():
            return {name: 0 for name in names}
        with self.connection() as conn:
            return {name: self._get(conn, "input_epochs", name, 0) for name in names}

    def accept_operation(self, key, record, guard):
        from homebrew_harness.storage.control_boundary import check_guard

        with self.write() as conn:
            check_guard(self, conn, guard)
            if record.get("managed_run"):
                from homebrew_harness.runs.store import owner_guard

                managed = owner_guard(self, conn, record["managed_run"])
                if record.get("approval_id"):
                    from homebrew_harness.runs.requests import consume

                    consume(self, conn, record, managed)
            if record.get("managed_run") and record.get("test_recipe"):
                from homebrew_harness.runs.test_authorization import accept

                accept(self, conn, managed, record)
            self._put(conn, "operations", key, record)

    @staticmethod
    def _ack(conn, name, owner, ids):
        conn.executemany(
            "DELETE FROM inbox WHERE recipient=? AND id=? AND owner IS ?",
            [(name, mid, owner) for mid in ids],
        )

    def _read(self, conn):
        state = {"format": 1, **{k: {} for k in (*SECTIONS, "inbox")}}
        for (name,) in conn.execute("SELECT name FROM sections"):
            state[name] = {}
        for (name,) in conn.execute("SELECT name FROM recipients"):
            state["inbox"][name] = {}
        for key, value in conn.execute("SELECT key,value FROM metadata"):
            state[key] = json.loads(value)
        for section, key, raw in conn.execute("SELECT section,key,value FROM records"):
            state.setdefault(section, {})[key] = json.loads(raw)
        for name, mid, status, owner, raw in conn.execute(
            "SELECT recipient,id,state,owner,record FROM inbox ORDER BY rowid"
        ):
            state["inbox"].setdefault(name, {})[mid] = {
                "state": status,
                "owner": owner,
                "record": json.loads(raw),
            }
        return state

    def read(self):
        if not self.exists():
            return {"format": 1, **{key: {} for key in (*SECTIONS, "inbox")}}
        with self.connection() as conn:
            conn.execute("BEGIN")
            try:
                return self._read(conn)
            finally:
                conn.execute("ROLLBACK")

    def _replace(self, conn, old, new):
        # Host maintenance transaction; normal runtime methods use indexed rows.
        for section in set(old) | set(new):
            if section == "format":
                continue
            if section == "inbox":
                for name in old.get("inbox", {}).keys() - new.get("inbox", {}).keys():
                    conn.execute("DELETE FROM recipients WHERE name=?", (name,))
                for name in new.get("inbox", {}):
                    conn.execute("INSERT OR IGNORE INTO recipients VALUES(?)", (name,))
                oldrows = {
                    (name, mid): item
                    for name, items in old.get(section, {}).items()
                    for mid, item in items.items()
                }
                newrows = {
                    (name, mid): item
                    for name, items in new.get(section, {}).items()
                    for mid, item in items.items()
                }
                for name, mid in oldrows.keys() - newrows.keys():
                    conn.execute(
                        "DELETE FROM inbox WHERE recipient=? AND id=?", (name, mid)
                    )
                for (name, mid), item in newrows.items():
                    if oldrows.get((name, mid)) != item:
                        conn.execute(
                            "INSERT INTO inbox VALUES(?,?,?,?,?) ON CONFLICT(recipient,id) DO UPDATE SET state=excluded.state,owner=excluded.owner,record=excluded.record",
                            (
                                name,
                                mid,
                                item["state"],
                                item.get("owner"),
                                encode(item["record"]),
                            ),
                        )
            elif section in RECORD_SECTIONS:
                if section in new:
                    conn.execute("INSERT OR IGNORE INTO sections VALUES(?)", (section,))
                else:
                    conn.execute("DELETE FROM sections WHERE name=?", (section,))
                for key in old.get(section, {}).keys() - new.get(section, {}).keys():
                    conn.execute(
                        "DELETE FROM records WHERE section=? AND key=?", (section, key)
                    )
                for key, value in new.get(section, {}).items():
                    if key not in old.get(section, {}) or old[section][key] != value:
                        self._put(conn, section, key, value)
            elif old.get(section) != new.get(section):
                if section in new:
                    conn.execute(
                        "INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (section, encode(new[section])),
                    )
                else:
                    conn.execute("DELETE FROM metadata WHERE key=?", (section,))

    def transaction(self, update):
        with self.write() as conn:
            old = self._read(conn)
            new = copy.deepcopy(old)
            result = update(new)
            self._replace(conn, old, new)
            return copy.deepcopy(result)

    def health(self):
        if not self.exists():
            return {
                "backend": "sqlite",
                "schema": 1,
                "initialized": False,
                "records": 0,
                "inbox": 0,
            }
        with self.connection() as conn:
            return {
                "backend": "sqlite",
                "schema": 1,
                "integrity": conn.execute("PRAGMA quick_check").fetchone()[0],
                "records": conn.execute("SELECT count(*) FROM records").fetchone()[0],
                "inbox": conn.execute("SELECT count(*) FROM inbox").fetchone()[0],
                "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
            }
