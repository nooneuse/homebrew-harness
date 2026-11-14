"""Small shared transitions for accepted external effects."""

import time
from homebrew_harness.platforms.effects import failed_state as failed_state


def confirm_unexecuted(action, *, revision, confirmed, decision):
    if (
        action["state"] != "unknown"
        or action["revision"] != revision
        or confirmed is not True
    ):
        raise ValueError("Confirm non-execution of the current unknown attempt.")
    return {
        **action,
        "state": "awaiting_approval",
        "authorization": None,
        "error": None, "error_detail": None, "error_info": None,
        "revision": action["revision"] + 1,
        "inspection": {
            "decision": decision,
            "attempt": action["attempts"],
            "conclusion": "not_executed",
            "at": time.time(),
        },
    }


def start_attempt(action, generation):
    if (
        action["state"] != "queued"
        or not action.get("authorization")
        or action["expires_at"] <= time.time()
    ):
        raise ValueError("Action is not authorized or has expired.")
    return {
        **action,
        "state": "started",
        "attempts": action["attempts"] + 1,
        "generation": generation,
        "revision": action["revision"] + 1,
        "started_at": time.time(),
        "error": None, "error_detail": None, "error_info": None,
    }


def finish_attempt(current, accepted, state, result=None, error=None):
    if state not in {"confirmed", "unknown", "superseded", "failed"}:
        raise ValueError("Invalid external action outcome.")
    if (
        current["state"] not in {"started", "unknown"}
        or current["attempts"] != accepted["attempts"]
        or current.get("generation") != accepted.get("generation")
    ):
        raise ValueError("External action attempt changed.")
    from homebrew_harness.runtime.errors import failure_fields
    diagnostic = failure_fields(error, action_id=current["id"], phase=getattr(error, "details", {}).get("phase", "execute")) if isinstance(error, BaseException) else {"error": error, "error_detail": None, "error_info": None}
    return {
        **current,
        **diagnostic,
        "state": state,
        "result": result,
        "revision": current["revision"] + 1,
        "finished_at": time.time(),
    }


def preflight_failure(current, error):
    from homebrew_harness.runtime.errors import failure_fields
    failures = current.get("preflight_failures", 0) + 1
    return {**current, **failure_fields(error, phase="preflight", action_id=current["id"]),
            "preflight_failures": failures, "next_attempt": time.time() + min(60, 2 ** min(failures, 6)),
            "revision": current["revision"] + 1}
