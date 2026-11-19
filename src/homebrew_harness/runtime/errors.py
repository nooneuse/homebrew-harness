"""errors for the harness."""

from __future__ import annotations


class AgentError(RuntimeError):
    """A readable error that never includes private connection settings."""


def annotate_error(error, *, code=None, message=None, **details):
    """Add observed boundary facts without changing exception or retry semantics."""
    from homebrew_harness.security.data_boundary import redact

    try:
        if message is not None:
            error.diagnostic_message = redact(str(message))[:2000]
        if code is not None:
            error.code = code
        previous = getattr(error, "details", {})
        error.details = redact({**(previous if isinstance(previous, dict) else {}), **details})
    except (AttributeError, TypeError):
        pass
    return error


def boundary_error(kind, code, message, **details):
    return annotate_error(kind(message), code=code, **details)


def error_info(error):
    """A bounded diagnostic, excluding provider payloads and reasoning text."""
    from homebrew_harness.security.data_boundary import redact
    import math

    chain, seen = [], set()
    while error is not None and id(error) not in seen and len(chain) < 8:
        seen.add(id(error))
        chain.append(error)
        error = error.__cause__
    if not chain:
        return None
    details, messages, code = {}, [], None
    for item in reversed(chain):
        value = getattr(item, "details", {})
        allowed = {"limit_name", "limit_value", "observed_value", "unit", "source", "phase",
                   "request_id", "sent", "settled", "purpose", "protocol", "context_id",
                   "gateway_request_timeout_seconds", "client_timeout_seconds", "request_timeout_seconds", "effective_timeout_seconds", "deadline_source",
                   "output_limit", "max_output_tokens", "output_tokens", "input_tokens",
                   "reasoning_tokens", "total_tokens", "finish", "provider_finish_reason",
                   "provider_incomplete_reason", "job_id", "job_kind", "job_seconds", "job_turns",
                   "elapsed_seconds", "preparation_seconds", "queue_seconds", "status_code", "kind", "workflow_timeout_seconds",
                   "component", "operation", "action_id", "task_id", "member", "profile", "field", "line", "column",
                   "errno", "outcome", "platform_code", "platform_request_id", "retry_after",
                   "attempts", "attempt_limit", "retry_stop", "previous_error", "failed_profiles",
                   "summary_ref", "target_characters", "input_characters", "chunk", "chunks",
                   "fixed_characters", "remaining_summary_characters", "archive_ref",
                   "file_path", "expected_version", "observed_version", "restart_offset",
                   "total_summary_characters", "used_summary_characters", "reserved_summary_characters",
                   "nominal_summary_characters", "acceptance_summary_characters",
                   "summary_characters", "soft_target_characters", "rewrite_attempts", "transcript_ref"}
        allowed.update({"rollback_confirmed", "commit_state", "persistence_timeout_seconds"})
        if isinstance(value, dict):
            details.update({key: (item[:512] if isinstance(item, str) else None if isinstance(item, float) and not math.isfinite(item) else item)
                            for key, item in value.items() if key in allowed
                            and (item is None or isinstance(item, (str, int, float, bool)))})
        if isinstance(item, OSError) and item.errno is not None:
            details["errno"] = item.errno
        trace = item.__traceback__
        while trace is not None:
            module = trace.tb_frame.f_globals.get("__name__", "")
            if module.startswith("homebrew_harness."):
                details["origin"] = module + "." + trace.tb_frame.f_code.co_name + ":" + str(trace.tb_lineno)
            trace = trace.tb_next
        current = getattr(item, "code", None) or getattr(item, "reason_code", None)
        if isinstance(current, str) and current:
            code = current[:128]
        for key in ("source", "phase", "request_id", "sent", "settled", "status_code", "kind"):
            value = getattr(item, key, None)
            if isinstance(value, (str, bool, int, float)):
                details[key] = value
    for item in chain:
        # Arbitrary extension/SDK exception bodies are not a public message.
        trusted = item.__class__.__module__.startswith("homebrew_harness.") or getattr(item, "code", None) or getattr(item, "reason_code", None)
        message = getattr(item, "diagnostic_message", None)
        if message is None and isinstance(item, OSError) and item.errno is not None:
            import os
            message = os.strerror(item.errno)
        if message is None:
            message = str(item) if trusted else type(item).__name__
        message = redact(message)
        if message not in messages and not any(message in previous for previous in messages):
            messages.append(message)
    return redact({"type": type(chain[0]).__name__, "code": code,
                   "message": ": ".join(messages)[:2000], "details": details})


def error_message(error):
    info = error_info(error)
    if info is None:
        return ""
    label = info["type"] + ("[" + info["code"] + "]" if info["code"] else "")
    fields = ("limit_name", "limit_value", "observed_value", "unit", "source", "phase", "rollback_confirmed", "commit_state", "persistence_timeout_seconds",
              "gateway_request_timeout_seconds", "client_timeout_seconds", "request_timeout_seconds", "effective_timeout_seconds", "workflow_timeout_seconds", "output_limit", "max_output_tokens",
              "output_tokens", "reasoning_tokens", "provider_finish_reason", "provider_incomplete_reason",
              "job_seconds", "job_turns", "elapsed_seconds", "preparation_seconds", "queue_seconds",
              "request_id", "sent", "settled", "component", "operation", "action_id", "field", "line", "column",
              "errno", "outcome", "status_code", "task_id", "member", "platform_code", "platform_request_id", "retry_after",
              "attempts", "attempt_limit", "retry_stop", "previous_error", "failed_profiles",
              "summary_ref", "target_characters", "input_characters", "chunk", "chunks",
              "fixed_characters", "remaining_summary_characters", "archive_ref",
              "file_path", "expected_version", "observed_version", "restart_offset",
              "total_summary_characters", "used_summary_characters", "reserved_summary_characters",
              "nominal_summary_characters", "acceptance_summary_characters",
              "summary_characters", "soft_target_characters", "rewrite_attempts", "transcript_ref")
    facts = ", ".join(key+"="+str(info["details"][key]) for key in fields if key in info["details"] and key+"=" not in info["message"])
    return label + (": " + info["message"] if info["message"] != info["type"] else "") + (" ("+facts+")" if facts else "")


def failure_fields(error, **context):
    """Store one diagnostic alongside the existing short error field."""
    annotate_error(error, **context)
    info = error_info(error)
    return {"error": type(error).__name__, "error_detail": error_message(error), "error_info": info}


def failure_signature(info):
    """New request IDs and timings do not make the same fault a new incident."""
    import re
    if not info:
        return None
    volatile = {"request_id", "platform_request_id", "elapsed_seconds", "preparation_seconds", "queue_seconds"}
    message = info["message"]
    for key in volatile:
        message = re.sub(r"\b" + key + r"=[^,;)\s]+", key + "=<observed>", message)
    return {"type": info["type"], "code": info["code"], "message": message,
            "details": {k: v for k, v in info["details"].items() if k not in volatile}}


def failure_record(error, previous=None, **context):
    """Coalesce repeated failures; callers decide when recovery is proven."""
    import time
    fields = failure_fields(error, **context)
    previous = previous or {}
    same = failure_signature(previous.get("error_info")) == failure_signature(fields["error_info"]) and not previous.get("resolved_at")
    now = time.time()
    return {**fields, "first_seen": previous["first_seen"] if same else now,
            "last_seen": now, "occurrences": previous.get("occurrences", 0) + 1 if same else 1}
