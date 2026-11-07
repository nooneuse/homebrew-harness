"""configuration for the harness."""

from __future__ import annotations

import threading

CONFIG_VARIABLES = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL")


PRIVATE_ENV_VARIABLES = {*CONFIG_VARIABLES, "DASHSCOPE_API_KEY"}
_PRIVATE_CONNECTION_VALUES = set()
_PRIVATE_ENV_LOCK = threading.Lock()


def register_model_environment(*, names=(), values=()):
    """Remember host model connections without passing them to ambient tools."""
    with _PRIVATE_ENV_LOCK:
        PRIVATE_ENV_VARIABLES.update(names)
        _PRIVATE_CONNECTION_VALUES.update(v for v in values if isinstance(v, str) and v)


def public_environment(environment):
    with _PRIVATE_ENV_LOCK:
        private_names = frozenset(PRIVATE_ENV_VARIABLES)
        private_values = frozenset(_PRIVATE_CONNECTION_VALUES)
    return {
        name: value
        for name, value in environment.items()
        if name not in private_names and value not in private_values
    }
