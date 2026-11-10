"""Explicit HTTP adapter; all protocol conversion is shared with SDK backends."""

from .adapter import ProtocolAdapter


class NativeAdapter(ProtocolAdapter):
    def __init__(self, protocol, **options):
        options["transport"] = "http"
        super().__init__(protocol, **options)
