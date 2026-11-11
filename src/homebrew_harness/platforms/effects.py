"""Platform write outcomes with explicit evidence of non-execution."""

from contextvars import ContextVar
from functools import wraps
from .http import RemoteFailure

_ATTEMPT = ContextVar("platform_effect", default=None)
REJECTED = frozenset({400, 401, 403, 404, 405, 409, 410, 413, 415, 422, 429})


class EffectFailure(ValueError):
    def __init__(self, outcome, *, status=None):
        self.outcome, self.status = outcome, status
        super().__init__(
            "Platform effect " + outcome + (" (" + str(status) + ")" if status else "")
        )


def publication(function):
    @wraps(function)
    def invoke(*args, **kwargs):
        state = {"sent": False}
        token = _ATTEMPT.set(state)
        try:
            return function(*args, **kwargs)
        except EffectFailure:
            raise
        except Exception as error:
            raise EffectFailure(
                "unknown" if state["sent"] else "not_executed"
            ) from error
        finally:
            _ATTEMPT.reset(token)

    return invoke


def tracks_write(function):
    @wraps(function)
    def invoke(self, method, *args, **kwargs):
        state = _ATTEMPT.get()
        write = state is not None and method not in {"GET", "HEAD"}
        if write:
            state["sent"] = True
        try:
            return function(self, method, *args, **kwargs)
        except RemoteFailure as error:
            if write and error.status in REJECTED:
                raise EffectFailure("rejected", status=error.status) from error
            raise

    return invoke


def failed_state(error):
    return (
        "failed"
        if isinstance(error, EffectFailure)
        and error.outcome in {"not_executed", "rejected"}
        else "unknown"
    )
