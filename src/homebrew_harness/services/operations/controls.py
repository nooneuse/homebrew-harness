"""Registered host commands; operation payloads never grant model permissions."""

from homebrew_harness.services.protocol import OPERATIONS_COMMANDS
from .health import snapshot


def dispatch(manager, method, payload, request_id):
    if "id" in payload:
        import re

        if not isinstance(payload["id"], str) or not re.fullmatch(
            r"(?:op|incident)_[0-9a-f]{32}", payload["id"]
        ):
            raise ValueError("Invalid operations record identity.")
    if method == "ops_upgrade_prepare" and (
        not isinstance(payload.get("alias"), str)
        or payload["alias"] not in manager.spec["upgrade_candidates"]
    ):
        raise ValueError("Select a registered upgrade alias.")
    if method == "ops_audit" and type(payload.get("force", False)) is not bool:
        raise ValueError("Audit force must be a boolean.")
    if method == "ops_health":
        return snapshot(manager.runtime)
    if method == "ops_list":
        return manager.page(**payload)
    if method == "ops_get":
        return manager.describe(manager.get(payload["id"]))
    if method == "ops_read":
        import json

        offset, limit = payload.get("offset", 0), payload.get("limit", 4000)
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 16000
        ):
            raise ValueError("Invalid operation record page.")
        value = json.dumps(manager.get(payload["id"]), ensure_ascii=False)
        return {
            "text": value[offset : offset + limit],
            "next_offset": offset + limit if offset + limit < len(value) else None,
            "length": len(value),
        }
    if method == "ops_retry":

        def retry(conn):
            row = manager.get(payload["id"])
            if row["state"] != "blocked":
                raise ValueError("Only a blocked operation can be retried.")
            import time

            row.update(
                state="queued",
                deadline_at=time.time() + manager.spec["operation_seconds"],
                error=None,
                revision=row["revision"] + 1,
            )
            return manager.put(conn, row)

        return manager.store.command(
            manager.runtime.header, method, payload, request_id, retry
        )
    if method not in OPERATIONS_COMMANDS:
        raise ValueError("Unsupported operations control.")
    if set(payload) != (
        OPERATIONS_COMMANDS[method] if method != "ops_audit" else set(payload)
    ):
        raise ValueError("Missing operations control fields.")
    return manager.enqueue(method[4:], payload, request_id)
