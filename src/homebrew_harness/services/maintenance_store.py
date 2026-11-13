"""Service-owned events, proposals, decisions, conversations and report windows."""

import json
import time

from .protocol import request_key, fingerprint
from .maintenance_spec import text, number

SECTIONS = {
    "events",
    "sources",
    "jobs",
    "decisions",
    "messages",
    "reports",
    "work",
    "evidence",
    "changesets",
    "actions",
    "batches",
    "audits",
    "incidents",
    "upgrade_plans",
    "operations",
    "requirements",
}


def section(kind):
    return "service_jobs_model" if kind == "jobs" else "service_" + kind


class MaintenanceStore:
    def __init__(self, store, header, policy):
        self.store, self.db, self.header, self.policy = store, store.db, header, policy
        self.id = header["id"]
        self.delivery = None
        self.communication = None

    def get(self, kind, identity, conn=None):
        if kind not in SECTIONS:
            raise ValueError("Unknown service record kind.")
        value = (
            self.db._get(conn, section(kind), identity)
            if conn is not None
            else self.store.runtime.get(section(kind), identity)
        )
        if not value or value.get("service_id") != self.id:
            raise ValueError("Record belongs to another service.")
        return value

    def put(self, conn, kind, value):
        value = {**value, "service_id": self.id, "checkpoint": "service:" + self.id}
        self.db._put(conn, section(kind), value["id"], value)
        return value

    def page(self, kind, *, cursor=0, limit=20, state=None):
        if kind not in SECTIONS:
            raise ValueError("Unknown service record kind.")
        number(cursor, "cursor", 0, 2**63 - 1, integer=True)
        number(limit, "limit", 1, 100, integer=True)
        if state is not None:
            text(state, "state", 40)
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT rowid,value FROM records WHERE section=? AND owner=? AND rowid>? AND (? IS NULL OR json_extract(value,'$.state')=?) ORDER BY rowid LIMIT ?",
                (
                    section(kind),
                    "service:" + self.id,
                    cursor,
                    state,
                    state,
                    limit + 1,
                ),
            ).fetchall()
        items = []
        for row, raw in rows[:limit]:
            value = json.loads(raw)
            for field in (
                "input",
                "result",
                "content",
                "text",
                "facts",
                "history",
                "candidate",
                "project_context",
                "discussion_assessment",
                "discussion_prompts",
                "phase_outputs",
                "unapplied_result",
                "integration",
                "integration_quality",
                "merge_rules",
                "candidate_history",
            ):
                if field in value:
                    value[field + "_available"] = True
                    value.pop(field)
            items.append(value)
        return {
            "items": items,
            "next_cursor": rows[limit - 1][0] if len(rows) > limit else None,
        }

    def read(self, kind, identity, *, offset=0, limit=4000):
        number(offset, "offset", 0, 2**63 - 1, integer=True)
        number(limit, "limit", 1, 16000, integer=True)
        value = self.get(kind, identity)
        raw = json.dumps(value, ensure_ascii=False)
        return {
            "id": identity,
            "offset": offset,
            "length": len(raw),
            "text": raw[offset : offset + limit],
            "next_offset": offset + limit if offset + limit < len(raw) else None,
        }

    def changed(self, conn, work_id=None, *, detail=None):
        meta = self.store.guard(conn, self.header)
        meta["activity"] = meta.get("activity", 0) + 1
        self.db._put(conn, "service_meta", self.id, meta)
        work = self.db._get(conn, "service_work", work_id) if work_id else None
        snapshot = (
            self.store.summary(work)
            if work
            else detail or {"service_state": meta["state"]}
        )
        snapshot.pop("result", None)
        self.db._put(
            conn,
            "service_activity",
            self.id + ":" + str(meta["activity"]),
            {
                "sequence": meta["activity"],
                "snapshot": snapshot,
                "created_at": time.time(),
                "checkpoint": "service:" + self.id,
            },
        )

    def job(self, conn, kind, target, revision, payload, key):
        from .job_contracts import HANDLERS

        if kind not in HANDLERS:
            raise ValueError("Unknown service job kind.")
        identity = "job_" + request_key("job", self.id, key)
        old = self.db._get(conn, "service_jobs_model", identity)
        if old:
            return old
        value = {
            "id": identity,
            "kind": kind,
            "target": target,
            "revision": revision,
            "input": payload,
            "state": "queued",
            "attempts": 0,
            "created_at": time.time(),
            "next_attempt": 0,
        }
        value.update(service_id=self.id, checkpoint="service:" + self.id)
        self.db._put(conn, "service_jobs_model", identity, value)
        return value

    def source_page(self, name, page, scope):
        from homebrew_harness.platforms.contracts import SourcePage

        if not isinstance(page, SourcePage):
            raise ValueError("Invalid source page.")
        page.validate(scope)
        if self.communication is not None and scope in self.communication.scopes:
            return self.communication.ingest(name, page.events, scope, page=page)
        with self.db.write() as conn:
            self.store.guard(conn, self.header)
            for item in page.events:
                event = item.data()
                eid = "event_" + request_key(
                    "event", self.id, fingerprint(event["key"])
                )
                prior = self.db._get(conn, "service_events", eid)
                if prior:
                    if name not in prior.get("sources", [prior["source"]]):
                        prior["sources"] = [
                            *prior.get("sources", [prior["source"]]),
                            name,
                        ]
                        self.put(conn, "events", prior)
                    continue
                relation = self.id + ":" + item.subject.key()
                link = self.db._get(conn, "service_source_work", relation)
                if link is None:
                    content = event["content"]
                    prompt = json.dumps(
                        {
                            "source": name,
                            "subject": event["subject"],
                            "title": content["title"],
                            "body": content["body"],
                        },
                        ensure_ascii=False,
                    )
                    work = self.store.enqueue(conn, self.id, prompt, relation)
                    work.update(
                        state="new",
                        events=[eid],
                        source=name,
                        subject=event["subject"],
                        run_history=[],
                        triage=None,
                        authority=None,
                    )
                    self.db._put(
                        conn, "service_source_work", relation, {"id": work["id"]}
                    )
                else:
                    work = self.db._get(conn, "service_work", link["id"])
                    work["events"] = [*work.get("events", []), eid]
                    work["requirements_revision"] += 1
                    work["revision"] += 1
                    if work["run_id"] and work["state"] == "in_progress":
                        work.update(
                            pause_requested=True,
                            review_required=True,
                            error="source_changed",
                        )
                    else:
                        work.update(state="new", authority=None)
                        work.pop("result", None)
                self.db._put(conn, "service_work", work["id"], work)
                self.put(
                    conn,
                    "events",
                    {
                        **event,
                        "id": eid,
                        "work_id": work["id"],
                        "source": name,
                        "sources": [name],
                        "received_at": time.time(),
                    },
                )
                self.job(
                    conn,
                    "triage",
                    work["id"],
                    work["requirements_revision"],
                    {},
                    work["id"] + ":" + str(work["requirements_revision"]),
                )
                self.changed(conn, work["id"])
            self.put(
                conn,
                "sources",
                {
                    "id": self.id + ":" + name,
                    "name": name,
                    "cursor": page.cursor,
                    "next_poll": page.next_poll,
                    "state": page.state,
                    "attempts": 0,
                    "error": None,
                    "last_success": time.time(),
                },
            )

    def source_error(self, name, old, error, delay):
        with self.db.write() as conn:
            self.store.guard(conn, self.header)
            if old.get("error") != type(error).__name__:
                self.changed(
                    conn, detail={"source": name, "error": type(error).__name__}
                )
            self.put(
                conn,
                "sources",
                {
                    **old,
                    "id": self.id + ":" + name,
                    "name": name,
                    "state": "retrying",
                    "error": type(error).__name__,
                    "attempts": old.get("attempts", 0) + 1,
                    "next_poll": time.time() + delay,
                },
            )

    def fail_target(self, conn, job):
        if job["kind"] == "discussion" and self.communication is not None:
            row = self.communication.get("discussions", job["target"], conn)
            row.update(state="blocked", error=job.get("error", "model_job_failed"))
            self.communication.put(conn, "discussions", row)
            return
        if job["kind"] in {"audit", "incident", "upgrade_assessment"}:
            kind = {
                "audit": "audits",
                "incident": "incidents",
                "upgrade_assessment": "upgrade_plans",
            }[job["kind"]]
            row = self.get(kind, job["target"], conn)
            row.update(error=job["error"], revision=row["revision"] + 1)
            if kind == "audits":
                row["state"] = "failed"
            self.put(conn, kind, row)
        elif job["kind"] in {"review", "ci_triage"}:
            row = self.get("changesets", job["target"], conn)
            row.update(
                state="quality_failed" if job["kind"] == "review" else "blocked",
                error=job["error"],
                revision=row["revision"] + 1,
            )
            self.put(conn, "changesets", row)
        elif job["kind"] in {"conversation", "report"}:
            kind = "messages" if job["kind"] == "conversation" else "reports"
            target = self.get(kind, job["target"], conn)
            target.update(state="failed", error=job["error"])
            self.put(conn, kind, target)
        elif job["kind"] == "triage":
            work = self.db._get(conn, "service_work", job["target"])
            if (
                work["requirements_revision"] == job["revision"]
                and work["state"] == "new"
            ):
                work.update(error="triage_failed", revision=work["revision"] + 1)
                self.db._put(conn, "service_work", work["id"], work)
        if job["kind"] != "report":
            self.changed(
                conn,
                detail={
                    "job_id": job["id"],
                    "kind": job["kind"],
                    "target": job["target"],
                    "error": job["error"],
                },
            )

    def claim_job(self):
        with self.db.write() as conn:
            self.store.guard(conn, self.header)
            rows = conn.execute(
                "SELECT value FROM records WHERE section='service_jobs_model' AND owner=? AND json_extract(value,'$.state') IN ('queued','running') ORDER BY CASE json_extract(value,'$.kind') WHEN 'conversation' THEN 0 ELSE 1 END,rowid LIMIT 32",
                ("service:" + self.id,),
            ).fetchall()
            for (raw,) in rows:
                job = json.loads(raw)
                if job["kind"] == "triage":
                    work = self.db._get(conn, "service_work", job["target"])
                    if (
                        work["requirements_revision"] != job["revision"]
                        or work["cancel_requested"]
                    ):
                        job.update(state="stale")
                        self.db._put(conn, "service_jobs_model", job["id"], job)
                        continue
                if job.get("next_attempt", 0) > time.time():
                    continue
                if (
                    job["state"] == "running"
                    and job.get("generation") == self.header["generation"]
                ):
                    continue
                if job["attempts"] >= job.get(
                    "attempt_limit", self.policy["job_attempts"]
                ):
                    job.update(state="failed", error="attempts_exhausted")
                    self.fail_target(conn, job)
                    self.db._put(conn, "service_jobs_model", job["id"], job)
                    continue
                job.update(
                    state="running",
                    attempts=job["attempts"] + 1,
                    generation=self.header["generation"],
                )
                self.db._put(conn, "service_jobs_model", job["id"], job)
                return job
        return None

    def job_get(self, identity):
        value = self.store.runtime.get("service_jobs_model", identity)
        if not value or value["service_id"] != self.id:
            raise ValueError("Unknown service job.")
        return value

    def work_request(self, conn, work, proposal, *, renew=False):
        rid = "request_" + request_key(
            "authority",
            self.id,
            work["id"]
            + ":"
            + str(work["requirements_revision"])
            + ":"
            + fingerprint(proposal)
            + ":"
            + str(work.get("grant_round", 0)),
        )
        request = {
            "id": rid,
            "work_id": work["id"],
            "requirements_revision": work["requirements_revision"],
            "revision": 0,
            "kind": "work_information"
            if proposal["disposition"] == "needs_info"
            else "work_authority",
            "state": "pending",
            "candidate": proposal,
            "created_at": time.time(),
            "expires_at": time.time() + self.policy["authority_seconds"],
        }
        previous = self.db._get(conn, "service_decisions", rid)
        if previous and renew:
            previous.update(
                revision=previous["revision"] + 1,
                expires_at=request["expires_at"],
                state="pending",
            )
            return self.put(conn, "decisions", previous)
        return previous or self.put(conn, "decisions", request)

    def grant(self, conn, work, proposal, origin):
        scope = proposal["scope"]
        candidate = (
            self.db._get(conn, "service_changesets", work.get("changeset_id"))
            if work.get("changeset_id")
            else None
        )
        if (
            candidate
            and candidate["state"] == "repair_waiting_authority"
            and work["state"] == "review"
            and proposal == work.get("triage")
            and candidate["requirements_revision"] == work["requirements_revision"]
        ):
            work.update(
                authority={
                    "requirements_revision": work["requirements_revision"],
                    "policy": fingerprint(self.policy),
                    "scope": scope,
                    "expires_at": time.time() + self.policy["authority_seconds"],
                    "origin": origin,
                    "revoked": False,
                },
                revision=work["revision"] + 1,
            )
            candidate.update(
                state="repair_approved", revision=candidate["revision"] + 1
            )
            self.db._put(conn, "service_work", work["id"], work)
            self.put(conn, "changesets", candidate)
            self.changed(conn, work["id"])
            return
        compiled = self.store.get(self.id)["spec"]["run_defaults"]
        from homebrew_harness.runs.spec import RunSpec

        supplemental = []
        latest = {}
        for eid in work.get("events", []):
            event = self.get("events", eid, conn)
            key = fingerprint(event["object"])
            if key not in latest or event["version"] >= latest[key]["version"]:
                latest[key] = event
        for event in latest.values():
            if work.get("events") and event["id"] == work["events"][0]:
                continue
            supplemental.append(
                json.dumps(
                    {
                        "source": event["source"],
                        "object": event["object"],
                        "subject": event["subject"],
                        "version": event["version"],
                        "content": event["content"],
                    },
                    ensure_ascii=False,
                )
            )
        prompt = (
            work["prompt"]
            + (
                __import__(
                    "homebrew_harness.services.requirements", fromlist=["instruction"]
                ).instruction(self.get("requirements", work["requirements_id"], conn))
                if work.get("requirements_id")
                else ""
            )
            + (
                "\n\nCurrent source requirements (external material):\n"
                + "\n".join(supplemental)
                if supplemental
                else ""
            )
            + (
                "\n\nOperator execution amendments:\n"
                + "\n".join(work.get("amendments", []))
                if work.get("amendments")
                else ""
            )
            + "\n\nHost maintenance objectives:\n"
            + self.policy["objectives"]
            + "\n\nHost non-goals and constraints:\n"
            + self.policy["non_goals"]
            + "\n\nExecution plan (original request remains authoritative):\n"
            + proposal["plan"]
        )
        from .maintenance_spec import instructions_for

        guidance = instructions_for(self.policy, "development")
        if guidance:
            prompt += "\n\nConfigured development guidance:\n" + "\n\n".join(guidance.values())
        settings = {**compiled, **self.policy["scopes"][scope]}
        if work.get("goal") is not None:
            settings["goal"] = work["goal"]
        spec = RunSpec(prompt, **settings)
        if work.get("run_id"):
            work["run_history"] = [*work.get("run_history", []), work["run_id"]]
        authority = {
            "requirements_revision": work["requirements_revision"],
            "policy": fingerprint(self.policy),
            "scope": scope,
            "expires_at": time.time() + self.policy["authority_seconds"],
            "origin": origin,
            "revoked": False,
        }
        work["grant_round"] = work.get("grant_round", 0) + 1
        work["revision"] += 1
        work.update(
            authority=authority,
            state="approved",
            run_spec=spec.data(),
            baseline=None,
            repair_of=None,
            run_id=None,
            launch_id="service_"
            + request_key(
                "launch",
                self.id,
                work["id"]
                + ":"
                + str(work["requirements_revision"])
                + ":"
                + str(len(work.get("run_history", []))),
            ),
            pause_requested=work.get("pause_requested", False),
            cancel_requested=False,
            review_required=False,
            error=None,
        )
        work.pop("result", None)
        self.db._put(conn, "service_work", work["id"], work)

    def prepare_job(self, job, **values):
        if set(values) - {"project_context", "discussion_assessment", "discussion_prompts", "phase_outputs"}:
            raise ValueError("Unknown service preparation field.")
        with self.db.write() as conn:
            self.store.guard(conn, self.header)
            current = self.get("jobs", job["id"], conn)
            if (current["state"] != "running"
                    or current["generation"] != self.header["generation"]
                    or current["input"] != job["input"]):
                raise ValueError("Job preparation ownership or input changed.")
            current.update(values)
            self.put(conn, "jobs", current)

    def finish(self, job, result=None, error=None):
        with self.db.write() as conn:
            meta = self.store.guard(conn, self.header)
            current = self.db._get(conn, "service_jobs_model", job["id"])
            if (
                current["state"] != "running"
                or current["generation"] != self.header["generation"]
            ):
                raise ValueError("Job ownership changed.")
            if meta["state"] == "stopping":
                current.update(state="queued", error="service_stopping", next_attempt=0)
                self.db._put(conn, "service_jobs_model", job["id"], current)
                return current
            if error is not None:
                from homebrew_harness.security.data_boundary import redact

                current.update(
                    state="queued"
                    if current["attempts"]
                    < current.get("attempt_limit", self.policy["job_attempts"])
                    else "failed",
                    error=type(error).__name__,
                    error_detail=redact(str(error))[:2000],
                    error_stage="apply" if result is not None else "reasoning",
                    next_attempt=time.time() + min(60, 2 ** current["attempts"]),
                )
                if result is not None:
                    current["unapplied_result"] = redact(result)
                if current["state"] == "failed":
                    self.fail_target(conn, current)
                elif job["kind"] == "conversation":
                    message = self.get("messages", job["target"], conn)
                    message.update(state=current["state"], error=current["error"])
                    self.put(conn, "messages", message)
            else:
                from .job_contracts import JobContract

                contract = JobContract(self, current)
                result = contract.validate(result, conn)
                current.update(
                    state=contract.apply(conn, result),
                    result=result,
                    completed_at=time.time(),
                )
            self.db._put(conn, "service_jobs_model", job["id"], current)
            return current

    def message(self, conn, payload, request_id):
        value = text(payload.get("text"))
        context = text(payload.get("context", "default"), "context", 100)
        identity = "message_" + request_key("message", self.id, request_id)
        work_id = payload.get("work_id")
        work = self.store.work(work_id, self.id) if work_id else None
        if work and (
            type(payload.get("revision")) is not int
            or payload.get("revision") != work["requirements_revision"]
        ):
            raise ValueError("Work requirements changed.")
        record = {
            "id": identity,
            "context": context,
            "text": value,
            "work_id": work_id,
            "work_revision": work["requirements_revision"] if work else None,
            "created_at": time.time(),
            "state": "queued",
        }
        self.put(conn, "messages", record)
        job = self.job(conn, "conversation", identity, 0, {}, identity)
        record["job_id"] = job["id"]
        self.put(conn, "messages", record)
        return {
            "id": identity,
            "job_id": job["id"],
            "accepted": True,
            "request_id": request_id,
        }

    def report(self, conn, *, force=False):
        meta = self.store.guard(conn, self.header)
        activity = meta.get("activity", 0)
        if (
            activity <= meta.get("report_activity", 0)
            or not force
            and time.time()
            < meta.get(
                "report_next", meta["created_at"] + self.policy["report_seconds"]
            )
        ):
            return {"accepted": False, "reason": "no_new_activity"}
        pending = conn.execute(
            "SELECT value FROM records WHERE section='service_reports' AND owner=? AND json_extract(value,'$.state')='queued' ORDER BY rowid LIMIT 1",
            ("service:" + self.id,),
        ).fetchone()
        if pending:
            return {"id": json.loads(pending[0])["id"], "accepted": True}
        rows = conn.execute(
            "SELECT value FROM records WHERE section='service_activity' AND owner=? AND json_extract(value,'$.sequence')>? ORDER BY rowid LIMIT 100",
            ("service:" + self.id, meta.get("report_activity", 0)),
        ).fetchall()
        if not rows:
            return {"accepted": False, "reason": "no_new_activity"}
        facts = [json.loads(row[0]) for row in rows]
        activity = facts[-1]["sequence"]
        identity = "report_" + request_key("report", self.id, str(activity))
        previous = self.db._get(conn, "service_reports", identity)
        if previous:
            return {"id": identity, "accepted": True}
        report = {
            "id": identity,
            "state": "queued",
            "activity": activity,
            "created_at": time.time(),
            "from_activity": meta.get("report_activity", 0),
            "more_activity": meta.get("activity", 0) > activity,
            "facts": facts,
        }
        self.put(conn, "reports", report)
        job = self.job(conn, "report", identity, activity, {}, identity)
        report["job_id"] = job["id"]
        self.put(conn, "reports", report)
        return {"id": identity, "job_id": job["id"], "accepted": True}

    def decide(self, conn, payload, request_id):
        decision = self.get("decisions", payload["id"], conn)
        if (
            type(payload["revision"]) is not int
            or payload["revision"] != decision["revision"]
            or decision["state"] != "pending"
        ):
            raise ValueError("Decision is stale; refresh requests.")
        if decision["kind"] in {
            "communication_authority",
            "discussion_information",
            "discussion_requirements",
        }:
            if self.communication is None:
                raise ValueError("Communication is not configured.")
            return self.communication.decide(conn, decision, payload, request_id)
        if decision["kind"] in {"delivery_authority", "ci_repair"}:
            return self.delivery.store.decide(conn, decision, payload, request_id)
        work = self.store.work(decision["work_id"], self.id)
        if (
            decision["expires_at"] <= time.time()
            or decision["requirements_revision"] != work["requirements_revision"]
        ):
            raise ValueError("Decision expired or requirements changed.")
        action = payload["decision"]
        if decision["kind"] == "work_information":
            if action != "answer":
                raise ValueError("This request requires an answer, not approval.")
            answer = text(payload.get("text"), "answer", 8000)
            amended = work["prompt"] + "\n\nOperator clarification:\n" + answer
            text(amended)
            work.update(
                prompt=amended,
                requirements_revision=work["requirements_revision"] + 1,
                revision=work["revision"] + 1,
            )
            if work["state"] != "in_progress":
                work["state"] = "new"
            else:
                work["pause_requested"] = True
            self.db._put(conn, "service_work", work["id"], work)
            self.job(
                conn,
                "triage",
                work["id"],
                work["requirements_revision"],
                {},
                work["id"] + ":" + str(work["requirements_revision"]),
            )
            decision.update(
                state="answered", revision=decision["revision"] + 1, answer=answer
            )
            self.put(conn, "decisions", decision)
            self.changed(conn, work["id"])
            return {"id": decision["id"], "state": "answered", "request_id": request_id}
        if "text" in payload:
            raise ValueError("Work authority does not accept an answer.")
        if action not in {"approve", "deny"}:
            raise ValueError("Work authorization requires approve or deny.")
        if work["state"] == "in_progress":
            authority = work.get("authority")
            if (
                not authority
                or authority["requirements_revision"] != work["requirements_revision"]
                or decision["candidate"] != work.get("triage")
            ):
                raise ValueError(
                    "Finish or cancel the previous Run before authorizing replacement requirements."
                )
            if action == "approve":
                authority.update(
                    expires_at=time.time() + self.policy["authority_seconds"],
                    revoked=False,
                    origin="operator:" + request_id,
                )
                work["grant_round"] = work.get("grant_round", 0) + 1
            else:
                authority["revoked"] = True
            work["revision"] += 1
            self.db._put(conn, "service_work", work["id"], work)
        elif action == "approve":
            self.grant(conn, work, decision["candidate"], "operator:" + request_id)
        else:
            work.update(state="rejected", revision=work["revision"] + 1)
            self.db._put(conn, "service_work", work["id"], work)
        decision.update(
            state="approved" if action == "approve" else "denied",
            revision=decision["revision"] + 1,
        )
        self.put(conn, "decisions", decision)
        self.changed(conn, work["id"])
        return {
            "id": decision["id"],
            "state": decision["state"],
            "request_id": request_id,
        }

    def valid_authority(self, work):
        auth = work.get("authority")
        return bool(
            auth
            and not auth["revoked"]
            and auth["requirements_revision"] == work["requirements_revision"]
            and auth["policy"] == fingerprint(self.policy)
            and auth["expires_at"] > time.time()
        )
