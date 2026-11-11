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
            "branch": self.get("changesets", work["repair_of"], conn)["branch"]
            if work.get("repair_of")
            else self.spec["review_prefix"] + work["id"][5:] + "-" + identity[-8:],
            "write_paths": work["run_spec"]["write_paths"],
            "goal": work["run_spec"].get("goal"),
            "requirements_id": work.get("requirements_id"),
            "source_workspace": work.get("run_workspace")
            or self.runtime.spec.workspace,
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
            "merge_change_request",
            "post_comment",
        }:
            raise ValueError("Unknown delivery action.")
        if not row.get("frozen"):
            raise ValueError("Candidate is not quality-ready.")
        if row["candidate"].get("artifacts_retired"):
            raise ValueError(
                "Candidate artifacts were retired; prepare new reviewed work before delivery."
            )
        if row["frozen"] != self.freeze(row):
            raise ValueError("Candidate quality evidence changed.")
        if (
            kind == "merge_change_request"
            and self.spec["merge"]["mode"] != "service_approved"
        ):
            raise ValueError(
                "Manual delivery waits for a platform maintainer to merge."
            )
        if kind == "merge_change_request" and row["state"] != "merge_ready":
            raise ValueError("Target delivery is not merge-ready.")
        content = {
            "changeset": row["id"],
            "hash": row["frozen"],
            "commit": row["candidate"]["commit"],
            "branch": row["branch"],
            "target_base": row["candidate"]["target_base"],
        }
        if kind == "merge_change_request":
            from .merge_flow import gate

            content.update(
                subject=row["change_request"],
                method=self.spec["merge"]["method"],
                gate_hash=gate(row["ci"], self.spec)["hash"],
                policy=fingerprint(self.spec["merge"]),
                title="Merge reviewed work " + row["work_id"],
                body="Verified candidate " + row["candidate"]["commit"],
            )
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
            if row.get("review", {}).get("publication"):
                content.update(row["review"]["publication"])
            if row.get("change_request"):
                content.update(
                    existing=row["change_request"],
                    previous_correlation=row["change_request_action"],
                )
            elif row.get("previous"):
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
            and not (
                kind == "ensure_change_request"
                and row.get("review", {}).get("publication")
            )
            else "awaiting_approval",
            "authorization": "host_policy"
            if kind in self.spec["auto_actions"]
            and not (
                kind == "ensure_change_request"
                and row.get("review", {}).get("publication")
            )
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
        if action["state"] not in {"awaiting_approval", "denied", "queued", "failed"}:
            raise ValueError(
                "Only an unstarted delivery action can request authorization."
            )
        if action["state"] == "failed":
            action.update(
                state="awaiting_approval",
                authorization=None,
                revision=action["revision"] + 1,
            )
            self.put(conn, "actions", action)
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
        if action["kind"] == "merge_change_request" and row["state"] != "merge_ready":
            raise ValueError("PR is no longer merge-ready.")
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
            if (
                current["kind"] == "merge_change_request"
                and row["state"] != "merge_ready"
            ):
                raise ValueError("Target delivery is not merge-ready.")
            if current["kind"] == "merge_change_request":
                from .merge_flow import gate

                if current["plan"]["gate_hash"] != gate(row["ci"], self.spec)[
                    "hash"
                ] or current["plan"]["policy"] != fingerprint(self.spec["merge"]):
                    raise ValueError("Merge evidence changed since authorization.")
            pending = conn.execute(
                "SELECT 1 FROM records WHERE section='service_actions' AND owner=? AND key<>? AND json_extract(value,'$.state') IN ('started','unknown') LIMIT 1",
                ("service:" + self.id, current["id"]),
            ).fetchone()
            if pending:
                raise ValueError(
                    "Inspect uncertain external actions before further writes."
                )
            from homebrew_harness.services.action_runtime import start_attempt

            current = start_attempt(current, self.runtime.header["generation"])
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
            from homebrew_harness.services.action_runtime import finish_attempt

            if (
                current["state"] == "confirmed"
                and current["kind"] == "merge_change_request"
                and current["attempts"] == action["attempts"]
            ):
                return current
            current = finish_attempt(current, action, state, result, error)
            self.put(conn, "actions", current)
            row = self.get("changesets", action["changeset_id"], conn)
            if (
                action["kind"] == "merge_change_request"
                and result
                and result.get("state") == "pending"
            ):
                row.update(merge=result, state="merge_pending", next_merge=0)
                self.put(conn, "changesets", row)
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
                elif action["kind"] == "merge_change_request":
                    row.update(merge=result, state="merged_verifying", next_merge=0)
                self.put(conn, "changesets", row)
                if self.ms.communication is not None:
                    self.ms.communication.link_delivery(conn, current)
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

    def invalidate_merges(self, identity):
        with self.db.write() as conn:
            self.runtime.store.guard(conn, self.runtime.header)
            for _, raw in conn.execute(
                "SELECT key,value FROM records WHERE section='service_actions' AND owner=? "
                "AND json_extract(value,'$.changeset_id')=? AND json_extract(value,'$.kind')='merge_change_request'",
                ("service:" + self.id, identity),
            ).fetchall():
                import json

                action = json.loads(raw)
                if action["state"] in {
                    "queued",
                    "awaiting_approval",
                    "denied",
                    "failed",
                }:
                    action.update(state="superseded", revision=action["revision"] + 1)
                    self.put(conn, "actions", action)
                    rid = "request_" + request_key(
                        "delivery-request", self.id, action["id"]
                    )
                    request = self.db._get(conn, "service_decisions", rid)
                    if request and request["state"] == "pending":
                        request.update(
                            state="superseded", revision=request["revision"] + 1
                        )
                        self.put(conn, "decisions", request)

    def record_merge(self, identity, observed):
        with self.db.write() as conn:
            self.runtime.store.guard(conn, self.runtime.header)
            row = self.get("changesets", identity, conn)
            pending_quality = row["state"] in {
                "preparing",
                "validating",
                "reviewing",
            } and (
                not row.get("quality")
                or self.spec["require_review"]
                and row.get("review") is None
            )
            if row.get("merge", {}).get("result_commit") == observed["result_commit"]:
                if (
                    row["state"] in {"review_branch_ready", "quality_failed"}
                    and not pending_quality
                ):
                    row.update(state="merged_verifying", revision=row["revision"] + 1)
                    self.put(conn, "changesets", row)
                return row
            if row["candidate"]["commit"] != observed["head"]:
                raise ValueError("Merge observation belongs to another candidate.")
            if observed["subject"] != row["change_request"]:
                raise ValueError("Merge observation belongs to another PR.")
            import json
            from homebrew_harness.services.action_runtime import finish_attempt

            for _, raw in conn.execute(
                "SELECT key,value FROM records WHERE section='service_actions' AND owner=? "
                "AND json_extract(value,'$.changeset_id')=? AND json_extract(value,'$.kind')='merge_change_request' "
                "AND json_extract(value,'$.state')='unknown'",
                ("service:" + self.id, identity),
            ).fetchall():
                action = json.loads(raw)
                if action["plan"]["commit"] == observed["head"]:
                    self.put(
                        conn,
                        "actions",
                        finish_attempt(action, action, "confirmed", observed),
                    )
            row.update(
                merge=observed,
                next_merge=time.time() + self.spec["merge"]["poll_seconds"],
            )
            row.setdefault("read_errors", {}).pop("pr", None)
            if not pending_quality:
                row.update(state="merged_verifying", revision=row["revision"] + 1)
            self.put(conn, "changesets", row)
            self.ms.changed(conn, row["work_id"])
            return row

    def observation(self, identity, observed, next_poll):
        with self.db.write() as conn:
            self.runtime.store.guard(conn, self.runtime.header)
            row = self.get("changesets", identity, conn)
            if row.get("observed") != observed:
                row["observation_revision"] = row.get("observation_revision", 0) + 1
            row.update(observed=observed, next_merge=next_poll)
            row.setdefault("read_errors", {}).pop("pr", None)
            return self.put(conn, "changesets", row)

    def read_failure(self, identity, stage, error):
        from homebrew_harness.security.data_boundary import redact

        with self.db.write() as conn:
            self.runtime.store.guard(conn, self.runtime.header)
            row = self.get("changesets", identity, conn)
            errors = row.setdefault("read_errors", {})
            attempts = errors.get(stage, {}).get("attempts", 0) + 1
            due = time.time() + min(
                self.spec["merge"]["poll_seconds"], 2 ** min(attempts, 8)
            )
            errors[stage] = {
                "type": type(error).__name__,
                "detail": redact(str(error)),
                "attempts": attempts,
                "next_attempt": due,
            }
            row.update(next_merge=due, next_ci=max(row.get("next_ci", 0), due))
            return self.put(conn, "changesets", row)

    def clear_read_failure(self, identity, stage):
        with self.db.write() as conn:
            self.runtime.store.guard(conn, self.runtime.header)
            row = self.get("changesets", identity, conn)
            row.setdefault("read_errors", {}).pop(stage, None)
            return self.put(conn, "changesets", row)

    def complete_integration(self, identity, actual, checked):
        if checked.get("ok") is not True or actual.get("proof") not in {
            "merge_tree",
            "squash_tree",
            "rebase_replay",
            "fixed_tree",
        }:
            raise ValueError(
                "Integration completion needs a verified content and quality receipt."
            )
        with self.db.write() as conn:
            meta = self.runtime.store.guard(conn, self.runtime.header)
            row = self.get("changesets", identity, conn)
            if row.get("state") == "delivered":
                return row
            if (
                row["merge"]["result_commit"] != actual["commit"]
                or row["candidate"]["commit"] != actual["head"]
            ):
                raise ValueError("Verified merge result changed before completion.")
            from pathlib import Path

            expected = (
                self.runtime.store.root
                / ".harness/delivery/integrations"
                / fingerprint([row["id"], actual["head"], actual["commit"]])
                / "workspace"
            )
            if Path(actual["workspace"]) != expected:
                raise ValueError(
                    "Verified integration workspace is outside its fixed owner."
                )
            row.update(
                integration=actual,
                integration_quality=checked,
                revision=row["revision"] + 1,
            )
            work = self.ms.get("work", row["work_id"], conn)
            current_work = (
                work["requirements_revision"] == row["requirements_revision"]
                and work.get("changeset_id") == row["id"]
                and work.get("run_id") == row["run_id"]
                and work["state"] == "review"
                and not work.get("cancel_requested")
                and not work.get("pause_requested")
            )
            if not current_work:
                row["requirements_superseded"] = True
            if meta["state"] != "running":
                self.put(conn, "changesets", row)
                return row
            row.update(state="delivered", delivered_at=time.time())
            if current_work:
                work.update(
                    state="delivered",
                    revision=work["revision"] + 1,
                    workspace_sync={"state": "pending", "changeset_id": row["id"]},
                )
                self.db._put(conn, "service_work", work["id"], work)
            else:
                row["requirements_superseded"] = True
            self.put(conn, "changesets", row)
            self.ms.changed(conn, row["work_id"])
            return row
