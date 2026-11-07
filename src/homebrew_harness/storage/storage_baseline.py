"""Current runtime write-amplification fixture, independent of obsolete backends."""

import tempfile
from pathlib import Path

from homebrew_harness.context.tracked_history import ConversationHistory
from homebrew_harness.storage.durable_runtime import DurableStore
from homebrew_harness.storage.storage_metrics import metrics_for


def run_baseline(records=200, payload_bytes=4096, updates=10):
    if (
        not 1 <= records <= 2000
        or not 1 <= payload_bytes <= 32768
        or not 1 <= updates <= 100
    ):
        raise ValueError("Storage baseline exceeds the bounded fixture limits.")
    results = []
    for count in (records, min(20000, records * 10)):
        with tempfile.TemporaryDirectory(prefix="harness-storage-") as temporary:
            root = Path(temporary).resolve()
            store = DurableStore(root)
            messages = ConversationHistory(
                [
                    {"role": "user", "content": str(index) + "x" * payload_bytes}
                    for index in range(count)
                ]
            )
            value = {"session_id": "baseline", "messages": messages}
            store.checkpoint("lead:baseline", value)
            metric = metrics_for(root)
            before = metric.snapshot()
            for index in range(updates):
                messages.append(
                    {"role": "assistant", "content": "result " + str(index)}
                )
                store.checkpoint("lead:baseline", value)
            after = metric.snapshot()
            results.append(
                {
                    "history_messages": count,
                    **{
                        key: after[key] - before[key]
                        for key in (
                            "bytes_written",
                            "messages_created",
                            "history_body_reads",
                        )
                    },
                }
            )
    return {"bytes_kind": "logical_payload", "updates": updates, "runs": results}
