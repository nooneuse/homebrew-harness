"""Bounded model-error observations reuse the owning Run and Agent Loop."""

import json
import time
import uuid

from homebrew_harness.execution.model_protocol import ProviderError, ModelOutputError
from homebrew_harness.context.tracked_history import set_context
from homebrew_harness.runtime.errors import error_info
from homebrew_harness.security.data_boundary import redact
from homebrew_harness.tools.tool_outcome import needs_inspection
from .spec import RunStopped

NAME = "model_error_observation"


def recoverable(error):
    if isinstance(error, ProviderError):
        code = (error.code or "").lower()
        return error.kind == "invalid_request" and code in {"datainspectionfailed", "data_inspection_failed"} and (error.message or "").lower().startswith("output data")
    if isinstance(error, ModelOutputError):
        details = getattr(error, "details", {})
        return not details.get("limit_name") and details.get("finish") not in {"incomplete", "refused"}
    return False


def context(control, messages):
    feedback = control.state.get("model_feedback") or {}
    latest = feedback.get("latest") or {}
    if not latest or latest.get("resolved"):
        set_context(messages, NAME, None)
        return
    value = {"error": latest, "remaining_feedback_attempts": None if control.spec.retries is None else max(0, control.spec.retries - feedback["consecutive"]),
             "business_actions_from_failed_model_request": 0,
             "instruction": "The previous model request failed before any new tool actions were accepted. This is an observation, not task completion or permission. Keep the original task and completed tool facts. Choose whether to continue with normal tools, adjust the attempted output, or call report_run_blocker with the obstacle and required next action. Do not repeat already completed or unverified side effects. This failure does not prove the model is permanently unavailable."}
    set_context(messages, NAME, {"role": "user", "name": NAME, "content": json.dumps(value, ensure_ascii=False)})


def observe(control, error, messages):
    if "report_run_blocker" not in control.spec.effective_tools() or not recoverable(error):
        return False
    control.safe_point()
    control.session._refresh_operation_recovery()
    if control.session._recovery_blocked or any(needs_inspection(m.get("content", "{}")) for m in messages if m.get("role") == "tool"):
        return False
    previous = control.state.get("model_feedback") or {}
    count = previous.get("consecutive", 0) + 1
    latest = {"id": "model_error_" + uuid.uuid4().hex, "at": time.time(), "attempt": count,
              "error_info": error_info(error), "resolved": False}
    control.state = control.store.update(control.header, {"model_feedback": {"consecutive": count, "latest": latest}})
    if control.spec.retries is not None and count > control.spec.retries:
        raise RunStopped("model_error_recovery_exhausted") from error
    context(control, messages)
    return True


def acknowledged(control):
    feedback = control.state.get("model_feedback") or {}
    if not feedback.get("latest") or feedback["latest"].get("resolved"):
        return
    control.state = control.store.update(control.header, {"model_feedback": {"consecutive": 0,
        "latest": {**feedback["latest"], "resolved": True, "resolved_at": time.time()}}})


def block(control, *, workdir, reason, required_action=""):
    from homebrew_harness.runtime.limits import current_limits
    limits = current_limits()
    if (not isinstance(reason, str) or not reason.strip()
            or limits.blocker_reason_chars is not None and len(reason) > limits.blocker_reason_chars
            or not isinstance(required_action, str)
            or limits.blocker_action_chars is not None and len(required_action) > limits.blocker_action_chars):
        raise ValueError("A blocker requires a nonempty reason and text next action within configured limits.")
    control.safe_point()
    latest = (control.state.get("model_feedback") or {}).get("latest") or {}
    blocker = redact({"reason": reason, "required_action": required_action, "error_ref": latest.get("id"), "at": time.time()})
    control.state = control.store.update(control.header, {"model_blocker": blocker})
    control.permission_required, control.permission_reason = "report_run_blocker", "model_blocked"
    return json.dumps({"accepted": True, "run_state": "blocked", "permission_granted": False, "blocker": blocker}, ensure_ascii=False)
