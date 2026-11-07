"""Bounded, body-free storage measurements; process-local per workspace."""

import threading
import time
from collections import OrderedDict, deque
from contextlib import contextmanager

_LOCK = threading.RLock()
_METRICS = OrderedDict()


class StorageMetrics:
    def __init__(self):
        self.lock = threading.RLock()
        self.times = deque(maxlen=512)
        self.counts = {
            "reads": 0,
            "transactions": 0,
            "commits": 0,
            "noops": 0,
            "failures": 0,
            "bytes_read": 0,
            "bytes_written": 0,
            "rows_written": 0,
            "messages_created": 0,
            "messages_examined": 0,
            "component_versions_created": 0,
            "views_created": 0,
            "history_body_reads": 0,
        }

    def add(self, **values):
        with self.lock:
            for key, value in values.items():
                self.counts[key] += value

    @contextmanager
    def transaction(self):
        start = time.monotonic()
        self.add(transactions=1)
        try:
            yield
        except BaseException:
            self.add(failures=1)
            raise
        finally:
            with self.lock:
                self.times.append(time.monotonic() - start)

    def snapshot(self):
        with self.lock:
            values = sorted(self.times)
            return {
                **self.counts,
                "samples": len(values),
                "transaction_seconds_p95": values[
                    min(len(values) - 1, int(len(values) * 0.95))
                ]
                if values
                else 0,
                "transaction_seconds_max": max(values, default=0),
                "scope": "current_process",
                "bytes_kind": "logical_payload",
            }


def metrics_for(root):
    with _LOCK:
        key = str(root)
        metric = _METRICS.setdefault(key, StorageMetrics())
        _METRICS.move_to_end(key)
        while len(_METRICS) > 128:
            _METRICS.popitem(last=False)
        return metric
