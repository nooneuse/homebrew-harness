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
        runtime.store.guard(conn, runtime.header)
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
        if method == "delivery_refresh":
            row = delivery.store.get("changesets", payload["id"], conn)
            delivery.store.current(row)
            if row["state"] not in {"blocked", "ci_waiting", "review_waiting"}:
                raise ValueError("Refresh a blocked or waiting CI observation.")
            row.update(
                state="ci_waiting",
                next_ci=0,
                ci_started=__import__("time").time(),
                revision=row["revision"] + 1,
            )
            return delivery.store.put(conn, "changesets", row)
        raise ValueError("Unknown delivery control.")

    return runtime.store.command(runtime.header, method, payload, request_id, apply)


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
        }
    return value
