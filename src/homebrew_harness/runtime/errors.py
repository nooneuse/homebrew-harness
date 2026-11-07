"""errors for the harness."""

from __future__ import annotations


class AgentError(RuntimeError):
    """A readable error that never includes private connection settings."""
