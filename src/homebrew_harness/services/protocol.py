"""Versioned control methods; no runtime or transport dependency."""

import re
import json
import hashlib

VERSION = 1
MAX_FRAME = 256 * 1024
SERVICE_ID = re.compile(r"svc_[0-9a-f]{32}\Z")
MAINTENANCE_QUERIES = {
    "communication_get": {"kind", "id"},
    "communication_page": {"kind", "cursor", "limit", "state"},
    "communication_read": {"kind", "identity", "offset", "limit"},
    "communication_events": {"id", "cursor", "limit"},
    "delivery_status": {"kind", "id"},
    "service_page": {"kind", "cursor", "limit", "state"},
    "service_read": {"kind", "identity", "offset", "limit"},
    "job_get": {"id"},
    "conversation_get": {"id"},
    "report_get": {"id"},
    "report_inbox": {"context", "cursor", "limit"},
}
OPERATIONS_QUERIES = {
    "ops_health": set(),
    "ops_list": {"cursor", "limit"},
    "ops_get": {"id"},
    "ops_read": {"id", "offset", "limit"},
}
OPERATIONS_COMMANDS = {
    "ops_quiesce": set(),
    "ops_resume_operations": set(),
    "ops_backup": set(),
    "ops_verify_backup": {"id"},
    "ops_restore_backup": {"id"},
    "ops_retention_preview": set(),
    "ops_retention_apply": {"id"},
    "ops_audit": {"force"},
    "ops_diagnose": {"id"},
    "ops_upgrade_prepare": {"alias"},
    "ops_upgrade_arm": {"id"},
    "ops_upgrade_confirm": {"id"},
    "ops_retry": {"id"},
}
QUERY_METHODS = {
    **OPERATIONS_QUERIES,
    **MAINTENANCE_QUERIES,
    "snapshot": set(),
    "focus_get": {"context"},
    "work_get": {"id"},
    "work_input": {"id", "offset", "limit", "amendment"},
    "work_list": {"state", "cursor", "limit"},
    "schedules": {"cursor", "limit"},
    "requests": {"work_id"},
    "receipt": {"request_id"},
}
MAINTENANCE_COMMANDS = {
    "discussion_assess": {"id", "revision"},
    "discussion_pause": {"id", "revision"},
    "discussion_reopen": {"id", "revision"},
    "discussion_prepare_review": {"id", "revision"},
    "communication_plan": {"id", "revision", "plan"},
    "communication_request_authority": {"id", "revision"},
    "communication_inspect": {"id"},
    "delivery_prepare": {"id", "revision"},
    "delivery_plan": {"id", "revision", "kind", "body", "subject"},
    "delivery_request": {"id"},
    "delivery_inspect": {"id", "conclusion", "confirmed"},
    "delivery_refresh": {"id"},
    "batch_create": {"refs"},
    "batch_decide": {"id", "revision", "decisions"},
    "conversation_submit": {"text", "context", "work_id", "revision"},
    "service_decide": {"id", "revision", "decision", "text"},
    "report_create": set(),
    "report_ack": {"id", "context"},
    "job_retry": {"id"},
    "work_revise": {"id", "revision", "text"},
    "work_request_authority": {"id", "revision"},
    "authority_revoke": {"id", "revision"},
}
COMMAND_METHODS = {
    **OPERATIONS_COMMANDS,
    **MAINTENANCE_COMMANDS,
    "focus": {"context", "id", "revision"},
    "tick": set(),
    "submit": {"text", "goal", "priority"},
    "pause": {"revision", "scope"},
    "resume": {"revision"},
    "stop": {"revision"},
    "work_pause": {"id", "revision"},
    "work_resume": {"id", "revision"},
    "work_cancel": {"id", "revision"},
    "steer": {"id", "revision", "text"},
    "interrupt": {"id", "revision", "text"},
    "decide": {"ref", "decision", "text", "runs", "seconds"},
}
METHODS = {**QUERY_METHODS, **COMMAND_METHODS}


def validate_method(method, payload):
    if (
        not isinstance(method, str)
        or method not in METHODS
        or not isinstance(payload, dict)
        or set(payload) - METHODS[method]
    ):
        raise ValueError("Unknown control method or fields.")


def validate_envelope(value):
    if not isinstance(value, dict) or set(value) != {
        "version",
        "service_id",
        "method",
        "payload",
        "request_id",
    }:
        raise ValueError("Invalid control envelope.")
    if type(value["version"]) is not int or value["version"] != VERSION:
        raise ValueError("Unsupported control protocol version.")
    if not isinstance(value["service_id"], str) or not SERVICE_ID.fullmatch(
        value["service_id"]
    ):
        raise ValueError("Invalid service reference.")
    validate_method(value["method"], value["payload"])
    request_id = value["request_id"]
    if request_id is not None and (
        not isinstance(request_id, str) or not 1 <= len(request_id) <= 256
    ):
        raise ValueError("Invalid control request_id.")
    if value["method"] in COMMAND_METHODS and request_id is None:
        raise ValueError("Control commands require a stable request_id.")


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, allow_nan=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


def request_key(kind, scope, request_id):
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 256:
        raise ValueError("request_id must contain 1 to 256 characters.")
    return fingerprint([kind, scope, request_id])[:32]
