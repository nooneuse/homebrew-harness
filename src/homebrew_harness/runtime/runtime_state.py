"""Current runtime failure contract."""


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
