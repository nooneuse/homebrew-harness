"""Current observation and analysis intent are distinct from archived events."""

import time
from datetime import datetime
from homebrew_harness.platforms.contracts import digest


def request_analysis(row, *, now=None):
    row.update(
        state="ready_to_assess",
        ready_after=0,
        analysis_epoch=row["analysis_epoch"] + 1,
        revision=row["revision"] + 1,
        retry_after=0,
    )
    return row


def mark_input(row, policy):
    row["retry_after"] = 0
    row["input_revision"] += 1
    row["revision"] += 1
    if row["state"] != "paused":
        row["state"] = "ready_to_assess"
    row["dirty_since"] = row.get("dirty_since") or time.time()
    row["ready_after"] = min(
        time.time() + policy["debounce_seconds"],
        row["dirty_since"] + policy["max_debounce_seconds"],
    )


def observation_event(store, conn, item, identity, *, observed):
    key = digest(
        [
            store.id,
            item.subject.key(),
            item.object.key(),
            bool((item.content.get("metadata") or {}).get("review_basis")),
        ]
    )
    prior = store.db._get(conn, "service_observed_objects", key)
    if prior and prior["identity"] == identity:
        if observed and not prior["observed"]:
            store.db._put(
                conn, "service_observed_objects", key, {**prior, "observed": True}
            )
        return None

    def timestamp(version):
        try:
            return datetime.fromisoformat(version.replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError, AttributeError):
            return None

    if prior:
        old_time, new_time = timestamp(prior.get("version")), timestamp(item.version)
        if old_time is not None and new_time is not None and new_time < old_time:
            return None
    if (
        prior
        and prior["observed"]
        and not observed
        and not (
            timestamp(prior.get("version")) is not None
            and timestamp(item.version) is not None
            and timestamp(item.version) > timestamp(prior["version"])
        )
    ):
        # Source pages are incremental and may replay older content. The full
        # observer owns the current projection once it has seen the object.
        return None
    previous_event = store.db._get(conn, "service_events", identity)
    sequence = (prior or {}).get("sequence", 0) + 1
    store.db._put(
        conn,
        "service_observed_objects",
        key,
        {
            "identity": identity,
            "sequence": sequence,
            "observed": observed or bool(prior and prior["observed"]),
            "version": item.version,
            "service_id": store.id,
            "checkpoint": "service:" + store.id,
        },
    )
    if previous_event:
        return "event_" + digest([key, sequence, identity])[:32]
    return identity


def preparation_applies(row, operation, snapshot):
    return (
        row["state"] != "paused"
        and row["analysis_epoch"] == operation["epoch"]
        and row.get("review_operation") == operation["id"]
        and row.get("observed_basis") == snapshot["basis"] == operation["basis"]
    )
