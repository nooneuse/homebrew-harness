"""Adapt durable Run requests to the shared reviewer and original decision publisher."""

from homebrew_harness.security.approval_review import requester_binding


def review_run(control, request):
    session = control.session
    reviewer = getattr(session, "approval_reviewer", None)
    if reviewer is None:
        return request
    if (
        request["state"] != "pending"
        or reviewer.config.mode != "auto_review"
        or request["kind"] not in reviewer.config.kinds
    ):
        return request
    requester = requester_binding()
    cwd = control.store.root / request["context"].get("cwd", ".")

    def host_scope(binding):
        scope = {
            "request_binding": binding,
            "write_paths": list(control.spec.write_paths),
            "tools": sorted(control.spec.effective_tools()),
            "preauthorized_tests": list(control.spec.allow_tests),
            "goal": control.spec.goal,
            "mcp_delegation": control.spec.mcp_delegation,
            "bash_backend": session.bash_policy.identity(),
        }
        if binding.get("task_id") and control.stages:
            scope["stage"] = control.stages.definition(
                session.workspace.store.load(binding["task_id"])
            )
        return scope

    snapshot = {
        "cwd": str(cwd),
        "intent": session.intent.to_dict(),
        "host_scope": host_scope(request["context"]),
        "lead_binding": session.lead_binding.identity(),
        "policy": reviewer.config.data(),
    }

    def current():
        value = control.requests.get(control.id, request["id"])
        if value["state"] != "pending" or value["revision"] != request["revision"]:
            return None
        if request["kind"] == "tool":
            from homebrew_harness.runs.requests import context

            scope = context(control, request["tool"], request["arguments"])
        else:
            from homebrew_harness.runs.test_authorization import valid

            if not valid(
                control.store, control.store.get(control.id), request["context"]
            ):
                return None
            scope = request["context"]
        return {
            "cwd": str(cwd),
            "intent": session.intent.to_dict(),
            "host_scope": host_scope(scope),
            "lead_binding": session.lead_binding.identity(),
            "policy": reviewer.config.data(),
        }

    def publish(decision, review_id):
        if request["kind"] == "tool":
            session._save_checkpoint()
        return control.requests.decide(
            control.id, request["id"], request["revision"], decision
        )

    reviewer.review(request, snapshot, current, publish, requester=requester)
    return control.requests.get(control.id, request["id"])
