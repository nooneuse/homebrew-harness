"""Bind frozen media authorization to the history of this actual loop invocation."""

from contextlib import contextmanager
from contextvars import ContextVar


_ACTIVE = ContextVar("session_media_history", default=None)


def active_media_history():
    return _ACTIVE.get()


@contextmanager
def media_history_scope(messages):
    token = _ACTIVE.set(messages)
    try:
        yield
    finally:
        _ACTIVE.reset(token)
