"""Durable read/reconciliation controls execute outside the Service control lock."""

from homebrew_harness.execution.execution_leases import ExecutionLeases
from .protocol import request_key, fingerprint

METHODS = frozenset(
    {"communication_inspect", "delivery_inspect", "delivery_refresh", "batch_decide"}
)


def perform(runtime, method, payload, request_id, action):
    key = request_key("command", runtime.id, request_id)
    digest = fingerprint([method, payload])
    with runtime.store.db.connection() as conn:
        saved = runtime.store.db._get(conn, "service_commands", key)
    if saved:
        if saved["payload"] != digest:
            raise ValueError("Control request_id has another payload.")
        return saved["result"]
    leases = ExecutionLeases(runtime.store.root)
    try:
        with leases.use("slow-control:" + key):
            with runtime.store.db.connection() as conn:
                saved = runtime.store.db._get(conn, "service_commands", key)
            if saved:
                if saved["payload"] != digest:
                    raise ValueError("Control request_id has another payload.")
                return saved["result"]
            with runtime.store.db.write() as conn:
                runtime.store.guard(conn, runtime.header)
                old = runtime.store.db._get(conn, "service_control_attempts", key)
                if old and old["payload"] != digest:
                    raise ValueError("Control request_id has another accepted payload.")
                runtime.store.db._put(
                    conn,
                    "service_control_attempts",
                    key,
                    {
                        "payload": digest,
                        "method": method,
                        "state": "started",
                        "generation": runtime.header["generation"],
                        "service_id": runtime.id,
                        "checkpoint": "service:" + runtime.id,
                    },
                )
            result = action()

            def finish(conn):
                row = runtime.store.db._get(conn, "service_control_attempts", key)
                runtime.store.db._put(
                    conn, "service_control_attempts", key, {**row, "state": "completed"}
                )
                return result

            return runtime.store.command(
                runtime.header, method, payload, request_id, finish
            )
    finally:
        leases.close()
