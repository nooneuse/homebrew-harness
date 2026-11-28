"""Service approval views reuse the shared reviewer and native service decisions."""

import copy
import json
import threading
from itertools import islice

from homebrew_harness.security.approval_review import ApprovalReviewer, ReviewConfig
from homebrew_harness.security.data_boundary import redact
from .job_contracts import JobContract, work_context
from .project_context import review_navigation


def evidence_preview(value, limit=8000):
    """Keep optional evidence bounded without substituting it for the exact request."""
    encoded = json.dumps(redact(value), ensure_ascii=False)
    if len(encoded) > limit:
        return {"preview": encoded[:limit], "truncated": True}
    return value


class ServiceApprovalReview:
    def __init__(self, runtime):
        self.runtime = runtime
        self.config = ReviewConfig.parse(
            getattr(runtime.core.config.settings, "approval_review", None)
        )
        self.running = False
        self.reviewer = None
        self.stop = threading.Event()

    @staticmethod
    def requires_operator(view):
        arguments = view.get("arguments") or {}
        if view["kind"] == "delivery_authority":
            # Delegation does not cover caller-supplied PR wording.
            return arguments.get("explicit_publication") is True
        if view["kind"] == "ci_repair":
            return bool((arguments.get("repair_attempts") or {}).get("extra_required"))
        if view["kind"] == "communication_authority":
            return (arguments.get("candidate") or {}).get("intent") in {
                "approve_review", "request_changes",
            }
        return False

    def delegated(self, view):
        return (
            view["kind"] in self.config.kinds
            and view["ref"]["target"]["kind"] == "service"
            and view.get("state") == "pending"
            and not self.requires_operator(view)
        )

    def tick(self):
        runtime = self.runtime
        if (
            self.config.mode != "auto_review"
            or runtime.closed
            or runtime.maintenance is None
            or self.running
            or runtime.store.get(runtime.id)["state"] != "running"
        ):
            return
        if not any(self.delegated(row) for row in runtime.requests()):
            return
        self.running = True
        try:
            runtime.workers.start(
                self.run, name="service-approval-review", track_operation=False
            )
        except BaseException:
            self.running = False
            raise

    def context(self, request):
        runtime = self.runtime
        meta = runtime.store.get(runtime.id)
        if meta["state"] != "running" or runtime.closed:
            return None
        current = next(
            (
                r
                for r in runtime.requests()
                if r["ref"] == request["ref"] and r.get("state") == "pending"
                and not self.requires_operator(r)
            ),
            None,
        )
        if current is None:
            return None
        maintenance = runtime.maintenance.store
        raw = maintenance.get("decisions", request["ref"]["id"])
        work = (
            runtime.store.work(raw["work_id"], runtime.id)
            if raw.get("work_id")
            else None
        )
        proposal = {"decision": copy.deepcopy(raw), "trust": "proposal, not authorization"}
        current = copy.deepcopy(current)
        cwd, navigation = runtime.spec.workspace, []
        host_scope = {
            "project_policy": maintenance.policy,
            "delegated_kinds": list(self.config.kinds),
        }
        if work:
            proposal["work"] = work_context(maintenance, work)
            host_scope.update(
                run_scope={key: value for key, value in (work.get("run_spec") or {}).items()
                           if key not in {"prompt", "work_context"}},
                accepted_authority=work.get("authority"),
            )
        kind = current["kind"]
        if kind in {"communication_authority", "discussion_requirements"}:
            communication = runtime.maintenance.communication.store
            if kind == "communication_authority":
                action = communication.get("communication_actions", raw["action_id"])
                plan = action.get("plan", {})
                if plan.get("kind") == "submit_review" and plan.get("event") != "COMMENT":
                    return None
                proposal["action"] = action
            host_scope["communication_policy"] = communication.policy
            discussion = communication.get("discussions", raw["discussion_id"])
            if kind == "discussion_requirements":
                projection = discussion.get("projection") or {}
                proposal["discussion"] = {
                    "id": discussion["id"],
                    "subject": evidence_preview(discussion.get("subject"), 1000),
                    "reason": evidence_preview(discussion.get("reason")),
                    "alignment": evidence_preview(discussion.get("alignment")),
                    "operator_note": evidence_preview(discussion.get("operator_note")),
                    "projection": {key: evidence_preview(projection[key])
                                   for key in ("facts", "questions", "findings", "draft")
                                   if key in projection},
                }
            if discussion.get("review_id"):
                snapshot = communication.get("review_snapshots", discussion["review_id"])
                cwd = snapshot["workspace"]
                if kind == "discussion_requirements":
                    proposal["discussion"]["review_snapshot"] = {
                        key: evidence_preview(snapshot.get(key))
                        for key in ("id", "basis", "coverage", "tests_passed")
                    }
            if discussion.get("last_round"):
                try:
                    completed = communication.get("rounds", discussion["last_round"])
                    prepared = maintenance.job_get(completed["job_id"])
                except ValueError:
                    prepared = None  # Retired optional navigation does not invalidate a pending request.
                if (prepared and prepared["target"] == discussion["id"]
                        and prepared["input"].get("review_id") == discussion.get("review_id")):
                    navigation = review_navigation(prepared)
        elif kind == "delivery_authority":
            proposal["action"] = maintenance.get("actions", raw["action_id"])
            plan = proposal["action"]["plan"]
            candidate_plan = proposal["decision"].get("candidate", {})
            if candidate_plan.get("plan") == str(plan):
                candidate_plan.pop("plan")
                candidate_plan["plan_ref"] = "proposal_evidence.action.plan"
            arguments = current.get("arguments") or {}
            if arguments.get("plan") == str(plan):
                arguments.pop("plan")
                arguments["plan_ref"] = "proposal_evidence.action.plan"
            changeset = maintenance.get("changesets", raw["changeset_id"])
            proposal["changeset"] = {key: copy.deepcopy(changeset.get(key)) for key in (
                "id", "work_id", "requirements_revision", "revision", "state", "run_id", "previous",
                "frozen", "branch", "review_commit", "change_request", "review", "ci", "ci_diagnosis",
                "goal_evidence", "error", "error_detail", "error_info", "publication", "publication_feedback")}
            proposal["changeset"]["quality"] = JobContract.quality(changeset.get("quality"))
            publication = proposal["changeset"].get("publication")
            if publication and all(publication.get(key) == plan.get(key) for key in ("title", "body")):
                proposal["changeset"]["publication"] = {"plan_ref": "proposal_evidence.action.plan"}
        elif kind == "ci_repair":
            changeset = maintenance.get("changesets", raw["changeset_id"])
            proposal["changeset"] = {key: changeset.get(key) for key in (
                "id", "work_id", "requirements_revision", "revision", "state",
            )}
            proposal["changeset"].update(
                quality=evidence_preview(JobContract.quality(changeset.get("quality"))),
                ci=evidence_preview(changeset.get("ci")),
                ci_diagnosis=evidence_preview(changeset.get("ci_diagnosis")),
                repair_attempts=(current.get("arguments") or {}).get("repair_attempts"),
            )
        if kind in {"delivery_authority", "ci_repair"}:
            candidate = changeset.get("candidate") or {}
            proposal["changeset"]["candidate"] = {
                key: evidence_preview(candidate.get(key)) if kind == "ci_repair" else candidate.get(key)
                for key in ("commit", "tree", "target_base", "delta", "conflicts")}
            host_scope["delivery_policy"] = runtime.maintenance.delivery.store.spec
            cwd = candidate.get("workspace") or cwd
            navigation = [{"path": path, "observed_sha256": value.get("sha256")}
                          for path, value in islice((candidate.get("files") or {}).items(), 16)]
        return {
            "cwd": cwd,
            "evidence_navigation": navigation,
            "intent": {
                "original": "Assess this proposed action against the operator-configured project policy and delegated approval kinds.",
                "operator_policy": maintenance.policy,
            },
            "proposal_evidence": copy.deepcopy(proposal),
            "host_scope": copy.deepcopy(host_scope),
            "request": copy.deepcopy(current),
            "policy": self.config.data(),
            "service_generation": meta.get("generation", 0),
        }

    def run(self):
        runtime = self.runtime
        try:
            agent = runtime.maintenance.agent
            if self.reviewer is None:
                self.reviewer = ApprovalReviewer(
                    self.config,
                    runtime.store.runtime,
                    agent.models.registry,
                    agent.model_control.contexts[agent.models.alias or "@default"][1]
                    if hasattr(agent, "model_control") else agent.gateway,
                    lambda: agent.base_binding,
                )
            for view in runtime.requests():
                if runtime.closed or self.stop.is_set() or not self.delegated(view):
                    continue
                try:
                    context = self.context(view)
                except (ValueError, OSError, KeyError):
                    continue
                if context is None:
                    continue
                request = {
                    "id": view["ref"]["id"],
                    "revision": view["ref"]["revision"],
                    "kind": view["kind"],
                    "checkpoint": "service:" + runtime.id,
                    "arguments": context["request"].get("arguments"),
                    "expires_at": view.get("expires_at"),
                    "reason": view.get("reason"),
                }

                def publish(decision, identity, view=view):
                    return runtime.dispatch(
                        runtime.id,
                        "service_decide",
                        {
                            "id": view["ref"]["id"],
                            "revision": view["ref"]["revision"],
                            "decision": decision,
                        },
                        "auto-review-" + identity,
                    )

                def current(view=view):
                    return self.context(view)

                with agent.gateway.scope(cancel_event=self.stop):
                    self.reviewer.review(request, context, current, publish)
        finally:
            self.running = False

    def retry(self, identity, revision, *, conn=None):
        if self.config.mode != "auto_review":
            raise ValueError("Automatic approval review is not enabled.")
        request = next((r for r in self.runtime.requests()
                        if r["ref"]["id"] == identity and r["ref"]["revision"] == revision
                        and self.delegated(r)), None)
        if request is None:
            raise ValueError("The approval request is no longer pending or delegated.")
        if self.reviewer is None:
            agent = self.runtime.maintenance.agent
            self.reviewer = ApprovalReviewer(self.config, self.runtime.store.runtime,
                agent.models.registry, agent.model_control.contexts[agent.models.alias or "@default"][1],
                lambda: agent.base_binding)
        return self.reviewer.request_retry(identity, revision, conn=conn)

    def request_close(self):
        self.stop.set()

    def close(self):
        self.stop.set()
        if self.reviewer:
            self.reviewer.close()
