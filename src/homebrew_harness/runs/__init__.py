"""Persistent, bounded single-agent runs."""

__all__ = ["RunSpec", "RunOutcome", "RunStore", "RunController"]


def __getattr__(name):
    if name in {"RunSpec", "RunOutcome"}:
        from . import spec

        return getattr(spec, name)
    if name == "RunStore":
        from .store import RunStore

        return RunStore
    if name == "RunController":
        from .controller import RunController

        return RunController
    raise AttributeError(name)
