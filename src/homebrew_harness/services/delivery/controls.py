"""Host controls over fixed delivery plans and their observed outcomes."""

from .batches import DecisionBatches


def command(delivery, method, payload, request_id):
    runtime, ms = delivery.runtime, delivery.ms
    if method == "batch_create":
        return DecisionBatches(runtime, ms).create(payload["refs"], request_id)
    if method == "batch_decide":
        return DecisionBatches(runtime, ms).apply(
            payload["id"], payload["revision"], payload["decisions"], request_id
        )
    if method == "delivery_refresh":
        from .merge_flow import refresh
        from homebrew_harness.services.slow_controls import perform

        def run():
            result = refresh(delivery, payload["id"])
            return status(delivery, "changesets", result["id"])

        return perform(runtime, method, payload, request_id, run)
    if method == "delivery_inspect":
        from homebrew_harness.execution.execution_leases import ExecutionLeases
        from homebrew_harness.services.protocol import request_key, fingerprint

        saved = runtime.store.runtime.get(
            "service_commands", request_key("command", runtime.id, request_id)
        )
        if saved:
            if saved["payload"] != fingerprint([method, payload]):
                raise ValueError("Control request_id has another payload.")
            return saved["result"]
        if payload["conclusion"] == "reconcile":
            from homebrew_harness.services.slow_controls import perform

            def inspect():
                action = delivery.store.get("actions", payload["id"])
                if action["state"] == "confirmed":
                    return action
                if action["state"] == "unknown":
                    return delivery.reconcile(action)
                raise ValueError(
                    "Only an uncertain completed attempt can be inspected."
                )

            return perform(runtime, method, payload, request_id, inspect)
        if (
            payload["conclusion"] != "retry_not_executed"
            or payload.get("confirmed") is not True
        ):
            raise ValueError(
                "Explicitly confirm non-execution before authorizing another attempt."
            )

        def retry(conn):
            action = delivery.store.get("actions", payload["id"], conn)
            if action["state"] != "unknown":
                raise ValueError("Only an uncertain completed attempt can be retried.")
            row = delivery.store.get("changesets", action["changeset_id"], conn)
            delivery.store.current(row, conn)
            from homebrew_harness.services.action_runtime import confirm_unexecuted

            action = confirm_unexecuted(
                action,
                revision=action["revision"],
                confirmed=payload["confirmed"],
                decision=request_id,
            )
            delivery.store.put(conn, "actions", action)
            delivery.store.request(conn, row, action, renew=True)
            return action

        leases = ExecutionLeases(runtime.store.root)
        try:
            with leases.use("delivery-git"):
                return runtime.store.command(
                    runtime.header, method, payload, request_id, retry
                )
        finally:
            leases.close()

    def apply(conn):
        meta = runtime.store.guard(conn, runtime.header)
        if method in {"delivery_ci_limits", "delivery_base_update"}:
            import copy, json
            from .spec import validate_ci_limits, validate_base_update
            from homebrew_harness.services.protocol import fingerprint
            if method == "delivery_base_update":
                if set(payload) != {"mode"}:
                    raise ValueError("Base updates require an explicit mode.")
                limits = validate_base_update(payload["mode"])
            else:
                if set(payload) != {"ci_fix_limit", "ci_fix_warning"}:
                    raise ValueError("CI limits require both an explicit nullable budget and warning threshold.")
                limits = validate_ci_limits(payload["ci_fix_limit"], payload["ci_fix_warning"])
            if (meta["state"] != "paused" or meta["scope"] != "all" or delivery.preparing
                    or meta.get("active_run") or conn.execute(
                        "SELECT 1 FROM records WHERE section='service_jobs_model' AND owner=? "
                        "AND json_extract(value,'$.state') IN ('running','preparing') LIMIT 1",
                        ("service:" + runtime.id,)).fetchone()):
                raise ValueError("Pause all Service activity and wait for preparation before updating delivery settings.")
            for key, raw in conn.execute(
                    "SELECT key,value FROM records WHERE section='service_changesets' AND owner=?",
                    ("service:" + runtime.id,)).fetchall():
                row = json.loads(raw)
                if delivery.store.policy_matches(row):
                    row["policy_scope"] = delivery.store.policy
                    delivery.store.put(conn, "changesets", row)
            spec = copy.deepcopy(meta["spec"])
            spec["delivery"] = {**delivery.spec, **limits}
            binding = copy.deepcopy(meta["binding"])
            binding["maintenance"]["delivery"]["policy"] = fingerprint(spec["delivery"])
            meta.update(spec=spec, binding=binding, revision=meta["revision"] + 1)
            runtime.store.db._put(conn, "service_meta", runtime.id, meta)
            return limits
        if method == "delivery_prepare":
            work = runtime.store.work(payload["id"], runtime.id)
            if (
                type(payload["revision"]) is not int
                or work["revision"] != payload["revision"]
                or work["state"] != "review"
            ):
                raise ValueError("Prepare delivery only for current reviewed work.")
            return delivery.store.create(conn, work)
        if method == "delivery_plan":
            row = delivery.store.get("changesets", payload["id"], conn)
            if (
                type(payload["revision"]) is not int
                or row["revision"] != payload["revision"]
            ):
                raise ValueError("ChangeSet revision changed.")
            return delivery.store.action(
                conn,
                row,
                payload["kind"],
                body=payload.get("body"),
                subject=payload.get("subject"),
            )
        if method == "delivery_request":
            action = delivery.store.get("actions", payload["id"], conn)
            row = delivery.store.get("changesets", action["changeset_id"], conn)
            delivery.store.current(row)
            return delivery.store.request(conn, row, action, renew=True)
        raise ValueError("Unknown delivery control.")

    result = runtime.store.command(runtime.header, method, payload, request_id, apply)
    if method in {"delivery_ci_limits", "delivery_base_update"}:
        from homebrew_harness.services.protocol import fingerprint
        meta = runtime.store.get(runtime.id)
        fields = ("base_update",) if method == "delivery_base_update" else ("ci_fix_limit", "ci_fix_warning")
        delivery.spec.update({k: meta["spec"]["delivery"][k] for k in fields})
        delivery.store.legacy_policy = fingerprint({k: v for k, v in delivery.spec.items() if k not in {"ci_fix_warning", "base_update"}})
        runtime.saved_model_bindings = meta["binding"]
    return result


def status(delivery, kind, identity):
    if kind not in {"changesets", "actions", "batches"}:
        raise ValueError("Unknown delivery record kind.")
    value = delivery.store.get(kind, identity)
    if kind == "changesets":
        value = {
            **value,
            "candidate": {
                k: v
                for k, v in (value.get("candidate") or {}).items()
                if k not in {"files", "repo", "workspace"}
            },
            "quality": [
                {
                    k: v
                    for k, v in receipt.items()
                    if k in {"recipe", "status", "exit_code", "source_stable", "run_id"}
                }
                for receipt in value.get("quality") or []
            ],
            "integration": {
                k: v
                for k, v in (value.get("integration") or {}).items()
                if k not in {"files", "repo", "workspace", "source_commits"}
            },
            "merge_rules": {
                k: v
                for k, v in (value.get("merge_rules") or {}).items()
                if k in {"allowed", "method", "digest"}
            },
            "merge_rules_available": value.get("merge_rules") is not None,
            "candidate_history_available": bool(value.get("candidate_history")),
            "candidate_history": [
                {
                    k: v
                    for k, v in item.items()
                    if k in {"commit", "tree", "target_base", "artifacts_retired"}
                }
                for item in value.get("candidate_history", [])[-10:]
            ],
            "integration_quality": {
                "ok": (value.get("integration_quality") or {}).get("ok"),
                "quality": [
                    {
                        k: v
                        for k, v in receipt.items()
                        if k
                        in {"recipe", "status", "exit_code", "source_stable", "run_id"}
                    }
                    for receipt in (value.get("integration_quality") or {}).get(
                        "quality", []
                    )
                ],
                "goal_evidence": {
                    "ok": (
                        (value.get("integration_quality") or {}).get("goal_evidence")
                        or {}
                    ).get("ok")
                },
            },
            "integration_quality_available": value.get("integration_quality")
            is not None,
        }
    return value
