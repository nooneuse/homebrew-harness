"""Versioned ChangeSets, immutable action plans and per-item host decisions."""
from homebrew_harness.runtime.limits import expiry, expired

from homebrew_harness.runtime.errors import boundary_error
import json
import time
from homebrew_harness.services.protocol import request_key, fingerprint
from homebrew_harness.services.maintenance_spec import text
from .git import StaleDelivery


PR_MUTATIONS = {"publish_branch", "ensure_change_request", "merge_change_request"}


def observed_merge(row):
    """Return a recorded platform result, not a closed or pending PR."""
    subject = row.get("change_request")
    for key in ("merge", "foreign_merge"):
        receipt = row.get(key) or {}
        if (subject and receipt.get("subject") == subject
                and receipt.get("state") == "merged" and receipt.get("result_commit")):
            return receipt
    return None


def scoped_repair(row):
    diagnosis = row.get("ci_diagnosis") or {}
    if diagnosis.get("next_action") is not None:
        return diagnosis["next_action"] == "repair"
    classification = diagnosis.get("classification")
    return classification == "code" or (
        (row.get("ci") or {}).get("kind") == "candidate_merge_conflict" and classification == "baseline")


class DeliveryStore:
    def __init__(self, runtime, maintenance, spec):
        self.runtime, self.ms, self.spec = runtime, maintenance, spec
        self.db, self.id = maintenance.db, maintenance.id
        self.policy = fingerprint({k: v for k, v in spec.items()
                                   if k not in {"ci_fix_limit", "ci_fix_warning", "base_update"}})
        self.legacy_policy = fingerprint({k: v for k, v in spec.items() if k not in {"ci_fix_warning", "base_update"}})

    def policy_matches(self, row):
        return row["policy"] in {self.policy, self.legacy_policy} or row.get("policy_scope") == self.policy

    def limit_reached(self, used):
        limit = self.spec["ci_fix_limit"]
        return limit is not None and used >= limit

    def get(self, kind, identity, conn=None):
        return self.ms.get(kind, identity, conn)

    def put(self, conn, kind, value):
        return self.ms.put(conn, kind, value)

    def update(self, kind, identity, **changes):
        with self.db.write() as conn:
            self.runtime.store.guard(conn, self.runtime.header)
            value = self.get(kind, identity, conn)
            value.update(changes, revision=value["revision"] + 1)
            result = self.put(conn, kind, value)
            if kind == "changesets" and changes.get("foreign_merge") and observed_merge(value):
                self.retire_publication(conn, work_id=value["work_id"])
            return result

    def commit_feedback(self, row, changes, *, diagnose=False, merge=False):
        """Commit one queried version, never overwrite newer preparation or merge facts."""
        with self.db.write() as conn:
            self.runtime.store.guard(conn, self.runtime.header)
            current = self.get("changesets", row["id"], conn)
            if (current["revision"] != row["revision"]
                    or current["candidate"]["commit"] != row["candidate"]["commit"]
                    or current.get("frozen") != row.get("frozen")
                    or current.get("merge")
                    or getattr(self.ms.delivery, "preparing", False)):
                return None
            try:
                self.current(current, conn)
            except StaleDelivery:
                return None
            current.update(changes, revision=current["revision"] + 1)
            self.put(conn, "changesets", current)
            if diagnose:
                self.ms.job(conn, "ci_triage", current["id"], current["revision"], {},
                            current["id"] + ":ci:" + fingerprint(current["ci"]))
            if merge:
                self.action(conn, current, "merge_change_request")
            return current

    def repair_decision(self, row, conn=None):
        identity = "request_" + request_key("ci-repair", self.id,
            row["id"] + ":" + fingerprint(row.get("ci_diagnosis") or {}))
        return (self.db._get(conn, "service_decisions", identity) if conn is not None
                else self.db.get("service_decisions", identity))

    def repair_allowed(self, row, work, conn=None):
        used = work.get("ci_fix_attempts", 0)
        decision = self.repair_decision(row, conn)
        accepted = (decision and decision["state"] == "approved"
            and decision["requirements_revision"] == row["requirements_revision"]
            and decision["candidate"]["plan"] == row["ci_diagnosis"]["plan"]
            and decision.get("authorized_attempt", used + 1) == used + 1)
        if accepted:
            return not self.limit_reached(used) or decision.get("extra_attempt") is True
        return self.spec["auto_ci_fix"] and not self.limit_reached(used)

    def repair_request(self, conn, row, *, renew=False):
        result = row["ci_diagnosis"]
        identity = "request_" + request_key("ci-repair", self.id, row["id"] + ":" + fingerprint(result))
        old = self.db._get(conn, "service_decisions", identity)
        if old:
            if renew and old["state"] == "pending" and expired(old["expires_at"], time.time()):
                old.update(revision=old["revision"] + 1,
                           expires_at=expiry(self.spec["authorization_seconds"], time.time()))
                return self.put(conn, "decisions", old)
            return old
        return self.put(conn, "decisions", {
            "id": identity, "kind": "ci_repair", "work_id": row["work_id"],
            "requirements_revision": row["requirements_revision"], "changeset_id": row["id"],
            "revision": 0, "state": "pending", "created_at": time.time(),
            "expires_at": expiry(self.spec["authorization_seconds"], time.time()),
            "candidate": {"scope": "ci-repair", "plan": result["plan"], "reason": result["reason"]},
            "reason": result["reason"],
        })

    def renew_repair_request(self, row):
        with self.db.write() as conn:
            self.runtime.store.guard(conn, self.runtime.header)
            current = self.get("changesets", row["id"], conn)
            self.current(current, conn)
            if current["state"] != "repair_waiting" or not scoped_repair(current):
                raise ValueError("Renew only a current pending scoped repair decision.")
            request = self.repair_request(conn, current, renew=True)
            if request["state"] != "pending":
                raise ValueError("The repair decision is already settled.")
            return current

    def create(self, conn, work):
        identity = "changeset_" + request_key(
            "changeset",
            self.id,
            work["id"]
            + ":"
            + str(work["requirements_revision"])
            + ":"
            + (work.get("progress_ref") or work["run_id"]),
        )
        old = self.db._get(conn, "service_changesets", identity)
        if old:
            return old
        value = {
            "id": identity,
            "work_id": work["id"],
            "requirements_revision": work["requirements_revision"],
            "run_id": work["run_id"],
            "handoff_ref": work.get("progress_ref"),
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
            "branch": (
                self.get("changesets", work["repair_of"], conn)["branch"]
                if work.get("repair_of")
                else self.spec["review_prefix"] + work["id"][5:] + "-" + identity[-8:]
            ),
            "write_paths": work["run_spec"]["write_paths"],
            "goal": work["run_spec"].get("goal"),
            "requirements_id": work.get("requirements_id"),
            "source_workspace": work.get("run_workspace")
            or self.runtime.spec.workspace,
        }
        if value["previous"]:
            previous = self.get("changesets", value["previous"], conn)
            value["publish_base"] = previous.get("review_commit") or previous.get("publish_base")
            if previous.get("integration_schedule"):
                value["integration_schedule"] = previous["integration_schedule"]
        self.put(conn, "changesets", value)
        work["changeset_id"] = identity
        work["revision"] += 1
        self.db._put(conn, "service_work", work["id"], work)
        self.ms.changed(conn, work["id"])
        return value

    def test_failure(self, row, checked):
        """Accept real failed receipts and queue the existing model diagnosis job."""
        with self.db.write() as conn:
            self.runtime.store.guard(conn, self.runtime.header)
            current = self.get("changesets", row["id"], conn)
            self.current(current, conn)
            if current["revision"] != row["revision"]:
                raise StaleDelivery("Candidate changed during quality verification.")
            current.update(
                state="ci_diagnosing", quality=checked["quality"],
                frozen=None, review=None,
                test_failure=checked["test_failure"],
                goal_evidence=checked["goal_evidence"],
                error="candidate_test_failed",
                error_detail="Registered candidate test failed; inspect its recorded receipt and output.",
                validation_history=[*current.get("validation_history", []),
                    {"observed_at": time.time(), "quality": checked["quality"],
                     "candidate_commit": current["candidate"]["commit"],
                     "test_failure": checked["test_failure"]}],
                revision=current["revision"] + 1,
            )
            current.pop("ci_diagnosis", None)
            self.put(conn, "changesets", current)
            self.ms.job(conn, "ci_triage", current["id"], current["revision"], {},
                        current["id"] + ":quality:" + checked["test_failure"]["run_id"])
            self.ms.changed(conn, current["work_id"])
            return current

    def retry_validation(self, row):
        """Explicit host recheck of unchanged, unpublished candidate content."""
        self.runtime.maintenance.delivery.git.verify(row["candidate"])
        with self.db.write() as conn:
            self.runtime.store.guard(conn, self.runtime.header)
            current = self.get("changesets", row["id"], conn)
            self.current(current, conn)
            if (current["revision"] != row["revision"]
                    or current["state"] not in {"failed", "quality_failed", "blocked"}
                    or current.get("change_request")):
                raise ValueError("Only a settled, unpublished candidate can be rechecked.")
            current.update(
                state="validating", error=None, error_detail=None,
                frozen=None, review=None, quality=None,
                validation_history=[*current.get("validation_history", []),
                    {"observed_at": time.time(), "state": row["state"],
                     "error": row.get("error"), "error_detail": row.get("error_detail"),
                     "quality": row.get("quality"), "requested_recheck": True}],
                revision=current["revision"] + 1, next_validation=0,
            )
            self.put(conn, "changesets", current)
            self.ms.changed(conn, current["work_id"])
            return current

    def retry_unstarted_repair(self, row):
        """Resume an accepted CI repair without repeating candidate validation."""
        self.runtime.maintenance.delivery.git.verify(row["candidate"])
        with self.db.write() as conn:
            self.runtime.store.guard(conn, self.runtime.header)
            current = self.get("changesets", row["id"], conn)
            work = self.current(current, conn)
            diagnosis = current.get("ci_diagnosis") or {}
            if (current["revision"] != row["revision"] or current["state"] != "failed"
                    or not scoped_repair(current)
                    or work.get("run_id") != current["run_id"]
                    or work.get("repair_of") == current["id"]):
                raise ValueError("Retry only a current CI repair that has not created a Run.")
            if not self.repair_allowed(current, work, conn):
                raise ValueError("CI repair still requires its current accepted decision.")
            current.update(
                state="repair_approved", error=None, error_detail=None, error_info=None,
                revision=current["revision"] + 1,
                repair_retry_history=[*current.get("repair_retry_history", []),
                    {"observed_at": time.time(), "error": row.get("error"),
                     "error_detail": row.get("error_detail"), "error_info": row.get("error_info")}],
            )
            self.put(conn, "changesets", current)
            self.ms.changed(conn, current["work_id"])
            return current

    def current(self, row, conn=None):
        work = (
            self.db._get(conn, "service_work", row["work_id"])
            if conn is not None
            else self.runtime.store.work(row["work_id"], self.id)
        )
        if (
            row["requirements_revision"] != work["requirements_revision"]
            or not self.policy_matches(row)
            or work.get("changeset_id") != row["id"]
            or (
                row.get("handoff_ref") is not None
                and row["handoff_ref"] != work.get("progress_ref")
            )
            or work.get("cancel_requested")
            or (work.get("authority") or {}).get("revoked")
        ):
            raise StaleDelivery("ChangeSet is stale; prepare a current candidate.")
        return work

    def action_basis(self,row,plan):
        if plan.get('draft'):
            return fingerprint({'work_id':row['work_id'],'requirements_revision':row['requirements_revision'],
                'policy':row['policy'],'candidate':{k:row['candidate'][k] for k in ('commit','tree','content_hash','delta')},'draft':True})
        return row.get('frozen')

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

    def merged_pr(self, conn, row):
        """Check the explicitly bound PR, including a same-branch predecessor."""
        ref = row.get("change_request")
        work_id, branch = row["work_id"], row.get("branch")
        seen = set()
        while row and row["id"] not in seen:
            seen.add(row["id"])
            subject = row.get("change_request")
            if (row["work_id"] != work_id or row.get("branch") != branch
                    or (subject and ref and subject != ref)):
                break
            ref = ref or subject
            if merged := observed_merge(row):
                return merged
            previous = row.get("previous")
            row = self.db._get(conn, "service_changesets", previous) if branch and previous else None
        if ref and branch:
            # A newer candidate may have recorded the merge while an older
            # action for this same PR still exists in the queue.
            for (raw,) in conn.execute(
                "SELECT value FROM records WHERE section='service_changesets' AND owner=? "
                "AND json_extract(value,'$.work_id')=? AND json_extract(value,'$.branch')=? "
                "AND json_extract(value,'$.change_request.id')=? "
                "AND (json_extract(value,'$.merge.state')='merged' OR json_extract(value,'$.foreign_merge.state')='merged')",
                ("service:" + self.id, work_id, branch, ref["id"]),
            ):
                receipt = observed_merge(json.loads(raw))
                if receipt and receipt["subject"] == ref:
                    return receipt
        return None

    def require_unmerged(self, conn, row, kind):
        if kind in PR_MUTATIONS and self.merged_pr(conn, row):
            raise boundary_error(
                ValueError, "pr_already_merged",
                "This PR is already merged; the delivery action is no longer applicable.",
            )

    def retire_action(self, conn, action, merge):
        evidence = {"subject": merge["subject"], "result_commit": merge["result_commit"]}
        if action["state"] in {"queued", "awaiting_approval"}:
            action.update(state="superseded", revision=action["revision"] + 1,
                          superseded_reason="pr_already_merged", superseded_by=evidence)
            self.put(conn, "actions", action)
        elif action["state"] not in {"failed", "denied"}:
            return  # A started or uncertain attempt still needs reconciliation.
        identity = "request_" + request_key("delivery-request", self.id, action["id"])
        request = self.db._get(conn, "service_decisions", identity)
        if request and request["state"] == "pending":
            request.update(state="superseded", revision=request["revision"] + 1,
                           unavailable_reason="pr_already_merged", superseded_by=evidence)
            self.put(conn, "decisions", request)

    def retire_publication(self, conn, *, work_id=None):
        """Also repair saved unstarted plans from before terminal settlement existed."""
        actions = conn.execute(
            "SELECT value FROM records WHERE section='service_actions' AND owner=? "
            "AND json_extract(value,'$.kind') IN ('publish_branch','ensure_change_request','merge_change_request') "
            "AND json_extract(value,'$.state') IN ('queued','awaiting_approval','failed','denied') "
            + ("AND json_extract(value,'$.work_id')=?" if work_id else ""),
            ("service:" + self.id, *([work_id] if work_id else [])),
        ).fetchall()
        for (raw,) in actions:
            action = json.loads(raw)
            row = self.db._get(conn, "service_changesets", action["changeset_id"])
            if row and (merged := self.merged_pr(conn, row)):
                self.retire_action(conn, action, merged)

    def recover_terminal(self):
        with self.db.write() as conn:
            self.runtime.store.guard(conn, self.runtime.header)
            self.retire_publication(conn)

    def action(self, conn, row, kind, *, body=None, subject=None, draft=False, publication=None):
        saved = self.db._get(conn, "service_changesets", row["id"])
        self.require_unmerged(conn, saved or row, kind)
        self.current(row, conn)
        if kind == "merge_change_request" and self.ms.communication is not None:
            from homebrew_harness.services.communication.followup import pending

            if pending(self.ms.communication, row, conn):
                row.update(state="review_waiting", error="pr_feedback_pending", next_ci=0,
                           revision=row["revision"] + 1)
                self.put(conn, "changesets", row)
                return None
        if kind not in {
            "publish_branch",
            "ensure_change_request",
            "merge_change_request",
            "post_comment",
        }:
            raise boundary_error(
                ValueError, "delivery_contract", "Unknown delivery action."
            )
        if type(draft) is not bool or draft and kind not in {'publish_branch','ensure_change_request'}:
            raise ValueError('Draft publication applies only to a review branch or change request.')
        if not row.get('candidate'):raise ValueError('Capture a source candidate before draft publication.')
        if draft and kind=='ensure_change_request' and not publication:
            raise ValueError('Draft PR publication requires a truthful title/body.')
        if not draft and not row.get("frozen"):
            raise boundary_error(
                ValueError, "delivery_contract", "Candidate is not quality-ready."
            )
        if row["candidate"].get("artifacts_retired"):
            raise boundary_error(
                ValueError,
                "delivery_contract",
                "Candidate artifacts were retired; prepare new reviewed work before delivery.",
            )
        if not draft and row["frozen"] != self.freeze(row):
            raise boundary_error(
                ValueError, "delivery_contract", "Candidate quality evidence changed."
            )
        if (
            kind == "merge_change_request"
            and self.spec["merge"]["mode"] != "service_approved"
        ):
            raise boundary_error(
                ValueError,
                "delivery_contract",
                "Manual delivery waits for a platform maintainer to merge.",
            )
        if kind == "merge_change_request" and row["state"] != "merge_ready":
            raise boundary_error(
                ValueError, "delivery_contract", "Target delivery is not merge-ready."
            )
        content = {
            "changeset": row["id"],
            "hash": self.action_basis(row,{'draft':draft}),
            "commit": row["candidate"]["commit"],
            "branch": row["branch"],
            "target_base": row["candidate"]["target_base"],
        }
        if draft:content['draft']=True
        if publication is not None:
            if not isinstance(publication,dict) or set(publication)!={'title','body'}:raise ValueError('Publication requires title/body.')
            text(publication['title']);text(publication['body'])
            content.update(publication)
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
            if self.spec.get("base_update") == "periodic":
                content["readiness_revision"] = row.get("merge_readiness_revision", 0)
            if self.ms.communication is not None:
                from homebrew_harness.services.communication.followup import basis

                feedback_basis = basis(self.ms.communication, row, conn)
                if feedback_basis is not None:
                    content["feedback_basis"] = feedback_basis
        if kind == "post_comment":
            text(body, maximum=16000)
            from homebrew_harness.platforms.contracts import ObjectRef

            ref = ObjectRef(**subject)
            if ref.scope() != (
                self.spec["provider"],
                self.spec["instance"],
                self.spec["project"],
            ):
                raise boundary_error(
                    ValueError, "delivery_contract", "Comment crosses platform scope."
                )
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
            publication = publication or row.get("publication") or (row.get("review") or {}).get("publication")
            if publication:
                content.update(publication)
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
        auto = kind in self.spec["auto_actions"] and (
            kind != "ensure_change_request"
            or (self.spec.get("auto_publish_pr_text") and not draft)
            or not (row.get("publication") or (row.get("review") or {}).get("publication"))
        )
        value = {
            "id": identity,
            "kind": kind,
            "work_id": row["work_id"],
            "changeset_id": row["id"],
            "plan": content,
            "plan_hash": fingerprint(content),
            "state": "queued" if auto else "awaiting_approval",
            "authorization": "host_policy" if auto else None,
            "expires_at": expiry(self.spec["authorization_seconds"], time.time()),
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
        self.require_unmerged(conn, row, action["kind"])
        if action["state"] not in {"awaiting_approval", "denied", "queued", "failed"}:
            raise boundary_error(
                ValueError,
                "delivery_contract",
                "Only an unstarted delivery action can request authorization.",
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
            "expires_at": expiry(self.spec["authorization_seconds"], time.time()),
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
            work = self.current(row, conn)
            extra = payload.get("allow_extra_attempt", False)
            if (
                request["state"] != "pending"
                or request["revision"] != payload["revision"]
                or expired(request["expires_at"], time.time())
                or row["state"] != "repair_waiting"
                or not scoped_repair(row)
                or request["requirements_revision"] != work["requirements_revision"]
                or request["candidate"]["plan"] != row["ci_diagnosis"]["plan"]
                or (self.repair_decision(row, conn) or {}).get("id") != request["id"]
            ):
                raise boundary_error(
                    ValueError, "delivery_contract", "CI decision is stale or expired."
                )
            if type(extra) is not bool or (extra and payload["decision"] != "approve") or "text" in payload:
                raise ValueError("A CI repair expects approve/deny and an optional Boolean extra-attempt grant.")
            if payload["decision"] == "approve":
                used = work.get("ci_fix_attempts", 0)
                if self.limit_reached(used) and not extra:
                    raise boundary_error(ValueError, "ci_repair_limit",
                        "The current repair needs an explicit one-attempt extension before approval.",
                        limit_name="ci_fix_limit", limit_value=self.spec["ci_fix_limit"],
                        observed_value=used, unit="repair_attempts")
                request.update(authorized_attempt=used + 1,
                               extra_attempt=extra and self.limit_reached(used))
                row.update(state="repair_approved")
            elif payload["decision"] == "deny":
                row.update(state="blocked", error="repair_denied")
            else:
                raise boundary_error(
                    ValueError, "delivery_contract", "CI repair expects approve/deny."
                )
            request.update(
                state="approved" if payload["decision"] == "approve" else "denied",
                revision=request["revision"] + 1,
            )
            row["revision"] += 1
            self.put(conn, "changesets", row)
            self.put(conn, "decisions", request)
            return {
                "id": request["id"],
                "state": request["state"],
                "request_id": decision_id,
            }
        if request["kind"] != "delivery_authority":
            raise boundary_error(
                ValueError, "delivery_contract", "Unknown delivery decision type."
            )
        action = self.get("actions", request["action_id"], conn)
        row = self.get("changesets", action["changeset_id"], conn)
        self.require_unmerged(conn, row, action["kind"])
        self.current(row, conn)
        if action["kind"] == "merge_change_request" and row["state"] != "merge_ready":
            raise boundary_error(
                ValueError, "delivery_contract", "PR is no longer merge-ready."
            )
        if (
            type(payload["revision"]) is not int
            or request["revision"] != payload["revision"]
            or request["state"] != "pending"
            or expired(request["expires_at"], time.time())
            or action["plan_hash"] != request["plan_hash"]
            or self.action_basis(row,action["plan"]) != action["plan"]["hash"]
            or action["state"] not in {"queued", "awaiting_approval"}
        ):
            raise boundary_error(
                ValueError, "delivery_contract", "Delivery request is stale or expired."
            )
        if payload["decision"] not in {"approve", "deny"} or "text" in payload:
            raise boundary_error(
                ValueError,
                "delivery_contract",
                "Delivery expects an explicit approve/deny.",
            )
        approve = payload["decision"] == "approve"
        action.update(
            state="queued" if approve else "denied",
            authorization="operator:" + decision_id if approve else None,
            expires_at=expiry(self.spec["authorization_seconds"], time.time()),
            revision=action["revision"] + 1,
        )
        request.update(
            state="approved" if approve else "denied", revision=request["revision"] + 1
        )
        self.put(conn, "actions", action)
        self.put(conn, "decisions", request)
        if not approve:
            if action["kind"] == "ensure_change_request":
                row["state"] = "publication_denied"
            row.update(error="delivery_action_denied",
                       error_detail="The exact " + action["kind"] + " plan was denied; inspect its decision and approval review.",
                       denied_action_id=action["id"], denied_request_id=request["id"],
                       revision=row["revision"] + 1)
            self.put(conn, "changesets", row)
        elif row.get("error") == "delivery_action_denied" and row.get("denied_action_id") == action["id"]:
            if row["state"] == "publication_denied":
                row["state"] = "review_branch_ready"
            row.update(error=None, error_detail=None, error_info=None, revision=row["revision"] + 1)
            self.put(conn, "changesets", row)
        self.ms.changed(conn, row["work_id"])
        return {
            "id": request["id"],
            "state": request["state"],
            "request_id": decision_id,
        }

    def start_action(self, action):
        with self.db.write() as conn:
            meta = self.runtime.store.guard(conn, self.runtime.header)
            current = self.get("actions", action["id"], conn)
            row = self.get("changesets", current["changeset_id"], conn)
            self.require_unmerged(conn, row, current["kind"])
            if meta["state"] != "running":
                raise boundary_error(
                    ValueError,
                    "delivery_contract",
                    "Service is not executing delivery.",
                )
            self.current(row, conn)
            if (
                current["state"] != "queued"
                or current["authorization"] is None
                or expired(current["expires_at"], time.time())
            ):
                raise boundary_error(
                    ValueError,
                    "delivery_contract",
                    "Action is not authorized or has expired.",
                )
            if self.action_basis(row,current["plan"]) != current["plan"]["hash"]:
                raise boundary_error(
                    ValueError, "delivery_contract", "Action candidate changed."
                )
            if not current['plan'].get('draft') and row["frozen"] != self.freeze(row):
                raise boundary_error(
                    ValueError,
                    "delivery_contract",
                    "Candidate quality evidence changed.",
                )
            if (
                current["kind"] == "merge_change_request"
                and row["state"] != "merge_ready"
            ):
                raise boundary_error(
                    ValueError,
                    "delivery_contract",
                    "Target delivery is not merge-ready.",
                )
            if current["kind"] == "merge_change_request":
                from .merge_flow import gate

                if (self.spec.get("base_update") == "periodic" and "readiness_revision" in current["plan"]
                        and current["plan"]["readiness_revision"] != row.get("merge_readiness_revision", 0)):
                    raise StaleDelivery("Merge readiness changed before admission.")
                if self.ms.communication is not None:
                    from homebrew_harness.services.communication.followup import basis, pending

                    if (pending(self.ms.communication, row, conn)
                            or current["plan"].get("feedback_basis") != basis(self.ms.communication, row, conn)):
                        raise StaleDelivery("PR feedback changed before merge admission.")

                if current["plan"]["gate_hash"] != gate(row["ci"], self.spec)[
                    "hash"
                ] or current["plan"]["policy"] != fingerprint(self.spec["merge"]):
                    raise boundary_error(
                        ValueError,
                        "delivery_contract",
                        "Merge evidence changed since authorization.",
                    )
            pending = conn.execute(
                "SELECT 1 FROM records WHERE section='service_actions' AND owner=? AND key<>? AND json_extract(value,'$.state') IN ('started','unknown') LIMIT 1",
                ("service:" + self.id, current["id"]),
            ).fetchone()
            if pending:
                raise boundary_error(
                    ValueError,
                    "delivery_contract",
                    "Inspect uncertain external actions before further writes.",
                )
            from homebrew_harness.services.action_runtime import start_attempt

            current = start_attempt(current, self.runtime.header["generation"])
            return self.put(conn, "actions", current)

    def preflight_failed(self, action, error):
        from homebrew_harness.services.action_runtime import preflight_failure

        with self.db.write() as conn:
            self.runtime.store.guard(conn, self.runtime.header)
            current = self.get("actions", action["id"], conn)
            if (
                current["state"] != "queued"
                or current["revision"] != action["revision"]
            ):
                return current
            return self.put(conn, "actions", preflight_failure(current, error))

    def expire_action(self, action):
        with self.db.write() as conn:
            self.runtime.store.guard(conn, self.runtime.header)
            current = self.get("actions", action["id"], conn)
            if current["state"] != "queued" or not expired(current["expires_at"], time.time()):
                return
            row = self.get("changesets", action["changeset_id"], conn)
            if current["kind"] in PR_MUTATIONS and (merged := self.merged_pr(conn, row)):
                self.retire_action(conn, current, merged)
                return
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
            if action["kind"] in PR_MUTATIONS and self.merged_pr(conn, row):
                # Keep the actual effect receipt without reopening a completed PR.
                self.ms.changed(conn, row["work_id"])
                return current
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
                        state=row['state'] if action['plan'].get('draft') else 'ci_waiting',
                        draft_published=bool(action['plan'].get('draft')),
                        ci_started=time.time(),
                        next_ci=0,
                    )
                    if row.get("integration_schedule") and not action["plan"].get("draft"):
                        from .periodic import after_publication

                        row["integration_schedule"] = after_publication(row)
                elif action["kind"] == "merge_change_request":
                    row.update(merge=result, state="merged_verifying", next_merge=0)
                self.put(conn, "changesets", row)
                if observed_merge(row):
                    self.retire_publication(conn, work_id=row["work_id"])
                if self.ms.communication is not None:
                    self.ms.communication.link_delivery(conn, current)
            self.ms.changed(conn, row["work_id"])
            return current

    def finish_model(self, conn, job, result):
        row = self.get("changesets", job["target"], conn)
        self.current(row)
        if row["revision"] != job["revision"]:
            return "stale"
        if job["kind"] == "validation_plan":
            row.update(validation_plan=result, state="validating", error=None)
            if result.get('draft') and not self.merged_pr(conn, row):
                self.action(conn,row,'publish_branch',draft=True)
                self.action(conn,row,'ensure_change_request',draft=True,publication=result['draft'])
        elif job["kind"] == "publication":
            from .publication import accept
            accept(self, row, job, result, conn)
        elif job["kind"] == "review":
            row["review"] = result
            if any(f["severity"] == "blocking" for f in result["findings"]):
                row.update(state="ci_diagnosing", error="review_findings",
                           ci={"kind": "independent_review", "head": row["candidate"]["commit"], "findings": result["findings"]})
                row["revision"] += 1
                self.put(conn, "changesets", row)
                self.ms.job(conn, "ci_triage", row["id"], row["revision"], {},
                            row["id"] + ":review-response:" + fingerprint(result))
                self.ms.changed(conn, row["work_id"])
                return "completed"
            elif not row["candidate"]["delta"]:
                row.update(state="already_present", observation_terminal=True)
                work = self.current(row, conn)
                work.update(
                    state="delivered",
                    result={
                        "method": "existing_target",
                        "target_commit": row["candidate"]["target_base"],
                        "change_request": None,
                        "quality": row.get("quality"),
                        "review": result,
                    },
                    revision=work["revision"] + 1,
                )
                self.db._put(conn, "service_work", work["id"], work)
                if self.ms.communication is not None:
                    self.ms.communication.retire_requirements(conn, work)
            else:
                row.update(state="review_branch_ready")
                row["frozen"] = self.freeze(row)
        elif job["kind"] == "ci_triage":
            row["ci_diagnosis"] = result
            row["reasoning_job_id"] = job["id"]
            action = result.get("next_action")
            if action=='retry_prepare':
                row.update(state='queued',error=None,next_validation=0)
            elif action == "assess_integration":
                from .periodic import ci_failure_basis

                row.update(state="review_waiting", error="baseline_ci_integration_pending",
                           integration_ci_basis=ci_failure_basis(self.spec, row["ci"]), next_merge=0)
            elif action in {"defer", "stop", "human_required"}:
                row.update(state="blocked", error="model_" + action, error_detail=result["reason"])
            elif action == "plan_validation":
                row["validation_history"] = [*row.get("validation_history", []), {"candidate_commit": row["candidate"]["commit"], "quality": row.get("quality"), "test_failure": row.get("test_failure"), "validation_plan": row.get("validation_plan"), "reason": result["reason"]}]
                row.update(state="validating", validation_execution=None, validation_plan=None, error=None)
            elif action == "reconsider_review":
                row["review_history"] = [*row.get("review_history", []), row.get("review")]
                row.update(state="reviewing", review=None, review_response=result, error=None)
                row["revision"] += 1
                self.put(conn, "changesets", row)
                self.ms.job(conn, "review", row["id"], row["revision"], {},
                            row["id"] + ":review-reconsider:" + str(row["revision"]))
                self.ms.changed(conn, row["work_id"])
                return "completed"
            elif action == "proceed":
                if self.spec["require_review"] and any(f["severity"] == "blocking" for f in (row.get("review") or {}).get("findings", [])):
                    raise ValueError("The operator requires a passing independent review; fix or request an evidence-based reconsideration.")
                if row.get("test_failure") or row.get("quality") is None or (row.get('goal_evidence') or {}).get('ok') is False:
                    raise ValueError("Explicit candidate acceptance remains unmet; choose verification or explain the remaining work.")
                row.update(state="review_branch_ready", review_response=result, error=None)
                row["frozen"] = self.freeze(row)
            elif action == "repair" or action is None and scoped_repair(row):
                work = self.current(row, conn)
                if self.spec["auto_ci_fix"] and not self.limit_reached(work.get("ci_fix_attempts", 0)):
                    row["state"] = "repair_approved"
                else:
                    row["state"] = "repair_waiting"
                    self.repair_request(conn, row)
            elif action == "recheck" or action is None and row.get("test_failure") and result["classification"] in {"flaky", "infrastructure"}:
                rechecks = row.get("validation_rechecks", 0)
                if self.limit_reached(rechecks):
                    row.update(state="blocked", error="candidate_recheck_limit",
                               error_detail="Model-requested candidate rechecks reached ci_fix_limit=" + str(self.spec["ci_fix_limit"]))
                else:
                    row.update(state="validating", validation_execution=None, validation_rechecks=rechecks + 1, force_test_recheck=True,
                               next_validation=time.time() + self.spec["ci_poll_seconds"],
                               error=None, error_detail=None)
            else:
                row.update(state="diagnosis_waiting", error=None, error_detail=result["reason"],
                           next_ci=time.time() + result.get("wait_seconds", self.spec["ci_poll_seconds"]))
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
                if observed_merge(row):
                    self.retire_publication(conn, work_id=row["work_id"])
                if (
                    row["state"] in {"review_branch_ready", "quality_failed"}
                    and not pending_quality
                ):
                    row.update(state="merged_verifying", revision=row["revision"] + 1)
                    self.put(conn, "changesets", row)
                return row
            if row["candidate"]["commit"] != observed["head"]:
                raise boundary_error(
                    ValueError,
                    "delivery_contract",
                    "Merge observation belongs to another candidate.",
                )
            if observed["subject"] != row["change_request"]:
                raise boundary_error(
                    ValueError,
                    "delivery_contract",
                    "Merge observation belongs to another PR.",
                )
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
            if observed_merge(row):
                self.retire_publication(conn, work_id=row["work_id"])
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
            raise boundary_error(
                ValueError,
                "delivery_contract",
                "Integration completion needs a verified content and quality receipt.",
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
                raise boundary_error(
                    ValueError,
                    "delivery_contract",
                    "Verified merge result changed before completion.",
                )
            from pathlib import Path

            expected = (
                self.runtime.store.root
                / ".harness/delivery/integrations"
                / fingerprint([row["id"], actual["head"], actual["commit"]])
                / "workspace"
            )
            if Path(actual["workspace"]) != expected:
                raise boundary_error(
                    ValueError,
                    "delivery_contract",
                    "Verified integration workspace is outside its fixed owner.",
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
                if self.ms.communication is not None:
                    self.ms.communication.retire_requirements(conn, work)
            else:
                row["requirements_superseded"] = True
            self.put(conn, "changesets", row)
            self.retire_publication(conn, work_id=row["work_id"])
            self.ms.changed(conn, row["work_id"])
            return row
