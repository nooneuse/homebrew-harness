"""Optional persistent services composed from the existing execution API."""


def __getattr__(name):
    if name in {"ServiceSpec", "ScheduleSpec"}:
        from . import spec

        return getattr(spec, name)
    raise AttributeError(name)


__all__ = ["ServiceSpec", "ScheduleSpec"]
