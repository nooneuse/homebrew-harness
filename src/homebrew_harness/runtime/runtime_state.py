"""Current runtime failure contract."""

import copy


class RecoveryRequired(ValueError):
    retryable = False


def settled_operation(record):
    return isinstance(record, dict) and record.get("state") in {"done", "inspected"}


def settled_checkpoint(key, snapshot):
    phase = snapshot.get("phase")
    return (
        phase in {"closed", "executed"}
        or key.startswith("sub:")
        and phase in {"completed", "not_executed"}
    )


def pending_model_work(usage):
    """Only execution facts block retirement; detail logs are optional observation."""
    if not isinstance(usage, dict):
        return ["invalid_usage_record"]
    pending = usage.get("pending_requests", [])
    counts = [usage.get("inflight", 0), usage.get("pending_usage_calls", 0)]
    if not isinstance(pending, list) or any(
        type(value) is not int or value < 0 for value in counts
    ):
        return ["invalid_usage_record"]
    return ["unsettled_model_request"] if pending or any(counts) else []


def settle_dead_usage(usage):
    """The owning process ended; sent requests with no response have unknown usage."""
    value = copy.deepcopy(usage)
    pending = value.get("pending_requests", [])
    if not isinstance(pending, list):
        raise RecoveryRequired("Invalid interrupted request observations.")
    sent = [p for p in pending if p.get("sent")]
    value["unknown_usage_calls"] = value.get("unknown_usage_calls", 0) + len(sent)
    for p in sent:
        for table, key in (
            ("by_role", p.get("role", "lead")),
            ("by_purpose", p.get("purpose", "model")),
        ):
            bucket = value.setdefault(table, {}).setdefault(
                key, {"calls": 0, "known_tokens": 0, "unknown_usage_calls": 0}
            )
            bucket["calls"] += 1
            bucket["unknown_usage_calls"] += 1
    value.update(pending_requests=[], inflight=0, pending_usage_calls=0)
    if pending:
        value["observation_incomplete"] = True
    return value
