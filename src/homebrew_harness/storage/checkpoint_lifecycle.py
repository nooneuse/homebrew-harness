"""Host-owned session lifecycle helpers, shared by JSON and SQLite commits."""

import copy
import re
import time

from homebrew_harness.runtime.runtime_state import RecoveryRequired

SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")


def session_id(value):
    if not isinstance(value, str) or not SESSION_ID.fullmatch(value):
        raise RecoveryRequired("Invalid checkpoint session identity.")
    return value


def checkpoint_session(key, snapshot):
    if key.startswith(("lead:", "member:", "sub:")):
        selected = key.split(":")[1]
        session_id(selected)
        if snapshot.get("session_id", selected) != selected:
            raise RecoveryRequired(
                "Checkpoint identity does not match its owning session."
            )
        return selected
    return None


def require_retained(meta):
    if meta and meta.get("lifecycle", "retained") != "retained":
        raise RecoveryRequired(
            "Session checkpoint is "
            + str(meta.get("lifecycle"))
            + "; use host checkpoint management before attempting recovery."
        )


def initial_meta(sid, now):
    return {
        "format": 1,
        "session_id": session_id(sid),
        "lifecycle": "retained",
        "pinned": False,
        "created_at": now,
        "last_activity_at": now,
        "closed_at": None,
        "close_complete": False,
        "instance": None,
        "revision": 0,
    }


def started_meta(previous, sid, instance, now=None):
    require_retained(previous)
    now = time.time() if now is None else now
    if previous is not None and previous.get("format") != 1:
        raise RecoveryRequired("Invalid current session lifecycle record.")
    value = copy.deepcopy(previous) if previous is not None else initial_meta(sid, now)
    if previous is not None:
        value["pinned"] = bool(previous.get("pinned", False))
    value.update(
        last_activity_at=now,
        closed_at=None,
        close_complete=False,
        instance=instance,
        revision=value["revision"] + 1,
    )
    return value


def updated_meta(previous, key, snapshot, changed, now=None):
    sid = checkpoint_session(key, snapshot)
    if sid is None:
        return None
    require_retained(previous)
    if previous is not None and not changed:
        return None
    now = time.time() if now is None else now
    if previous is not None and previous.get("format") != 1:
        raise RecoveryRequired("Invalid current session lifecycle record.")
    value = copy.deepcopy(previous) if previous is not None else initial_meta(sid, now)
    if previous is not None:
        value["pinned"] = bool(previous.get("pinned", False))
    value.update(
        last_activity_at=now,
        close_complete=False,
        closed_at=None,
        revision=value["revision"] + 1,
    )
    if snapshot.get("host_instance") is not None:
        value["instance"] = snapshot["host_instance"]
    return value


def finished_meta(previous, instance, complete, now=None):
    require_retained(previous)
    if (
        not previous
        or previous.get("format") != 1
        or previous.get("instance") != instance
    ):
        raise RecoveryRequired(
            "Session closure belongs to a different host generation."
        )
    value = copy.deepcopy(previous)
    now = time.time() if now is None else now
    value.update(
        close_complete=bool(complete),
        closed_at=now if complete else None,
        last_activity_at=now,
        revision=value["revision"] + 1,
    )
    return value
