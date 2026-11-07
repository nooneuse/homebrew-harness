"""Structured execution certainty shared by parent, child and recovery paths."""

import json

INSPECTION_OUTCOMES = frozenset({"partial", "unknown"})


class ToolExecutionError(ValueError):
    """Host-owned effects that require inspection; never inferred from prose."""

    def __init__(self, message, *, outcome, details):
        if outcome not in INSPECTION_OUTCOMES or not isinstance(details, dict):
            raise ValueError("Invalid execution outcome.")
        super().__init__(message)
        self.outcome = outcome
        self.details = json.loads(
            json.dumps(details, ensure_ascii=False, allow_nan=False)
        )

    def serialize(self):
        return json.dumps(
            {
                "error": str(self),
                "outcome": self.outcome,
                "executed": True,
                "details": self.details,
            },
            ensure_ascii=False,
            allow_nan=False,
        )


def needs_inspection(output):
    try:
        value = json.loads(output) if isinstance(output, str) else output
        return isinstance(value, dict) and value.get("outcome") in INSPECTION_OUTCOMES
    except (TypeError, ValueError):
        return False


def child_result(run_id, status, *, result="", error="", effects=(), executed=None):
    value = {
        "run_id": run_id,
        "status": status,
        "outcome": status
        if status in INSPECTION_OUTCOMES
        else "completed"
        if status == "completed"
        else "not_executed",
        "executed": bool(effects) if executed is None else executed,
        "result": result,
        "effects": list(effects),
    }
    if error:
        value["error"] = error
    return json.dumps(value, ensure_ascii=False)
