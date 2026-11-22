"""Bounded immutable page views for one reasoning job."""

import hashlib
from collections import OrderedDict
from homebrew_harness.runtime.limits import current_limits


class RecordPages:
    def __init__(self):
        self.values = OrderedDict()

    def page(self, raw, offset, limit, expected_version=None):
        version = hashlib.sha256(raw.encode()).hexdigest()
        if expected_version is not None:
            if expected_version in self.values:
                raw = self.values[expected_version]
                version = expected_version
            elif expected_version != version:
                raise ValueError(
                    "Record page version is no longer available; read the current snapshot."
                )
        if len(raw.encode()) <= current_limits().replay_cache_bytes:
            self.values[version] = raw
            self.values.move_to_end(version)
            while (
                len(self.values) > current_limits().replay_cache_entries
                or sum(len(v.encode()) for v in self.values.values())
                > current_limits().replay_cache_bytes
            ):
                self.values.popitem(last=False)
        return {
            "version": version,
            "offset": offset,
            "length": len(raw),
            "text": raw[offset : offset + limit],
            "next_offset": offset + limit if offset + limit < len(raw) else None,
        }
