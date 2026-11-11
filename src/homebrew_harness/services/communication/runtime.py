"""Event-driven discussion preparation and authorized outbox execution."""

import threading
import time
from pathlib import Path

from homebrew_harness.execution.execution_control import (
    control_scope,
    operation_deadline,
    cleanup_scope,
    ControlledRLock,
)
from homebrew_harness.execution.execution_leases import ExecutionLeases
from homebrew_harness.platforms.contracts import ObjectRef, SourceEvent, digest
from homebrew_harness.services.protocol import fingerprint, request_key
from homebrew_harness.services.sources import retry_after
from .spec import open_projects
from .store import CommunicationStore, SECTIONS
from .snapshots import materialize, verify
from homebrew_harness.runtime.runtime_control import ClosePending
from homebrew_harness.services.resources import TestResources
from .transitions import request_analysis, preparation_applies


class CommunicationRuntime:
    def __init__(self, runtime, maintenance, environment):
        self.runtime, self.ms, self.policy = (
            runtime,
            maintenance,
            runtime.spec.communication,
        )
        self.store = CommunicationStore(runtime, maintenance, self.policy)
        self.projects = open_projects(
            self.policy, environment, (runtime.store.root, runtime.spec.workspace)
        )
        self.stop = threading.Event()
        self.finished = threading.Event()
        self.started = False
        self.error = None
        self.closed = set()
        self.leases = ExecutionLeases(runtime.store.root)
        self.tests = TestResources(self.leases, "communication-review")
        self.observation_lock = ControlledRLock()
        self.publication_lock = ControlledRLock()
        self.cursors = {}
        try:
            for scope, (adapter, _identity) in self.projects.items():
                if set(self.policy["enabled_actions"]) - set(adapter.capabilities):
                    raise ValueError(
                        "Platform cannot perform enabled communication actions."
                    )
                self.store.actor_ids[scope] = adapter.identity()
        except BaseException:
            for adapter, _identity in self.projects.values():
                adapter.close()
            self.leases.close()
            raise
        self.ms.communication = self.store

    def identity(self):
        return {
            "policy": fingerprint(self.policy),
            "projects": {
                digest(scope): {
                    "identity": identity,
                    "actor_id": self.store.actor_ids[scope],
                }
                for scope, (_adapter, identity) in self.projects.items()
            },
        }

    @property
    def test_runner(self):
        return self.tests.runner

    def adapter(self, subject):
        ref = ObjectRef(**subject)
        if ref.scope() not in self.projects:
            raise ValueError("Discussion project is not configured.")
        return self.projects[ref.scope()][0]

    def rows(self, kind, limit=100):
        import json

        now = time.time()
        condition = (
            " AND coalesce(json_extract(value,'$.retry_after'),0)<=?"
            " AND (json_extract(value,'$.next_poll')<=? OR"
            " (json_extract(value,'$.state')='ready_to_assess' AND coalesce(json_extract(value,'$.ready_after'),0)<=?))"
            if kind == "discussions"
            else " AND json_extract(value,'$.state')='queued' AND coalesce(json_extract(value,'$.next_attempt'),0)<=?"
        )
        times = (now, now, now) if kind == "discussions" else (now,)
        with self.ms.db.connection() as conn:
            rows = conn.execute(
                "SELECT rowid,value FROM records WHERE section=? AND owner=?"
                + condition
                + " ORDER BY CASE WHEN rowid>? THEN 0 ELSE 1 END,rowid LIMIT ?",
                (
                    SECTIONS[kind],
                    "service:" + self.runtime.id,
                    *times,
                    self.cursors.get(kind, 0),
                    limit,
                ),
            ).fetchall()
        if rows:
            self.cursors[kind] = rows[-1][0]
        return [json.loads(row[1]) for row in rows]

    def loop(self):
        self.started = True
        try:
            self.store.recover()
            while not self.stop.wait(0.05):
                try:
                    self.tests.settle()
                except ClosePending:
                    continue
                meta = self.runtime.store.get(self.runtime.id)
                if meta["state"] == "stopping":
                    return
                if (
                    meta.get("operations_phase", "active") != "active"
                    or meta["state"] == "paused"
                    and meta["scope"] == "all"
                ):
                    continue
                for row in self.rows("discussions"):
                    if self.stop.is_set():
                        return
                    try:
                        if row["next_poll"] <= time.time():
                            self.poll(row)
                        current = self.store.get("discussions", row["id"])
                        if (
                            current["subject"]["kind"] == "change_request"
                            and not current.get("review_id")
                            and current.get("observed_basis", {}).get("state") == "open"
                            and current["state"] not in {"paused", "blocked"}
                        ):
                            self.prepare_review(current)
                        current = self.store.get("discussions", row["id"])
                        if current["state"] == "ready_to_assess":
                            self.store.queue_round(
                                current, force=current.get("ready_after") == 0
                            )
                    except Exception as error:
                        if self.stop.is_set():
                            return
                        self.fail(row, error)
                for action in self.rows("communication_actions"):
                    if self.stop.is_set():
                        return
                    if (
                        action["state"] == "queued"
                        and action.get("next_attempt", 0) <= time.time()
                    ):
                        self.execute(action)
        except Exception as error:
            self.error = type(error).__name__
        finally:
            self.finished.set()

    def fail(self, row, error):
        with self.ms.db.write() as conn:
            self.runtime.store.guard(conn, self.runtime.header)
            current = self.store.get("discussions", row["id"], conn)
            current.update(
                error=type(error).__name__,
                next_poll=time.time()
                + retry_after(error, current.get("poll_attempts", 0) + 1),
                poll_attempts=current.get("poll_attempts", 0) + 1,
                retry_after=time.time()
                + retry_after(error, current.get("poll_attempts", 0) + 1),
            )
            if current.get("poll_attempts", 0) >= 3:
                current["state"] = "blocked"
            self.store.put(conn, "discussions", current)

    def poll(self, row):
        with control_scope(
            deadline=operation_deadline(self.policy["prepare_seconds"]),
            cancel_events=(self.stop,),
        ):
            self.freshness(row)
        with self.ms.db.write() as conn:
            self.runtime.store.guard(conn, self.runtime.header)
            current = self.store.get("discussions", row["id"], conn)
            current.update(
                next_poll=time.time() + self.policy["poll_seconds"],
                error=None,
                poll_attempts=0,
            )
            self.store.put(conn, "discussions", current)

    def request_review(self, identity, revision, request_id):
        def apply(conn):
            row = self.store.get("discussions", identity, conn)
            if (
                row["revision"] != revision
                or row["subject"]["kind"] != "change_request"
            ):
                raise ValueError("Review request requires the current PR reference.")
            operation = "review_prepare_" + request_key(
                "review-prepare", self.runtime.id, request_id
            )
            request_analysis(row)
            value = {
                "id": operation,
                "discussion_id": identity,
                "state": "queued",
                "basis": row.get("observed_basis"),
                "epoch": row["analysis_epoch"],
                "created_at": time.time(),
            }
            self.store.put(conn, "review_operations", value)
            row.update(
                review_operation=operation,
                review_id=None,
                next_poll=0,
            )
            self.store.put(conn, "discussions", row)
            self.store.supersede(conn, row)
            return {"id": operation, "state": "queued"}

        return self.runtime.store.command(
            self.runtime.header,
            "discussion_prepare_review",
            {"id": identity, "revision": revision},
            request_id,
            apply,
        )

    def prepare_review(self, row):
        self.tests.settle()
        operation = row.get("review_operation") or "review_prepare_" + request_key(
            "review-prepare",
            self.runtime.id,
            row["id"]
            + ":"
            + digest([row.get("observed_basis"), row["analysis_epoch"]]),
        )
        with (
            self.leases.use("communication-review"),
            control_scope(
                deadline=operation_deadline(self.policy["prepare_seconds"]),
                cancel_events=(self.stop,),
            ),
        ):
            with self.ms.db.write() as conn:
                self.runtime.store.guard(conn, self.runtime.header)
                current = self.store.get("discussions", row["id"], conn)
                if (
                    current["state"] == "paused"
                    or current["analysis_epoch"] != row["analysis_epoch"]
                    or current.get("observed_basis") != row.get("observed_basis")
                ):
                    return {"id": operation, "state": "superseded"}
                record = self.ms.db._get(
                    conn, "service_review_operations", operation
                ) or {
                    "id": operation,
                    "discussion_id": row["id"],
                    "basis": row.get("observed_basis"),
                    "epoch": row["analysis_epoch"],
                    "created_at": time.time(),
                }
                record.update(
                    state="running", generation=self.runtime.header["generation"]
                )
                current["review_operation"] = operation
                self.store.put(conn, "discussions", current)
                self.store.put(conn, "review_operations", record)
            try:
                captured = self.adapter(row["subject"]).snapshot(
                    row["subject"], self.policy
                )
                snapshot = materialize(
                    self.runtime.store.root, row["subject"], captured, self.policy
                )
                with self.ms.db.connection() as conn:
                    existing = self.ms.db._get(
                        conn, "service_review_snapshots", snapshot["id"]
                    )
                if existing:
                    verify(existing, self.policy)
                    snapshot = existing
                elif self.policy["required_tests"]:
                    self.run_tests(snapshot)
                verify(snapshot, self.policy)
                with self.ms.db.write() as conn:
                    self.runtime.store.guard(conn, self.runtime.header)
                    current = self.store.get("discussions", row["id"], conn)
                    self.store.put(conn, "review_snapshots", snapshot)
                    if not preparation_applies(current, record, snapshot):
                        self.store.put(
                            conn, "review_operations", {**record, "state": "superseded"}
                        )
                        return {"id": operation, "state": "superseded"}
                    current.update(
                        review_id=snapshot["id"],
                        revision=current["revision"] + 1,
                        state="ready_to_assess",
                        ready_after=0,
                    )
                    self.store.put(conn, "discussions", current)
                    self.store.supersede(conn, current)
                    self.store.put(
                        conn,
                        "review_operations",
                        {
                            **record,
                            "state": "completed",
                            "snapshot_id": snapshot["id"],
                            "finished_at": time.time(),
                        },
                    )
                return {
                    "id": operation,
                    "state": "completed",
                    "snapshot_id": snapshot["id"],
                }
            except BaseException as error:
                with cleanup_scope(), self.ms.db.write() as conn:
                    self.runtime.store.guard(conn, self.runtime.header)
                    self.store.put(
                        conn,
                        "review_operations",
                        {**record, "state": "failed", "error": type(error).__name__},
                    )
                raise

    def run_tests(self, snapshot):
        from homebrew_harness.execution.test_runner import load_test_runner
        from homebrew_harness.execution.execution_backend import ContainerBackend

        runner = load_test_runner(
            self.runtime.core.config.settings.test_config, Path(snapshot["workspace"])
        )
        self.tests.own(runner)
        try:
            receipts = []
            for name in self.policy["required_tests"]:
                runner._recipe(name)
                if self.policy["mode"] == "remote" and not isinstance(
                    runner.backend, ContainerBackend
                ):
                    raise ValueError(
                        "External PR execution requires a container test backend."
                    )
                runner.approve(name)
                receipts.append(runner.run(name))
            snapshot.update(
                tests=receipts,
                tests_passed=all(
                    r.get("status") == "passed" and r.get("source_stable")
                    for r in receipts
                ),
            )
        finally:
            self.tests.settle()

    def freshness(self, row):
        with self.observation_lock:
            through = self.store.get("discussions", row["id"])["seq"]
            observed = self.adapter(row["subject"]).observe(row["subject"])
            items = self.store.observed_events(
                row["subject"],
                observed["events"],
                complete=observed.get("complete", False),
                through=through,
            )
            if row["subject"]["kind"] == "change_request":
                items.append(
                    SourceEvent(
                        ObjectRef(**row["subject"]),
                        ObjectRef(**row["subject"]),
                        digest(observed["basis"]),
                        {
                            "title": "",
                            "body": "",
                            "state": observed["basis"]["state"],
                            "actor_id": None,
                            "metadata": {"review_basis": observed["basis"]},
                        },
                    )
                )
            self.store.ingest(
                "communication",
                items,
                ObjectRef(**row["subject"]).scope(),
                observed=True,
            )

    def execute(self, action):
        accepted = None
        with (
            control_scope(
                deadline=operation_deadline(self.policy["request_seconds"]),
                cancel_events=(self.stop,),
            ),
            self.publication_lock,
            self.leases.use("communication-publish"),
        ):
            row = self.store.get("discussions", action["discussion_id"])
            if action["expires_at"] <= time.time():
                self.renew_expired(action)
                return
            try:
                self.freshness(row)
                if (
                    self.store.get("communication_actions", action["id"])["state"]
                    != "queued"
                ):
                    return
                if row.get("review_id"):
                    verify(
                        self.store.get("review_snapshots", row["review_id"]),
                        self.policy,
                    )
                accepted = self.store.start(action)
                result = self.adapter(row["subject"]).publish(
                    {**accepted["plan"], "correlation": accepted["id"]}
                )
                try:
                    self.freshness(self.store.get("discussions", row["id"]))
                except Exception:
                    result["freshness"] = "unverified"
                with cleanup_scope():
                    self.store.finish_action(accepted, "confirmed", result)
            except Exception as error:
                if accepted is not None:
                    from homebrew_harness.services.action_runtime import failed_state

                    with cleanup_scope():
                        self.store.finish_action(
                            accepted,
                            failed_state(error),
                            error=str(error)
                            if hasattr(error, "outcome")
                            else type(error).__name__,
                        )
                else:
                    with self.ms.db.write() as conn:
                        self.runtime.store.guard(conn, self.runtime.header)
                        current = self.store.get(
                            "communication_actions", action["id"], conn
                        )
                        if current["state"] == "queued":
                            failures = current.get("preflight_failures", 0) + 1
                            current.update(
                                preflight_failures=failures,
                                next_attempt=time.time() + min(60, 2**failures),
                                error=type(error).__name__,
                                revision=current["revision"] + 1,
                            )
                            if failures >= 3:
                                current["state"] = "failed"
                                current["authorization"] = None
                            self.store.put(conn, "communication_actions", current)

    def renew_expired(self, action):
        with self.ms.db.write() as conn:
            self.runtime.store.guard(conn, self.runtime.header)
            current = self.store.get("communication_actions", action["id"], conn)
            if current["state"] != "queued":
                return
            current.update(
                state="awaiting_approval",
                authorization=None,
                revision=current["revision"] + 1,
            )
            row = self.store.get("discussions", current["discussion_id"], conn)
            self.store.put(conn, "communication_actions", current)
            self.store.request_authority(conn, row, current)

    def reconcile(self, identity):
        with (
            control_scope(deadline=operation_deadline(self.policy["request_seconds"])),
            self.publication_lock,
            self.leases.use("communication-publish"),
        ):
            action = self.store.get("communication_actions", identity)
            if action["state"] == "confirmed":
                return action
            if action["state"] != "unknown":
                raise ValueError("Only unknown actions can be reconciled.")
            result = self.adapter(action["plan"]["subject"]).publish(
                {**action["plan"], "correlation": action["id"]}, reconcile=True
            )
            if result:
                try:
                    self.freshness(
                        self.store.get("discussions", action["discussion_id"])
                    )
                except Exception:
                    result["freshness"] = "unverified"
                return self.store.finish_action(action, "confirmed", result)
            return action

    def inspect(self, payload, request_id):
        if payload.get("conclusion", "reconcile") == "reconcile":
            return self.reconcile(payload["id"])
        if payload.get("conclusion") != "retry_not_executed":
            raise ValueError("Unknown publication inspection conclusion.")
        from homebrew_harness.services.action_runtime import confirm_unexecuted

        def apply(conn):
            self.runtime.store.guard(conn, self.runtime.header)
            action = self.store.get("communication_actions", payload["id"], conn)
            row = self.store.get("discussions", action["discussion_id"], conn)
            value = confirm_unexecuted(
                action,
                revision=payload.get("revision"),
                confirmed=payload.get("confirmed"),
                decision=request_id,
            )
            if action["basis"] != self.store.basis(row):
                value["state"] = "superseded"
            self.store.put(conn, "communication_actions", value)
            if value["state"] == "awaiting_approval":
                self.store.request_authority(conn, row, value)
            return value

        with self.publication_lock, self.leases.use("communication-publish"):
            return self.runtime.store.command(
                self.runtime.header, "communication_inspect", payload, request_id, apply
            )

    def close(self):
        self.stop.set()
        self.tests.request_close()
        if not self.started:
            self.finished.set()
        self.finished.wait()
        self.tests.settle()
        if not self.publication_lock.acquire(blocking=False):
            raise ClosePending(
                "A communication control still owns its platform connection."
            )
        self.publication_lock.release()
        for scope, (adapter, _identity) in self.projects.items():
            if scope not in self.closed:
                adapter.close()
                self.closed.add(scope)
        self.leases.close()
