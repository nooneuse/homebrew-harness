"""Service approval views reuse the shared reviewer and native service decisions."""

import copy
import threading

from homebrew_harness.security.approval_review import ApprovalReviewer, ReviewConfig


class ServiceApprovalReview:
    def __init__(self, runtime):
        self.runtime = runtime
        self.config = ReviewConfig.parse(
            getattr(runtime.core.config.settings, "approval_review", None)
        )
        self.running = False
        self.reviewer = None
        self.stop = threading.Event()

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
        if not any(
            row["kind"] in self.config.kinds
            and row["ref"]["target"]["kind"] == "service"
            for row in runtime.requests()
        ):
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
        proposal = {"decision": raw, "trust": "proposal, not authorization"}
        host_scope = {
            "project_policy": maintenance.policy,
            "delegated_kinds": list(self.config.kinds),
        }
        if work:
            proposal.update(
                prompt=work["prompt"],
                goal=work.get("goal"),
                requirements_revision=work["requirements_revision"],
            )
            host_scope.update(
                run_scope=work["run_spec"], accepted_authority=work.get("authority")
            )
        if current["kind"] == "communication_authority":
            communication = runtime.maintenance.communication.store
            proposal["action"] = communication.get(
                "communication_actions", raw["action_id"]
            )
            host_scope["communication_policy"] = communication.policy
        elif current["kind"] == "delivery_authority":
            proposal["action"] = maintenance.get("actions", raw["action_id"])
            proposal["changeset"] = maintenance.get("changesets", raw["changeset_id"])
            host_scope["delivery_policy"] = runtime.maintenance.delivery.store.spec
        return {
            "cwd": runtime.spec.workspace,
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
                if (
                    runtime.closed
                    or view["kind"] not in self.config.kinds
                    or view["ref"]["target"]["kind"] != "service"
                ):
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
                    "arguments": view.get("arguments"),
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
                        and r["kind"] in self.config.kinds), None)
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
