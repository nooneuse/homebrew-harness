"""Small shared transitions for accepted external effects."""

import time


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
    return {
        **current,
        "state": state,
        "result": result,
        "error": error,
        "revision": current["revision"] + 1,
        "finished_at": time.time(),
    }
