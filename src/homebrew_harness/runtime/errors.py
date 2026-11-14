"""errors for the harness."""

from __future__ import annotations


class AgentError(RuntimeError):
    """A readable error that never includes private connection settings."""


def annotate_error(error, *, code=None, **details):
    """Add observed boundary facts without changing exception or retry semantics."""
    from homebrew_harness.security.data_boundary import redact

    try:
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
                   "elapsed_seconds", "preparation_seconds", "queue_seconds", "status_code", "kind", "workflow_timeout_seconds"}
        if isinstance(value, dict):
            details.update({key: (item[:512] if isinstance(item, str) else None if isinstance(item, float) and not math.isfinite(item) else item)
                            for key, item in value.items() if key in allowed
                            and (item is None or isinstance(item, (str, int, float, bool)))})
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
        message = str(item) if trusted else type(item).__name__
        if message not in messages:
            messages.append(message)
    return redact({"type": type(chain[0]).__name__, "code": code,
                   "message": ": ".join(messages)[:2000], "details": details})


def error_message(error):
    info = error_info(error)
    if info is None:
        return ""
    label = info["type"] + ("[" + info["code"] + "]" if info["code"] else "")
    fields = ("limit_name", "limit_value", "observed_value", "unit", "source", "phase",
              "gateway_request_timeout_seconds", "client_timeout_seconds", "request_timeout_seconds", "effective_timeout_seconds", "workflow_timeout_seconds", "output_limit", "max_output_tokens",
              "output_tokens", "reasoning_tokens", "provider_finish_reason", "provider_incomplete_reason",
              "job_seconds", "job_turns", "elapsed_seconds", "preparation_seconds", "queue_seconds",
              "request_id", "sent", "settled")
    facts = ", ".join(key+"="+str(info["details"][key]) for key in fields if key in info["details"] and key+"=" not in info["message"])
    return label + (": " + info["message"] if info["message"] != info["type"] else "") + (" ("+facts+")" if facts else "")
