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


class ExecutionPending(RuntimeError):
    """Known owned work has not ended; no inspection claim is implied."""
    reason_code = "execution_still_active"


def execution_pending(output):
    try:
        value = json.loads(output) if isinstance(output, str) else output
        return isinstance(value, dict) and value.get("effect_state") == "pending"
    except (TypeError, ValueError):
        return False


def subagent_result(run_id, status, *, result="", error="", effects=(), effect_state="none", cleanup_complete=True, tools_executed=0, evidence_ref=None):
    if status not in {"completed", "incomplete", "failed", "cancelled"} or effect_state not in {"none", "settled", "pending", "uncertain"}:
        raise ValueError("Invalid subagent execution result.")
    value = {"run_id": run_id, "status": status, "effect_state": effect_state,
             "cleanup_complete": cleanup_complete, "executed": tools_executed > 0,
             "tools_executed": tools_executed, "outcome": "unknown" if effect_state == "uncertain" else "pending" if effect_state == "pending" else "completed",
             "result": result, "effects": list(effects), "evidence_ref": evidence_ref}
    if error:
        value["error"] = error
    return value
