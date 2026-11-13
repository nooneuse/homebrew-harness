"""Transactional discussion projections, rounds and independently authorized messages."""

import copy
import json
import time

from homebrew_harness.platforms.contracts import ObjectRef, SourceEvent, digest
from homebrew_harness.services.protocol import request_key, fingerprint
from homebrew_harness.services.maintenance_spec import text
from homebrew_harness.services.action_runtime import start_attempt, finish_attempt
from .spec import validate_plan, validate_proposal, open_questions, validate_public_questions
from .transitions import observation_event, mark_input, request_analysis

SECTIONS = {
    "discussions": "service_discussions",
    "rounds": "service_discussion_rounds",
    "review_snapshots": "service_review_snapshots",
    "review_operations": "service_review_operations",
    "communication_actions": "service_communication_actions",
}


class CommunicationStore:
    def __init__(self, runtime, maintenance, policy):
        self.runtime, self.ms, self.db, self.policy = (
            runtime,
            maintenance,
            maintenance.db,
            policy,
        )
        self.id = maintenance.id
        self.scopes = {
            tuple(p[k] for k in ("provider", "instance", "project"))
            for p in policy["projects"]
        }
        self.actor_ids = {}

    def get(self, kind, identity, conn=None):
        if kind not in SECTIONS:
            raise ValueError("Unknown communication record kind.")
        value = (
            self.db._get(conn, SECTIONS[kind], identity)
            if conn is not None
            else self.ms.store.runtime.get(SECTIONS[kind], identity)
        )
        if not value or value["service_id"] != self.id:
            raise ValueError("Communication record belongs to another service.")
        return value

    def put(self, conn, kind, value):
        value = {**value, "service_id": self.id, "checkpoint": "service:" + self.id}
        self.db._put(conn, SECTIONS[kind], value["id"], value)
        return value

    def page(self, kind, cursor=0, limit=20, state=None):
        from homebrew_harness.services.maintenance_spec import number

        number(cursor, "cursor", 0, 2**63 - 1, integer=True)
        number(limit, "limit", 1, 100, integer=True)
        if kind not in SECTIONS:
            raise ValueError("Unknown communication record kind.")
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT rowid,value FROM records WHERE section=? AND owner=? AND rowid>? AND (? IS NULL OR json_extract(value,'$.state')=?) ORDER BY rowid LIMIT ?",
                (SECTIONS[kind], "service:" + self.id, cursor, state, state, limit + 1),
            ).fetchall()
        values = []
        for _, raw in rows[:limit]:
            value = json.loads(raw)
            for key in (
                "result",
                "input",
                "files",
                "changed",
                "plan",
                "projection",
                "body",
            ):
                if key in value:
                    value.pop(key)
                    value[key + "_available"] = True
            values.append(value)
        return {
            "items": values,
            "next_cursor": rows[limit - 1][0] if len(rows) > limit else None,
        }

    def read(self, kind, identity, offset=0, limit=4000):
        from homebrew_harness.services.maintenance_spec import number

        number(offset, "offset", 0, 2**63 - 1, integer=True)
        number(limit, "limit", 1, 16000, integer=True)
        raw = json.dumps(self.get(kind, identity), ensure_ascii=False)
        return {
            "id": identity,
            "offset": offset,
            "length": len(raw),
            "text": raw[offset : offset + limit],
            "next_offset": offset + limit if offset + limit < len(raw) else None,
        }

    def basis(self, row):
        return fingerprint(
            {
                "input": row["input_revision"],
                "epoch": row["analysis_epoch"],
                "draft": row["draft_revision"],
                "review": row.get("review_id"),
                "policy": self.policy,
                "maintenance": self.ms.policy,
            }
        )

    def event_identity(self, item):
        metadata = item.content.get("metadata") or {}
        return "event_" + request_key(
            "event",
            self.id,
            digest(
                [
                    item.object.key(),
                    item.subject.key(),
                    item.version,
                    item.content.get("title", ""),
                    item.content.get("body", ""),
                    item.content.get("state"),
                    {
                        k: metadata[k]
                        for k in (
                            "path",
                            "line",
                            "side",
                            "commit",
                            "review_state",
                            "review_basis",
                        )
                        if k in metadata
                    },
                ]
            ),
        )

    def echo(self, conn, item):
        body = item.content.get("body", "")
        if (
            type(item.content.get("actor_id")) is not int
            or item.content["actor_id"] <= 0
        ):
            return None
        rows = conn.execute(
            "SELECT value FROM records WHERE section IN ('service_communication_actions','service_actions') AND owner=? AND json_extract(value,'$.state') IN ('started','unknown','confirmed')",
            ("service:" + self.id,),
        ).fetchall()
        for (raw,) in rows:
            action = json.loads(raw)
            plan = action.get("plan") or {}
            result = action.get("result") or {}
            target = plan.get("subject") or result.get("subject")
            if not target:
                delivery = self.runtime.spec.delivery
                if (
                    action.get("kind") != "ensure_change_request"
                    or not delivery
                    or item.object != item.subject
                    or item.subject.scope()
                    != tuple(delivery[k] for k in ("provider", "instance", "project"))
                ):
                    continue
            elif ObjectRef(**target) != item.subject:
                continue
            marker = (
                "<!-- harness-communication:" + action["id"] + " -->"
                if action["id"].startswith("communication_")
                else "<!-- harness-delivery:" + action["id"] + " -->"
            )
            expected = (plan.get("body") or "") + "\n\n" + marker
            if body != expected:
                if self.inline_candidate(action, item):
                    if (
                        result.get("confirmed")
                        and str(result.get("id"))
                        == item.content["metadata"]["review_id"]
                        and result.get("actor_id") == item.content.get("actor_id")
                    ):
                        return "echo"
                    if action["state"] in {"started", "unknown"} and item.content.get(
                        "actor_id"
                    ) == self.actor_ids.get(item.subject.scope()):
                        return "pending_echo"
                continue
            if result.get("confirmed"):
                ref = result.get("object")
                if (
                    ref
                    and ObjectRef(**ref) == item.object
                    and result.get("actor_id") == item.content.get("actor_id")
                ):
                    return "echo"
            if action["state"] in {"started", "unknown"} and item.content.get(
                "actor_id"
            ) == self.actor_ids.get(item.subject.scope()):
                return "pending_echo"
        return None

    @staticmethod
    def inline_candidate(action, item):
        plan = action.get("plan") or {}
        metadata = item.content.get("metadata") or {}
        if (
            plan.get("kind") != "submit_review"
            or plan.get("subject") != item.subject.data()
            or metadata.get("channel") != "inline"
            or not metadata.get("review_id")
            or metadata.get("reply_to")
            or metadata.get("commit") != (plan.get("review_basis") or {}).get("head")
        ):
            return False
        return any(
            c.get("body") == item.content.get("body")
            and c.get("path") == metadata.get("path")
            and c.get("line") == metadata.get("line")
            and c.get("side", "RIGHT") == metadata.get("side", "RIGHT")
            for c in plan.get("comments", [])
        )

    def observed_events(self, subject, observed, *, complete=False, through=None):
        items = [
            SourceEvent(
                ObjectRef(**item["object"]),
                ObjectRef(**item["subject"]),
                item["version"],
                item["content"],
            )
            for item in observed
        ]
        if not complete:
            return items
        did = "discussion_" + request_key(
            "discussion", self.id, ObjectRef(**subject).key()
        )
        present = {item.object.key() for item in items}
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT value FROM (SELECT value,row_number() OVER (PARTITION BY json_extract(value,'$.object') "
                "ORDER BY json_extract(value,'$.sequence') DESC) AS position FROM records "
                "WHERE section='service_events' AND owner=? AND json_extract(value,'$.discussion_id')=?) "
                "WHERE position=1",
                ("service:" + self.id, did),
            ).fetchall()
        for (raw,) in rows:
            event = json.loads(raw)
            ref = ObjectRef(**event["object"])
            if (
                through is not None
                and event["sequence"] > through
                or ref == ObjectRef(**subject)
                or ref.key() in present
                or event["content"].get("state") == "deleted"
            ):
                continue
            items.append(
                SourceEvent(
                    ref,
                    ObjectRef(**subject),
                    digest([event["version"], "observed_deleted"]),
                    {
                        **event["content"],
                        "title": "",
                        "body": "",
                        "state": "deleted",
                        "metadata": {
                            **event["content"].get("metadata", {}),
                            "observed_deleted": True,
                        },
                    },
                )
            )
        return items

    def delivery_work(self, conn, subject):
        rows = conn.execute(
            "SELECT value FROM records WHERE section='service_actions' AND owner=? "
            "AND json_extract(value,'$.kind')='ensure_change_request' "
            "AND json_extract(value,'$.state')='confirmed' "
            "AND json_extract(value,'$.result.subject.provider')=? "
            "AND json_extract(value,'$.result.subject.instance')=? "
            "AND json_extract(value,'$.result.subject.project')=? "
            "AND json_extract(value,'$.result.subject.kind')=? "
            "AND json_extract(value,'$.result.subject.id')=? LIMIT 1",
            (
                "service:" + self.id,
                *(
                    subject[k]
                    for k in ("provider", "instance", "project", "kind", "id")
                ),
            ),
        )
        for (raw,) in rows:
            action = json.loads(raw)
            if (action.get("result") or {}).get("subject") == subject:
                return action["work_id"]
        return None

    def link_delivery(self, conn, action):
        subject = (action.get("result") or {}).get("subject")
        if not subject or ObjectRef(**subject).scope() not in self.scopes:
            return
        did = "discussion_" + request_key(
            "discussion", self.id, ObjectRef(**subject).key()
        )
        row = self.db._get(conn, "service_discussions", did)
        if row and action["kind"] == "ensure_change_request":
            row["work_id"] = action["work_id"]
            self.put(conn, "discussions", row)
        self.resolve_echoes(conn, action)

    def resolve_echoes(self, conn, action):
        # A pre-receipt observation is provisional; another remote object with
        # the same body becomes ordinary input once the receipt identifies it.
        marker = (
            (
                "<!-- harness-communication:"
                if action["id"].startswith("communication_")
                else "<!-- harness-delivery:"
            )
            + action["id"]
            + " -->"
        )
        for key, raw in conn.execute(
            "SELECT key,value FROM records WHERE section='service_events' AND owner=? "
            "AND json_extract(value,'$.echo')='pending_echo'",
            ("service:" + self.id,),
        ).fetchall():
            event = json.loads(raw)
            item = SourceEvent(
                ObjectRef(**event["object"]),
                ObjectRef(**event["subject"]),
                event["version"],
                event["content"],
            )
            if not event["content"].get("body", "").endswith(
                marker
            ) and not self.inline_candidate(action, item):
                continue
            echo = self.echo(conn, item)
            if echo == "pending_echo":
                continue
            event["echo"] = echo
            self.ms.put(conn, "events", event)
            if not echo:
                row = self.get("discussions", event["discussion_id"], conn)
                row["input_revision"] += 1
                row["revision"] += 1
                if row["state"] != "paused":
                    row["state"] = "ready_to_assess"
                row.update(dirty_since=time.time(), ready_after=0)
                self.put(conn, "discussions", row)
                self.supersede(conn, row)

    def ingest(self, name, items, scope, *, page=None, observed=False):
        if scope not in self.scopes:
            raise ValueError("Discussion crosses host project scope.")
        with self.db.write() as conn:
            self.runtime.store.guard(conn, self.runtime.header)
            for item in items:
                if not isinstance(item, SourceEvent) or item.subject.scope() != scope:
                    raise ValueError("Invalid communication source event.")
                eid = observation_event(
                    self, conn, item, self.event_identity(item), observed=observed
                )
                if eid is None:
                    continue
                echo = self.echo(conn, item)
                did = "discussion_" + request_key(
                    "discussion", self.id, item.subject.key()
                )
                row = self.db._get(conn, "service_discussions", did)
                if row is None:
                    row = {
                        "id": did,
                        "subject": item.subject.data(),
                        "work_id": self.delivery_work(conn, item.subject.data()),
                        "source": name,
                        "state": "ready_to_assess",
                        "revision": 0,
                        "seq": 0,
                        "processed_seq": 0,
                        "input_revision": 0,
                        "assessed_input_revision": 0,
                        "analysis_epoch": 0,
                        "draft_revision": 0,
                        "review_id": None,
                        "observed_basis": None,
                        "next_poll": 0,
                        "projection": {
                            "facts": [],
                            "questions": [],
                            "findings": [],
                            "draft": None,
                        },
                        "no_progress": 0,
                        "created_at": time.time(),
                        "dirty_since": time.time(),
                        "ready_after": time.time() + self.policy["debounce_seconds"],
                    }
                if item.object == item.subject:
                    metadata = item.content.get("metadata") or {}
                    field = (
                        "review_observation_hash"
                        if metadata.get("review_basis")
                        else "subject_hash"
                    )
                    semantic = (
                        digest(metadata["review_basis"])
                        if metadata.get("review_basis")
                        else digest(
                            [
                                item.content.get("title", ""),
                                item.content.get("body", ""),
                                item.content.get("state"),
                            ]
                        )
                    )
                    if row.get(field) == semantic:
                        echo = echo or "unchanged_subject"
                    row[field] = semantic
                row["seq"] += 1
                event = {
                    **item.data(),
                    "id": eid,
                    "discussion_id": did,
                    "sequence": row["seq"],
                    "source": name,
                    "received_at": time.time(),
                    "echo": echo,
                }
                self.ms.put(conn, "events", event)
                if not echo:
                    mark_input(row, self.policy)
                    if (item.content.get("metadata") or {}).get("review_basis"):
                        observed = item.content["metadata"]["review_basis"]
                        if row.get("observed_basis") != observed:
                            row["observed_basis"] = observed
                            row["review_id"] = None
                            row.pop("review_operation", None)
                    self.supersede(conn, row)
                self.put(conn, "discussions", row)
                self.ms.changed(
                    conn,
                    row["work_id"],
                    detail={"discussion_id": did, "state": row["state"]},
                )
            if page is not None:
                self.ms.put(
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

    def supersede(self, conn, row):
        for key, raw in conn.execute(
            "SELECT key,value FROM records WHERE section='service_communication_actions' AND owner=? AND json_extract(value,'$.discussion_id')=? AND json_extract(value,'$.state') IN ('queued','awaiting_approval')",
            ("service:" + self.id, row["id"]),
        ).fetchall():
            action = json.loads(raw)
            if action["basis"] != self.basis(row):
                action.update(
                    state="superseded",
                    error="discussion_changed",
                    revision=action["revision"] + 1,
                )
                self.put(conn, "communication_actions", action)
        for key, raw in conn.execute(
            "SELECT key,value FROM records WHERE section='service_decisions' AND owner=? AND json_extract(value,'$.discussion_id')=? AND json_extract(value,'$.state')='pending'",
            ("service:" + self.id, row["id"]),
        ).fetchall():
            decision = json.loads(raw)
            if decision.get("basis") != self.basis(row):
                decision.update(state="stale", revision=decision["revision"] + 1)
                self.ms.put(conn, "decisions", decision)

    def event_page(self, identity, cursor=0, limit=20):
        from homebrew_harness.services.maintenance_spec import number

        number(cursor, "cursor", 0, 2**63 - 1, integer=True)
        number(limit, "limit", 1, 100, integer=True)
        row = self.get("discussions", identity)
        with self.db.connection() as conn:
            data = conn.execute(
                "SELECT value FROM records WHERE section='service_events' AND owner=? AND json_extract(value,'$.discussion_id')=? AND json_extract(value,'$.sequence')>? ORDER BY json_extract(value,'$.sequence') LIMIT ?",
                ("service:" + self.id, row["id"], cursor, limit + 1),
            ).fetchall()
        values = [json.loads(v[0]) for v in data]
        return {
            "items": [
                {
                    k: e.get(k)
                    for k in ("id", "sequence", "object", "subject", "version", "echo")
                }
                for e in values[:limit]
            ],
            "next_cursor": values[limit - 1]["sequence"]
            if len(values) > limit
            else None,
        }

    def history(self, row, conn=None, after=0, through=None):
        def query(c):
            data = c.execute(
                "SELECT value FROM records WHERE section='service_events' AND owner=? AND json_extract(value,'$.discussion_id')=? AND json_extract(value,'$.sequence')>? AND json_extract(value,'$.sequence')<=? AND json_extract(value,'$.echo') IS NULL ORDER BY json_extract(value,'$.sequence') LIMIT 100",
                (
                    "service:" + self.id,
                    row["id"],
                    after,
                    through if through is not None else row["seq"],
                ),
            ).fetchall()
            return [json.loads(v[0]) for v in data if not json.loads(v[0]).get("echo")]

        if conn is not None:
            return query(conn)
        with self.db.connection() as c:
            return query(c)

    def queue_round(self, row, force=False):
        with self.db.write() as conn:
            self.runtime.store.guard(conn, self.runtime.header)
            row = self.get("discussions", row["id"], conn)
            force = force or row["analysis_epoch"] > row.get("assessed_epoch", 0)
            if (
                row["state"] in {"paused", "waiting_operator"}
                or row.get("assessed_input_revision", 0) >= row["input_revision"]
                and row["processed_seq"] >= row["seq"]
                and not force
            ):
                if row["state"] == "ready_to_assess":
                    row["state"] = "waiting_participant"
                    self.put(conn, "discussions", row)
                return None
            if not force and row.get("ready_after", 0) > time.time():
                return None
            running = conn.execute(
                "SELECT 1 FROM records WHERE section='service_jobs_model' AND owner=? AND json_extract(value,'$.kind')='discussion' AND json_extract(value,'$.target')=? AND json_extract(value,'$.state') IN ('queued','running')",
                ("service:" + self.id, row["id"]),
            ).fetchone()
            if running:
                return None
            basis = self.basis(row)
            history = self.history(row, conn, after=row["processed_seq"])
            if not history and not force:
                row["processed_seq"] = row["seq"]
                row["assessed_input_revision"] = row["input_revision"]
                if row["state"] == "ready_to_assess":
                    row["state"] = "waiting_participant"
                self.put(conn, "discussions", row)
                return None
            through = history[-1]["sequence"] if history else row["seq"]
            payload = {
                "policy_hash": fingerprint([self.policy, self.ms.policy]),
                "discussion_id": row["id"],
                "subject": row["subject"],
                "basis": basis,
                "seq": through,
                "input_revision": row["input_revision"],
                "epoch": row["analysis_epoch"],
                "draft_revision": row["draft_revision"],
                "projection": copy.deepcopy(row["projection"]),
                "events": [e["id"] for e in history],
                "review_id": row["review_id"],
                "work_id": row["work_id"],
                "operator_note": row.get("operator_note"),
                "assessment_job_id": row.get("assessment_job_id"),
                "no_progress": row["no_progress"],
            }
            key = row["id"] + ":" + basis + ":" + str(through)
            job = self.ms.job(
                conn, "discussion", row["id"], row["revision"], payload, key
            )
            row.update(state="assessing", last_job=job["id"])
            self.put(conn, "discussions", row)
            return job

    def validate_evidence(self, job, result, conn=None):
        """Reject invalid frozen references before accepting a model candidate."""
        if conn is None:
            with self.db.connection() as connection:
                return self.validate_evidence(job, result, connection)
        for kind in ("facts", "questions", "findings"):
            for entry in result.get(kind, []):
                for source in entry.get("sources", []):
                    event = self.db._get(conn, "service_events", source)
                    if (
                        not event
                        or event.get("discussion_id") != job["target"]
                        or event["sequence"] > job["input"]["seq"]
                        or event.get("echo")
                    ):
                        raise ValueError(
                            "Discussion candidate cites an unknown source."
                        )
        if result.get("findings") and not job["input"].get("review_id"):
            raise ValueError(
                "Code findings require a fixed review snapshot; use facts for issue observations."
            )

    def finish(self, conn, job, result):
        result = validate_proposal(result, self.ms.policy)
        row = self.get("discussions", job["target"], conn)
        rid = "round_" + request_key("round", self.id, job["id"])
        compatible = (
            job["input"]["epoch"] == row["analysis_epoch"]
            and job["input"]["draft_revision"] == row["draft_revision"]
            and job["input"].get("review_id") == row.get("review_id")
            and job["input"]["policy_hash"]
            == fingerprint([self.policy, self.ms.policy])
            and row["state"] != "paused"
        )
        remaining = conn.execute(
            "SELECT 1 FROM records WHERE section='service_events' AND owner=? AND json_extract(value,'$.discussion_id')=? AND json_extract(value,'$.sequence')>? AND json_extract(value,'$.echo') IS NULL LIMIT 1",
            ("service:" + self.id, row["id"], job["input"]["seq"]),
        ).fetchone()
        fresh = (
            compatible and not remaining and self.basis(row) == job["input"]["basis"]
        )
        self.put(
            conn,
            "rounds",
            {
                "id": rid,
                "discussion_id": row["id"],
                "job_id": job["id"],
                "basis": job["input"]["basis"],
                "seq": job["input"]["seq"],
                "result": result,
                "state": "applied" if fresh else "superseded",
                "created_at": time.time(),
            },
        )
        if not compatible:
            if row["state"] != "paused":
                row["state"] = "ready_to_assess"
            self.put(conn, "discussions", row)
            return "stale"
        self.validate_evidence(job, result, conn)
        projection = {
            key: result.get(key, row["projection"].get(key, []))
            for key in ("facts", "questions", "findings")
        }
        projection["draft"] = (
            result.get("draft")
            if result.get("draft") is not None
            else row["projection"].get("draft")
        )
        if projection["draft"] != row["projection"].get("draft"):
            row["draft_revision"] += 1
        if job["input"].get("review_id"):
            for finding in projection["findings"]:
                finding["review_id"] = job["input"]["review_id"]
        elif projection["findings"]:
            raise ValueError("Code findings require a fixed review snapshot.")
        row.update(
            projection=projection,
            assessed_input_revision=job["input"]["input_revision"],
            assessed_epoch=job["input"]["epoch"],
            processed_seq=job["input"]["seq"],
            last_round=rid,
            dirty_since=None,
            reason=result["reason"],
            alignment=result["alignment"],
            revision=row["revision"] + 1,
        )
        row["no_progress"] = (
            0 if result.get("progress", True) else row["no_progress"] + 1
        )
        step = result["next_step"]
        row["state"] = {
            "clarify": "waiting_participant",
            "discuss": "waiting_participant",
            "wait": "waiting_participant",
            "operator": "waiting_operator",
            "develop": "ready_for_decision",
            "defer": "settled",
            "decline": "waiting_operator",
            "settle": "settled",
        }[step]
        self.put(conn, "discussions", row)
        if not fresh:
            row["state"] = "ready_to_assess"
            row["ready_after"] = 0
            self.put(conn, "discussions", row)
            return "completed"
        if step == "develop":
            self.requirements_request(conn, row, result)
        elif row["state"] == "waiting_operator" or open_questions(result, "maintainer"):
            self.information_request(conn, row)
        if job.get("discussion_assessment") and fresh:
            row["assessment_job_id"] = job["id"]
            self.put(conn, "discussions", row)
        if result.get("communication"):
            action = self.plan(conn, row, result["communication"])
            row["state"] = "awaiting_publication"
            row["last_action"] = action["id"]
            self.put(conn, "discussions", row)
        self.ms.changed(
            conn,
            row["work_id"],
            detail={"discussion_id": row["id"], "state": row["state"]},
        )
        return "completed"

    def information_request(self, conn, row):
        identity = "request_" + request_key(
            "discussion-information", self.id, row["id"] + ":" + self.basis(row)
        )
        return self.ms.put(
            conn,
            "decisions",
            {
                "id": identity,
                "kind": "discussion_information",
                "discussion_id": row["id"],
                "work_id": row["work_id"],
                "requirements_revision": 0,
                "basis": self.basis(row),
                "revision": 0,
                "state": "pending",
                "reason": row.get(
                    "reason", "Direction or progress requires an operator decision."
                ),
                "candidate": row["projection"],
                "expires_at": None,
            },
        )

    def accept_requirements(
        self, conn, row, draft, category, *, automatic=False, origin="operator"
    ):
        work = self.ms.store.work(row["work_id"], self.id) if row["work_id"] else None
        if work and work.get("run_id"):
            from homebrew_harness.storage.durable_runtime import DurableStore

            location = work.get("run_workspace") or self.runtime.spec.workspace
            native = DurableStore(location).get("runs", work["run_id"])
            if (
                not native
                or native["status"] not in {"completed", "failed", "cancelled"}
                or not native.get("cleanup_complete")
                or not native.get("finalized_at")
            ):
                raise ValueError(
                    "Settle or cancel the existing Run before accepting replacement requirements."
                )
        proposal = {
            "disposition": "accept",
            "category": category,
            "reason": row["reason"],
            "plan": draft["text"] + "\n\nAcceptance:\n" + draft["acceptance"],
            "scope": draft["scope"],
            "priority": 0,
        }
        from homebrew_harness.services.requirements import accept

        accepted = accept(
            self, conn, row, draft, "host_policy" if automatic else origin
        )
        if work is None:
            work = self.ms.store.enqueue(
                conn,
                self.id,
                draft["text"],
                row["id"] + ":accepted:" + str(row["draft_revision"]),
            )
            row["work_id"] = work["id"]
        else:
            if work.get("run_id"):
                work["run_history"] = [*work.get("run_history", []), work["run_id"]]
            work["requirements_revision"] += 1
            work["prompt"] += "\n\nAccepted discussion amendment:\n" + draft["text"]
            text(work["prompt"])
            work.update(
                run_id=None,
                run_workspace=None,
                pause_requested=False,
                cancel_requested=False,
            )
        work.update(
            requirements_id=accepted["id"],
            requirements_history=list(
                dict.fromkeys([*work.get("requirements_history", []), accepted["id"]])
            ),
            triage=proposal,
            triage_revision=work["requirements_revision"],
            authority=None,
            state="proposed",
            revision=work["revision"] + 1,
        )
        self.db._put(conn, "service_work", work["id"], work)
        if automatic:
            self.ms.grant(conn, work, proposal, "host_policy")
        else:
            self.ms.work_request(conn, work, proposal)
        row["state"] = "settled"
        return work

    def requirements_request(self, conn, row, result):
        draft = row["projection"].get("draft")
        if not draft or not draft.get("scope"):
            raise ValueError("Development proposals require scope and acceptance.")
        category = result.get("category", "feature")
        if category in self.ms.policy["auto_categories"] and row["work_id"] is None:
            self.accept_requirements(conn, row, draft, category, automatic=True)
            self.put(conn, "discussions", row)
            return None
        identity = "request_" + request_key(
            "discussion-requirements", self.id, row["id"] + ":" + self.basis(row)
        )
        return self.ms.put(
            conn,
            "decisions",
            {
                "id": identity,
                "kind": "discussion_requirements",
                "category": category,
                "discussion_id": row["id"],
                "work_id": row["work_id"],
                "requirements_revision": 0,
                "basis": self.basis(row),
                "revision": 0,
                "state": "pending",
                "reason": result["reason"],
                "candidate": draft,
                "expires_at": None,
            },
        )

    def validate_anchors(self, row, plan, conn):
        snapshot = None
        if (
            row["subject"]["kind"] == "change_request"
            and plan["kind"] == "submit_review"
        ):
            if not row.get("review_id"):
                raise ValueError("A review must have a fixed code snapshot.")
            snapshot = self.get("review_snapshots", row["review_id"], conn)
            if snapshot["basis"] != row.get("observed_basis"):
                raise ValueError("Review code basis changed.")
            if plan["event"] == "APPROVE":
                if not snapshot["coverage"]["complete"] or any(
                    f.get("status", "open") in {"open", "needs_explanation"}
                    for f in row["projection"]["findings"]
                ):
                    raise ValueError(
                        "Approval requires complete coverage and no unresolved findings."
                    )
                if self.policy["require_tests_for_approval"] and not snapshot.get(
                    "tests_passed"
                ):
                    raise ValueError("Approval requires current fixed test evidence.")
            for comment in plan.get("comments", []):
                key = comment["path"] + ":" + comment["side"]
                if comment["line"] not in snapshot.get("anchors", {}).get(key, []):
                    raise ValueError("Inline comment is outside the fixed diff.")
        if plan["kind"] == "reply_thread":
            found = conn.execute(
                "SELECT 1 FROM records WHERE section='service_events' AND owner=? AND json_extract(value,'$.discussion_id')=? AND cast(json_extract(value,'$.content.metadata.thread_id') AS TEXT)=? LIMIT 1",
                ("service:" + self.id, row["id"], plan["thread_id"]),
            ).fetchone()
            if not found:
                raise ValueError(
                    "Thread reply must reference observed discussion history."
                )
        return snapshot

    def plan(self, conn, row, proposal):
        validate_plan(proposal)
        validate_public_questions(proposal, row["projection"])
        if proposal["kind"] not in self.policy["enabled_actions"]:
            raise ValueError("Communication operation is disabled by host policy.")
        if row["state"] in {"paused", "blocked"}:
            raise ValueError("Discussion is not accepting publication.")
        snapshot = self.validate_anchors(row, proposal, conn)
        pending = conn.execute(
            "SELECT value FROM records WHERE section='service_communication_actions' AND owner=? AND json_extract(value,'$.discussion_id')=? AND json_extract(value,'$.state') IN ('awaiting_approval','queued','started','unknown')",
            ("service:" + self.id, row["id"]),
        ).fetchall()
        content = {
            **copy.deepcopy(proposal),
            "subject": row["subject"],
            "review_basis": snapshot["basis"]
            if snapshot
            else row.get("observed_basis"),
        }
        basis = self.basis(row)
        identity = "communication_" + request_key(
            "communication", self.id, fingerprint([row["id"], basis, content])
        )
        prior = self.db._get(conn, SECTIONS["communication_actions"], identity)
        if prior:
            return prior
        if pending:
            raise ValueError("This discussion already has a pending public message.")
        auto = (
            proposal["intent"] in self.policy["auto_intents"]
            and proposal["kind"] != "submit_review"
        )
        value = {
            "id": identity,
            "discussion_id": row["id"],
            "work_id": row["work_id"],
            "basis": basis,
            "plan": content,
            "plan_hash": fingerprint(content),
            "resume_state": "waiting_operator"
            if row["state"] == "waiting_operator"
            else "waiting_participant",
            "state": "queued" if auto else "awaiting_approval",
            "authorization": "host_policy" if auto else None,
            "expires_at": time.time() + self.policy["authorization_seconds"],
            "revision": 0,
            "attempts": 0,
            "result": None,
            "created_at": time.time(),
        }
        self.put(conn, "communication_actions", value)
        if not auto:
            self.request_authority(conn, row, value)
        return value

    def request_authority(self, conn, row, action):
        identity = "request_" + request_key(
            "communication-authority", self.id, action["id"]
        )
        prior = self.db._get(conn, "service_decisions", identity)
        value = {
            "id": identity,
            "kind": "communication_authority",
            "discussion_id": row["id"],
            "action_id": action["id"],
            "work_id": row["work_id"],
            "requirements_revision": 0,
            "basis": self.basis(row),
            "plan_hash": action["plan_hash"],
            "revision": prior["revision"] + 1 if prior else 0,
            "state": "pending",
            "reason": "Authorize " + action["plan"]["kind"],
            "candidate": {
                "body": action["plan"]["body"],
                "subject": row["subject"],
                "intent": action["plan"]["intent"],
                "basis": action["plan"].get("review_basis"),
            },
            "expires_at": time.time() + self.policy["authorization_seconds"],
        }
        return self.ms.put(conn, "decisions", value)

    def decide(self, conn, request, payload, decision_id):
        row = self.get("discussions", request["discussion_id"], conn)
        if (
            request["expires_at"] is not None and request["expires_at"] <= time.time()
        ) or request["basis"] != self.basis(row):
            raise ValueError("Discussion decision has expired or changed.")
        choice = payload["decision"]
        if request["kind"] == "discussion_information":
            if choice != "answer":
                raise ValueError("Discussion requires an operator answer.")
            answer = text(payload.get("text"), "operator answer", 8000)
            row["operator_note"] = answer
            row["analysis_epoch"] += 1
            row["revision"] += 1
            row["state"] = "ready_to_assess"
            row["ready_after"] = 0
            request["state"] = "answered"
        elif request["kind"] == "communication_authority":
            if choice not in {"approve", "deny"} or "text" in payload:
                raise ValueError("Communication requires approve/deny.")
            action = self.get("communication_actions", request["action_id"], conn)
            if (
                action["state"] not in {"awaiting_approval", "queued"}
                or action["plan_hash"] != request["plan_hash"]
            ):
                raise ValueError("Publication plan changed.")
            self.validate_anchors(row, action["plan"], conn)
            action.update(
                state="queued" if choice == "approve" else "denied",
                authorization="operator:" + decision_id
                if choice == "approve"
                else None,
                expires_at=time.time() + self.policy["authorization_seconds"],
                revision=action["revision"] + 1,
            )
            self.put(conn, "communication_actions", action)
            request["state"] = "approved" if choice == "approve" else "denied"
        elif request["kind"] == "discussion_requirements":
            if choice not in {"approve", "deny"} or "text" in payload:
                raise ValueError("Requirements acceptance requires approve/deny.")
            if choice == "approve":
                self.accept_requirements(
                    conn,
                    row,
                    request["candidate"],
                    request.get("category", "feature"),
                    origin="operator:" + decision_id,
                )
                request["state"] = "approved"
            else:
                row["state"] = "waiting_operator"
                request["state"] = "denied"
        else:
            raise ValueError("Unknown discussion decision.")
        request["revision"] += 1
        self.put(conn, "discussions", row)
        self.ms.put(conn, "decisions", request)
        self.ms.changed(
            conn,
            row["work_id"],
            detail={"discussion_id": row["id"], "state": row["state"]},
        )
        return {
            "id": request["id"],
            "state": request["state"],
            "request_id": decision_id,
        }

    def start(self, action):
        with self.db.write() as conn:
            meta = self.runtime.store.guard(conn, self.runtime.header)
            if (
                meta["state"] != "running"
                or meta.get("operations_phase", "active") != "active"
            ):
                raise ValueError("Service publication is paused.")
            current = self.get("communication_actions", action["id"], conn)
            row = self.get("discussions", current["discussion_id"], conn)
            if row["state"] == "paused" or current["basis"] != self.basis(row):
                raise ValueError("Discussion publication became stale.")
            self.validate_anchors(row, current["plan"], conn)
            return self.put(
                conn,
                "communication_actions",
                start_attempt(current, self.runtime.header["generation"]),
            )

    def finish_action(self, accepted, state, result=None, error=None):
        with self.db.write() as conn:
            self.runtime.store.guard(conn, self.runtime.header)
            current = self.get("communication_actions", accepted["id"], conn)
            value = finish_attempt(current, accepted, state, result, error)
            row = self.get("discussions", current["discussion_id"], conn)
            value["applicability"] = (
                "unverified"
                if result and result.get("freshness") == "unverified"
                else "current"
                if current["basis"] == self.basis(row)
                else "published_basis_stale"
            )
            self.put(conn, "communication_actions", value)
            self.resolve_echoes(conn, value)
            row = self.get("discussions", current["discussion_id"], conn)
            if current["basis"] != self.basis(row):
                value["applicability"] = "published_basis_stale"
                self.put(conn, "communication_actions", value)
            if (
                state == "confirmed"
                and value["applicability"] != "published_basis_stale"
            ):
                row["state"] = current.get("resume_state", "waiting_participant")
                self.put(conn, "discussions", row)
            self.ms.changed(
                conn,
                row["work_id"],
                detail={
                    "discussion_id": row["id"],
                    "action_id": value["id"],
                    "state": state,
                },
            )
            return value

    def recover(self):
        with self.db.write() as conn:
            self.runtime.store.guard(conn, self.runtime.header)
            for _, raw in conn.execute(
                "SELECT key,value FROM records WHERE section='service_communication_actions' AND owner=? AND json_extract(value,'$.state')='started'",
                ("service:" + self.id,),
            ).fetchall():
                value = json.loads(raw)
                value.update(
                    state="unknown",
                    error="owner_interrupted",
                    revision=value["revision"] + 1,
                )
                self.put(conn, "communication_actions", value)

    def command(self, method, payload, request_id):
        def apply(conn):
            self.runtime.store.guard(conn, self.runtime.header)
            row = (
                self.get("discussions", payload["id"], conn)
                if method.startswith("discussion_") or method == "communication_plan"
                else self.get("communication_actions", payload["id"], conn)
            )
            if row["revision"] != payload["revision"]:
                raise ValueError("Communication reference revision changed.")
            if method in {"discussion_assess", "discussion_reopen"}:
                request_analysis(row)
            elif method == "discussion_pause":
                row.update(
                    state="paused",
                    analysis_epoch=row["analysis_epoch"] + 1,
                    revision=row["revision"] + 1,
                )
            elif method == "communication_plan":
                discussion = self.get("discussions", payload["id"], conn)
                return self.plan(conn, discussion, payload["plan"])
            elif method == "communication_request_authority":
                if row["state"] not in {
                    "awaiting_approval",
                    "queued",
                    "denied",
                    "failed",
                }:
                    raise ValueError("Only unstarted messages can renew authorization.")
                discussion = self.get("discussions", row["discussion_id"], conn)
                if row["basis"] != self.basis(discussion):
                    raise ValueError("Plan basis changed.")
                row.update(
                    state="awaiting_approval",
                    authorization=None,
                    revision=row["revision"] + 1,
                )
                self.put(conn, "communication_actions", row)
                return self.request_authority(conn, discussion, row)
            else:
                raise ValueError("Unknown communication control.")
            self.put(conn, "discussions", row)
            self.supersede(conn, row)
            return row

        return self.runtime.store.command(
            self.runtime.header, method, payload, request_id, apply
        )
