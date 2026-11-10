"""Versioned ChangeSets, immutable action plans and per-item host decisions."""

import time
from homebrew_harness.services.protocol import request_key, fingerprint
from homebrew_harness.services.maintenance_spec import text
from .git import StaleDelivery


class DeliveryStore:
    def __init__(self, runtime, maintenance, spec):
        self.runtime, self.ms, self.spec = runtime, maintenance, spec
        self.db, self.id = maintenance.db, maintenance.id
        self.policy = fingerprint(spec)

    def get(self, kind, identity, conn=None):
        return self.ms.get(kind, identity, conn)

    def put(self, conn, kind, value):
        return self.ms.put(conn, kind, value)

    def update(self, kind, identity, **changes):
        with self.db.write() as conn:
            self.runtime.store.guard(conn, self.runtime.header)
            value = self.get(kind, identity, conn)
            value.update(changes, revision=value["revision"] + 1)
            return self.put(conn, kind, value)

    def create(self, conn, work):
        identity = "changeset_" + request_key(
            "changeset",
            self.id,
            work["id"]
            + ":"
            + str(work["requirements_revision"])
            + ":"
            + work["run_id"],
        )
        old = self.db._get(conn, "service_changesets", identity)
        if old:
            return old
        value = {
            "id": identity,
            "work_id": work["id"],
            "requirements_revision": work["requirements_revision"],
            "run_id": work["run_id"],
            "previous": work.get("repair_of"),
            "state": "queued",
            "revision": 0,
            "policy": self.policy,
            "created_at": time.time(),
            "attempts": 0,
            "candidate": None,
            "quality": None,
            "review": None,
            "change_request": None,
            "ci": None,
            "error": None,
            "branch": self.spec["review_prefix"] + work["id"][5:],
        }
        self.put(conn, "changesets", value)
        work["changeset_id"] = identity
        work["revision"] += 1
        self.db._put(conn, "service_work", work["id"], work)
        self.ms.changed(conn, work["id"])
        return value

    def current(self, row, conn=None):
        work = (
            self.db._get(conn, "service_work", row["work_id"])
            if conn is not None
            else self.runtime.store.work(row["work_id"], self.id)
        )
        if (
            row["requirements_revision"] != work["requirements_revision"]
            or row["policy"] != self.policy
            or work.get("changeset_id") != row["id"]
            or work.get("run_id") != row["run_id"]
            or work.get("cancel_requested")
            or (work.get("authority") or {}).get("revoked")
        ):
            raise StaleDelivery("ChangeSet is stale; prepare a current candidate.")
        return work

    def freeze(self, row):
        return fingerprint(
            {
                "work_id": row["work_id"],
                "requirements_revision": row["requirements_revision"],
                "policy": row["policy"],
                "candidate": {
                    k: row["candidate"][k]
                    for k in ("commit", "tree", "target_base", "content_hash", "delta")
                },
                "quality": row["quality"],
                "goal_evidence": row.get("goal_evidence"),
                "review": row["review"],
            }
        )

    def action(self, conn, row, kind, *, body=None, subject=None):
        self.current(row)
        if kind not in {
            "publish_branch",
            "ensure_change_request",
            "publish_target",
            "post_comment",
        }:
            raise ValueError("Unknown delivery action.")
        if not row.get("frozen"):
            raise ValueError("Candidate is not quality-ready.")
        if row["frozen"] != self.freeze(row):
            raise ValueError("Candidate quality evidence changed.")
        if kind == "publish_target" and row["state"] != "merge_ready":
            raise ValueError("Target delivery is not merge-ready.")
        content = {
            "changeset": row["id"],
            "hash": row["frozen"],
            "commit": row["candidate"]["commit"],
            "branch": row["branch"],
            "target_base": row["candidate"]["target_base"],
        }
        if kind == "publish_target":
            content["ci_hash"] = fingerprint(row.get("ci"))
        if kind == "post_comment":
            text(body, maximum=16000)
            from homebrew_harness.platforms.contracts import ObjectRef

            ref = ObjectRef(**subject)
            if ref.scope() != (
                self.spec["provider"],
                self.spec["instance"],
                self.spec["project"],
            ):
                raise ValueError("Comment crosses platform scope.")
            content.update(body=body, subject=subject)
        if kind == "ensure_change_request":
            work = self.current(row)
            content.update(
                title="Maintain " + work["id"],
                body="Work "
                + work["id"]
                + "\n\nCandidate "
                + row["candidate"]["commit"]
                + "\n\nLocal verification completed. Publication is tracked separately.",
            )
            if row.get("previous"):
                prior = self.get("changesets", row["previous"], conn)
                if prior.get("change_request"):
                    content.update(
                        existing=prior["change_request"],
                        previous_correlation=prior["change_request_action"],
                    )
        identity = "delivery_" + request_key(
            "action", self.id, fingerprint([kind, content])
        )
        old = self.db._get(conn, "service_actions", identity)
        if old:
            return old
        value = {
            "id": identity,
            "kind": kind,
            "work_id": row["work_id"],
            "changeset_id": row["id"],
            "plan": content,
            "plan_hash": fingerprint(content),
            "state": "queued"
            if kind in self.spec["auto_actions"]
            else "awaiting_approval",
            "authorization": "host_policy"
            if kind in self.spec["auto_actions"]
            else None,
            "expires_at": time.time() + self.spec["authorization_seconds"],
            "revision": 0,
            "attempts": 0,
            "result": None,
            "created_at": time.time(),
        }
        self.put(conn, "actions", value)
        if value["state"] == "awaiting_approval":
            self.request(conn, row, value)
        return value

    def request(self, conn, row, action, *, renew=False):
        if action["state"] not in {"awaiting_approval", "denied", "queued"}:
            raise ValueError(
                "Only an unstarted delivery action can request authorization."
            )
        identity = "request_" + request_key("delivery-request", self.id, action["id"])
        prior = self.db._get(conn, "service_decisions", identity)
        if prior and not renew:
            return prior
        value = {
            "id": identity,
            "kind": "delivery_authority",
            "work_id": row["work_id"],
            "requirements_revision": row["requirements_revision"],
            "changeset_id": row["id"],
            "action_id": action["id"],
            "plan_hash": action["plan_hash"],
            "state": "pending",
            "revision": prior["revision"] + 1 if prior else 0,
            "expires_at": time.time() + self.spec["authorization_seconds"],
            "reason": "Authorize "
            + action["kind"]
            + " for frozen candidate "
            + row["candidate"]["commit"],
            "candidate": {
                "scope": "delivery",
                "plan": str(action["plan"]),
                "reason": "Concrete candidate and target authorization.",
            },
            "created_at": time.time(),
        }
        return self.put(conn, "decisions", value)

    def decide(self, conn, request, payload, decision_id):
        if request["kind"] == "ci_repair":
            row = self.get("changesets", request["changeset_id"], conn)
            self.current(row)
            if (
                request["state"] != "pending"
                or request["revision"] != payload["revision"]
                or request["expires_at"] <= time.time()
            ):
                raise ValueError("CI decision is stale or expired.")
            if payload["decision"] == "approve":
                row.update(state="repair_approved")
            elif payload["decision"] == "deny":
                row.update(state="blocked", error="repair_denied")
            else:
                raise ValueError("CI repair expects approve/deny.")
            request.update(
                state="approved" if payload["decision"] == "approve" else "denied",
                revision=request["revision"] + 1,
            )
            self.put(conn, "changesets", row)
            self.put(conn, "decisions", request)
            return {
                "id": request["id"],
                "state": request["state"],
                "request_id": decision_id,
            }
        if request["kind"] != "delivery_authority":
            raise ValueError("Unknown delivery decision type.")
        action = self.get("actions", request["action_id"], conn)
        row = self.get("changesets", action["changeset_id"], conn)
        self.current(row)
        if (
            type(payload["revision"]) is not int
            or request["revision"] != payload["revision"]
            or request["state"] != "pending"
            or request["expires_at"] <= time.time()
            or action["plan_hash"] != request["plan_hash"]
            or row["frozen"] != action["plan"]["hash"]
            or action["state"] not in {"queued", "awaiting_approval"}
        ):
            raise ValueError("Delivery request is stale or expired.")
        if payload["decision"] not in {"approve", "deny"} or "text" in payload:
            raise ValueError("Delivery expects an explicit approve/deny.")
        approve = payload["decision"] == "approve"
        action.update(
            state="queued" if approve else "denied",
            authorization="operator:" + decision_id if approve else None,
            expires_at=time.time() + self.spec["authorization_seconds"],
            revision=action["revision"] + 1,
        )
        request.update(
            state="approved" if approve else "denied", revision=request["revision"] + 1
        )
        self.put(conn, "actions", action)
        self.put(conn, "decisions", request)
        self.ms.changed(conn, row["work_id"])
        return {
            "id": request["id"],
            "state": request["state"],
            "request_id": decision_id,
        }

    def start_action(self, action):
        with self.db.write() as conn:
            meta = self.runtime.store.guard(conn, self.runtime.header)
            if meta["state"] != "running":
                raise ValueError("Service is not executing delivery.")
            current = self.get("actions", action["id"], conn)
            row = self.get("changesets", current["changeset_id"], conn)
            self.current(row)
            if (
                current["state"] != "queued"
                or current["authorization"] is None
                or current["expires_at"] <= time.time()
            ):
                raise ValueError("Action is not authorized or has expired.")
            if row["frozen"] != current["plan"]["hash"]:
                raise ValueError("Action candidate changed.")
            if row["frozen"] != self.freeze(row):
                raise ValueError("Candidate quality evidence changed.")
            if current["kind"] == "publish_target" and row["state"] != "merge_ready":
                raise ValueError("Target delivery is not merge-ready.")
            if current["kind"] == "publish_target" and current["plan"].get(
                "ci_hash"
            ) != fingerprint(row.get("ci")):
                raise ValueError("CI evidence changed since target authorization.")
            pending = conn.execute(
                "SELECT 1 FROM records WHERE section='service_actions' AND owner=? AND key<>? AND json_extract(value,'$.state') IN ('started','unknown') LIMIT 1",
                ("service:" + self.id, current["id"]),
            ).fetchone()
            if pending:
                raise ValueError(
                    "Inspect uncertain external actions before further writes."
                )
            current.update(
                state="started",
                attempts=current["attempts"] + 1,
                generation=self.runtime.header["generation"],
                revision=current["revision"] + 1,
            )
            return self.put(conn, "actions", current)

    def expire_action(self, action):
        with self.db.write() as conn:
            self.runtime.store.guard(conn, self.runtime.header)
            current = self.get("actions", action["id"], conn)
            if current["state"] != "queued" or current["expires_at"] > time.time():
                return
            row = self.get("changesets", action["changeset_id"], conn)
            current.update(
                state="awaiting_approval",
                authorization=None,
                revision=current["revision"] + 1,
            )
            self.put(conn, "actions", current)
            self.request(conn, row, current, renew=True)

    def settle_action(self, action, state, result=None, error=None):
        with self.db.write() as conn:
            self.runtime.store.guard(conn, self.runtime.header)
            current = self.get("actions", action["id"], conn)
            if current["state"] not in {"started", "unknown"}:
                raise ValueError("Delivery action phase changed.")
            current.update(
                state=state,
                result=result,
                error=error,
                revision=current["revision"] + 1,
            )
            self.put(conn, "actions", current)
            row = self.get("changesets", action["changeset_id"], conn)
            if state == "confirmed":
                if action["kind"] == "publish_branch":
                    row.update(review_commit=action["plan"]["commit"])
                elif action["kind"] == "ensure_change_request":
                    row.update(
                        change_request=result["subject"],
                        change_request_action=action["id"],
                        state="ci_waiting",
                        ci_started=time.time(),
                        next_ci=0,
                    )
                elif action["kind"] == "publish_target":
                    row.update(state="delivered", delivered_at=time.time())
                    work = self.runtime.store.work(row["work_id"], self.id)
                    if (
                        work["requirements_revision"] == row["requirements_revision"]
                        and work.get("changeset_id") == row["id"]
                        and work.get("run_id") == row["run_id"]
                        and work["state"] == "review"
                    ):
                        work.update(state="delivered", revision=work["revision"] + 1)
                        self.db._put(conn, "service_work", work["id"], work)
                    else:
                        row["requirements_superseded"] = True
                self.put(conn, "changesets", row)
            self.ms.changed(conn, row["work_id"])
            return current

    def finish_model(self, conn, job, result):
        row = self.get("changesets", job["target"], conn)
        self.current(row)
        if row["revision"] != job["revision"]:
            return "stale"
        if job["kind"] == "review":
            row["review"] = result
            if any(f["severity"] == "blocking" for f in result["findings"]):
                row.update(state="quality_failed", error="review_findings")
            else:
                row.update(state="review_branch_ready")
                row["frozen"] = self.freeze(row)
        elif job["kind"] == "ci_triage":
            row["ci_diagnosis"] = result
            if result["classification"] == "code":
                if self.spec["auto_ci_fix"]:
                    row["state"] = "repair_approved"
                else:
                    row["state"] = "repair_waiting"
                    identity = "request_" + request_key(
                        "ci-repair", self.id, row["id"] + ":" + fingerprint(result)
                    )
                    self.put(
                        conn,
                        "decisions",
                        {
                            "id": identity,
                            "kind": "ci_repair",
                            "work_id": row["work_id"],
                            "requirements_revision": row["requirements_revision"],
                            "changeset_id": row["id"],
                            "revision": 0,
                            "state": "pending",
                            "expires_at": time.time()
                            + self.spec["authorization_seconds"],
                            "candidate": {
                                "scope": "ci-repair",
                                "plan": result["plan"],
                                "reason": result["reason"],
                            },
                            "reason": result["reason"],
                        },
                    )
            else:
                row.update(state="blocked", error="ci_" + result["classification"])
        row["revision"] += 1
        self.put(conn, "changesets", row)
        self.ms.changed(conn, row["work_id"])
        return "completed"
