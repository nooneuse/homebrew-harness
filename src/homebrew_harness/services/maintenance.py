"""Background source/reasoning coordinator; short state transitions stay transactional."""

import json
import threading
import time

from homebrew_harness.execution.model_gateway import Deadline
from homebrew_harness.execution.model_connections import read_environment
from homebrew_harness.services.agent import ServiceAgent
from .maintenance_store import MaintenanceStore
from .maintenance_spec import text
from .sources import RepositorySource, retry_after


class Maintenance:
    def __init__(self, runtime):
        self.runtime = runtime
        self.store = MaintenanceStore(
            runtime.store, runtime.header, runtime.spec.maintenance
        )
        self.stop = threading.Event()
        self.finished = threading.Event()
        self.sources = {}
        self.agent = None
        self.started = False
        self.error = None
        self.closed_sources = set()
        self.agent_closed = False
        self.delivery = None
        self.communication = None
        settings = runtime.core.config.settings
        environment = (
            read_environment(settings.env_file, runtime.store.root)
            if settings.env_file
            else __import__("os").environ
        )
        try:
            for spec in runtime.spec.sources:
                self.sources[spec["name"]] = RepositorySource(
                    spec,
                    environment,
                    roots=(runtime.store.root, runtime.spec.workspace),
                )
            self.agent = ServiceAgent(runtime, self.store, self.stop)
            if runtime.spec.communication:
                from .communication.runtime import CommunicationRuntime

                self.communication = CommunicationRuntime(
                    runtime, self.store, environment
                )
            if runtime.spec.delivery:
                from .delivery.runtime import Delivery

                self.delivery = Delivery(runtime, self.store, environment)
        except BaseException:
            if self.communication:
                self.communication.close()
            if self.delivery:
                self.delivery.close()
            if self.agent:
                self.agent.close()
            for source in self.sources.values():
                source.close()
            raise

    def identity(self):
        return {
            "delivery": self.delivery.identity() if self.delivery else None,
            **(
                {"communication": self.communication.identity()}
                if self.communication
                else {}
            ),
            "agent": self.agent.identity(),
            "sources": {name: source.identity for name, source in self.sources.items()},
        }

    def loop(self):
        self.started = True
        try:
            while not self.stop.wait(0.05):
                meta = self.runtime.store.get(self.store.id)
                if meta["state"] == "stopping":
                    return
                if meta["state"] == "paused" and meta["scope"] == "all":
                    continue
                for name, source in self.sources.items():
                    if self.stop.is_set():
                        break
                    for _ in range(source.spec["max_pages"]):
                        old = self.runtime.store.runtime.get(
                            "service_sources",
                            self.store.id + ":" + name,
                            {"cursor": {}, "next_poll": 0},
                        )
                        if old["next_poll"] > time.time() or self.stop.is_set():
                            break
                        try:
                            page = source.poll(old["cursor"], Deadline.after(5))
                            with self.runtime.lock:
                                self.store.source_page(name, page, source.scope)
                        except Exception as error:
                            self.store.source_error(
                                name,
                                old,
                                error,
                                retry_after(error, old.get("attempts", 0) + 1),
                            )
                            break
                with self.store.db.write() as conn:
                    self.store.report(conn)
                job = self.store.claim_job()
                if job is None:
                    continue
                result = None
                try:
                    result = self.agent.run(job)
                    if self.stop.is_set():
                        raise RuntimeError("Service stopped during reasoning.")
                    with self.runtime.lock:
                        self.store.finish(job, result)
                except Exception as error:
                    with self.runtime.lock:
                        self.store.finish(job, result=result, error=error)
        except Exception as error:
            self.error = type(error).__name__
        finally:
            self.finished.set()

    def close(self):
        self.stop.set()
        if self.communication:
            self.communication.stop.set()
            self.communication.tests.request_close()
        if self.delivery:
            self.delivery.stop.set()
            self.delivery.git.stop.set()
            self.delivery.tests.request_close()
        if self.communication:
            self.communication.close()
        if self.delivery:
            self.delivery.close()
        if not self.started:
            self.finished.set()
        self.finished.wait()
        for name, source in self.sources.items():
            if name not in self.closed_sources:
                source.close()
                self.closed_sources.add(name)
        if self.agent and not self.agent_closed:
            self.agent.close()
            self.agent_closed = True

    def command(self, method, payload, request_id):
        if method == "model_recover":
            return self.agent.model_control.recover(
                payload["profile"], payload["revision"], request_id
            )
        if method.startswith(("discussion_", "communication_")):
            if self.communication is None:
                raise ValueError("Communication is not configured.")
            if method == "communication_inspect":
                if payload.get("conclusion", "reconcile") != "reconcile":
                    return self.communication.inspect(payload, request_id)
                from .slow_controls import perform

                return perform(
                    self.runtime,
                    method,
                    payload,
                    request_id,
                    lambda: self.communication.inspect(payload, request_id),
                )
            if method == "discussion_prepare_review":
                return self.communication.request_review(
                    payload["id"], payload["revision"], request_id
                )
            return self.communication.store.command(method, payload, request_id)
        if method.startswith(("delivery_", "batch_")):
            if (
                method.startswith("batch_")
                and self.delivery is None
                and self.communication is not None
            ):
                from .delivery.batches import DecisionBatches

                batches = DecisionBatches(self.runtime, self.store)
                return (
                    batches.create(payload["refs"], request_id)
                    if method == "batch_create"
                    else batches.apply(
                        payload["id"],
                        payload["revision"],
                        payload["decisions"],
                        request_id,
                    )
                )
            if self.delivery is None:
                raise ValueError("Delivery is not configured for this service.")
            from .delivery.controls import command

            return command(self.delivery, method, payload, request_id)

        def apply(conn):
            meta = self.runtime.store.guard(conn, self.runtime.header)
            if meta["state"] in {"stopped", "stopping"}:
                raise ValueError("Service is stopping.")
            if method == "conversation_submit":
                return self.store.message(conn, payload, request_id)
            if method == "service_decide":
                return self.store.decide(conn, payload, request_id)
            if method == "report_create":
                return self.store.report(conn, force=True)
            if method == "report_ack":
                report = self.store.get("reports", payload["id"], conn)
                if report["state"] != "ready":
                    raise ValueError("Only a ready report can be acknowledged.")
                context = text(payload["context"], "context", 100)
                previous = (
                    self.store.db._get(
                        conn, "service_report_reads", self.store.id + ":" + context
                    )
                    or {}
                )
                self.store.db._put(
                    conn,
                    "service_report_reads",
                    self.store.id + ":" + context,
                    {
                        "id": payload["id"],
                        "activity": max(
                            previous.get("activity", 0), report["activity"]
                        ),
                        "checkpoint": "service:" + self.store.id,
                    },
                )
                return {"accepted": True, "id": payload["id"]}
            if method == "job_retry":
                job = self.store.job_get(payload["id"])
                if job["state"] != "failed":
                    raise ValueError("Only a failed reasoning job can be retried.")
                job.update(
                    state="queued",
                    next_attempt=0,
                    attempt_limit=job["attempts"] + self.store.policy["job_attempts"],
                )
                if job["kind"] in {"review", "ci_triage"}:
                    row = self.store.get("changesets", job["target"], conn)
                    self.delivery.store.current(row, conn)
                    row.update(
                        state="reviewing"
                        if job["kind"] == "review"
                        else "ci_diagnosing",
                        error=None,
                        revision=row["revision"] + 1,
                    )
                    job["revision"] = row["revision"]
                    self.store.put(conn, "changesets", row)
                if job["kind"] == "report":
                    report = self.store.get("reports", job["target"], conn)
                    report.update(state="queued", error=None)
                    self.store.put(conn, "reports", report)
                elif job["kind"] == "conversation":
                    message = self.store.get("messages", job["target"], conn)
                    message.update(state="queued", error=None)
                    self.store.put(conn, "messages", message)
                self.store.db._put(conn, "service_jobs_model", job["id"], job)
                return {"accepted": True, "id": job["id"]}
            if method in {"work_revise", "authority_revoke", "work_request_authority"}:
                work = self.runtime.store.work(payload["id"], self.store.id)
                if (
                    type(payload.get("revision")) is not int
                    or work["revision"] != payload["revision"]
                ):
                    raise ValueError("Work revision changed.")
                if method == "work_request_authority":
                    if self.store.valid_authority(work):
                        return {"id": work["id"], "state": "authorized"}
                    if (
                        not work.get("triage")
                        or work.get("triage_revision") != work["requirements_revision"]
                    ):
                        raise ValueError("Work has no validated plan yet.")
                    request = self.store.work_request(
                        conn, work, work["triage"], renew=True
                    )
                    self.store.changed(conn, work["id"])
                    return {
                        "id": request["id"],
                        "revision": request["revision"],
                        "state": request["state"],
                    }
                if method == "authority_revoke":
                    if work.get("authority"):
                        work["authority"]["revoked"] = True
                    work.update(pause_requested=True, error="authority_revoked")
                    if work["state"] != "in_progress":
                        work["state"] = "proposed"
                    if work.get("triage") and work["triage"].get("scope"):
                        self.store.work_request(conn, work, work["triage"])
                else:
                    amendment = text(payload.get("text"))
                    if work["state"] == "in_progress":
                        raise ValueError(
                            "Use steering or cancel the active Run before revising its work contract."
                        )
                    work["prompt"] = (
                        work["prompt"] + "\n\nOperator amendment:\n" + amendment
                    )
                    text(work["prompt"])
                    work["requirements_revision"] += 1
                    work.update(
                        state="new",
                        authority=None,
                        cancel_requested=False,
                        pause_requested=False,
                    )
                    work.pop("result", None)
                    self.store.job(
                        conn,
                        "triage",
                        work["id"],
                        work["requirements_revision"],
                        {},
                        work["id"] + ":" + str(work["requirements_revision"]),
                    )
                work["revision"] += 1
                self.store.db._put(conn, "service_work", work["id"], work)
                self.store.changed(conn, work["id"])
                return {
                    "id": work["id"],
                    "accepted": True,
                    "revision": work["revision"],
                }
            raise ValueError("Unknown maintenance command.")

        return self.runtime.store.command(
            self.runtime.header, method, payload, request_id, apply
        )

    def query(self, method, payload):
        if method == "requests_page":
            return self.request_page(**payload)
        if method == "service_models":
            return self.agent.model_control.view()
        if method.startswith("communication_"):
            if self.communication is None:
                raise ValueError("Communication is not configured.")
            if method == "communication_page":
                return self.communication.store.page(**payload)
            if method == "communication_events":
                return self.communication.store.event_page(
                    payload["id"], payload.get("cursor", 0), payload.get("limit", 20)
                )
            if method == "communication_read":
                return self.communication.store.read(**payload)
            return self.communication.store.get(payload["kind"], payload["id"])
        if method == "delivery_status":
            if self.delivery is None:
                raise ValueError("Delivery is not configured.")
            from .delivery.controls import status

            return status(self.delivery, payload["kind"], payload["id"])
        if method == "report_inbox":
            context = text(payload.get("context", "default"), "context", 100)
            from .maintenance_spec import number

            cursor = payload.get("cursor", 0)
            limit = payload.get("limit", 20)
            number(cursor, "cursor", 0, 2**63 - 1, integer=True)
            number(limit, "limit", 1, 100, integer=True)
            with self.store.db.connection() as conn:
                read = (
                    self.store.db._get(
                        conn, "service_report_reads", self.store.id + ":" + context
                    )
                    or {}
                )
                rows = conn.execute(
                    "SELECT rowid,value FROM records WHERE section='service_reports' AND owner=? AND rowid>? AND json_extract(value,'$.state')='ready' AND json_extract(value,'$.activity')>? ORDER BY rowid LIMIT ?",
                    (
                        "service:" + self.store.id,
                        cursor,
                        read.get("activity", 0),
                        limit + 1,
                    ),
                ).fetchall()
            return {
                "items": [
                    {
                        k: v
                        for k, v in json.loads(raw).items()
                        if k not in {"text", "facts"}
                    }
                    for _, raw in rows[:limit]
                ],
                "next_cursor": rows[limit - 1][0] if len(rows) > limit else None,
            }
        if method == "service_page":
            return self.store.page(**payload)
        if method == "service_read":
            return self.store.read(**payload)
        if method == "job_get":
            return self.store.job_get(payload["id"])
        if method == "conversation_get":
            return self.store.get("messages", payload["id"])
        if method == "report_get":
            return self.store.get("reports", payload["id"])
        raise ValueError("Unknown maintenance query.")

    def request_page(self, **arguments):
        from .request_views import page

        return page(self, **arguments)

    def requests(self, work_id=None):
        current = self.request_page(work_id=work_id, limit=100)["items"]
        return (
            current
            or self.request_page(work_id=work_id, limit=100, expired=True)["items"]
        )
