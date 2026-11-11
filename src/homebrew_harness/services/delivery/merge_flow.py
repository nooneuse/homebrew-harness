"""PR observation, semantic merge gates and actual integration progression."""

import time

from homebrew_harness.platforms.merges import receipt
from homebrew_harness.services.protocol import fingerprint
from .review_gate import check as review_check

OBSERVING = frozenset(
    {
        "ci_waiting",
        "review_waiting",
        "merge_ready",
        "merge_pending",
        "integration_required",
        "closed_unmerged",
        "verification_blocked",
        "superseded",
        "blocked",
    }
)
PASSIVE = frozenset(
    {
        "validating",
        "reviewing",
        "review_branch_ready",
        "ci_diagnosing",
        "repairing",
        "repair_waiting",
        "repair_waiting_authority",
        "quality_failed",
    }
)


def gate(feedback, spec):
    latest = {}
    for row in feedback["checks"]:
        if (
            row["head"] != feedback["head"]
            or row["app_id"] not in spec["check_app_ids"]
            or row["name"] not in spec["required_checks"]
        ):
            continue
        key = row["name"], row["app_id"]
        version = int(row["id"]) if str(row["id"]).isdecimal() else str(row["id"])
        old = latest.get(key)
        if old is None or str(version).zfill(30) > str(old[0]).zfill(30):
            latest[key] = version, row
    checks = [item[1] for item in latest.values()]
    failed = any(
        c["status"] == "completed" and c["conclusion"] != "success" for c in checks
    )
    ci_ready = set(spec["required_checks"]) <= {c["name"] for c in checks} and all(
        c["status"] == "completed" and c["conclusion"] == "success" for c in checks
    )
    reviews = review_check(feedback, spec)
    basis = {
        "head": feedback["head"],
        "base": feedback["base"],
        "source_project": feedback["source_project"],
        "source_branch": feedback["source_branch"],
        "base_project": feedback["base_project"],
        "base_branch": feedback["base_branch"],
        "state": feedback["state"],
        "draft": feedback["draft"],
        "merged": feedback["merged"],
        "checks": sorted(
            (c["name"], c["app_id"], c["status"], c["conclusion"]) for c in checks
        ),
        "reviews": reviews,
    }
    return {
        "ci_ready": ci_ready,
        "failed": failed,
        "review_gate": reviews,
        "hash": fingerprint(basis),
    }


def observe(delivery, row):
    if not row.get("change_request") or row.get("next_merge", 0) > time.time():
        return False
    try:
        observed = delivery.adapter.metadata(row["change_request"])
    except Exception as error:
        from homebrew_harness.execution.execution_control import cleanup_scope

        if not delivery.stop.is_set():
            with cleanup_scope():
                delivery.store.read_failure(row["id"], "pr", error)
        return True
    delay = delivery.spec["merge"]["poll_seconds"]
    if row["state"] in PASSIVE and not observed["merged"]:
        delivery.store.observation(row["id"], observed, time.time() + delay)
        return False
    if (
        observed["head"] != row["candidate"]["commit"]
        or observed["source_branch"] != row["branch"]
        or observed["base_branch"] != delivery.spec["target_branch"]
    ):
        work = delivery.runtime.store.work(row["work_id"], delivery.ms.id)
        latest_id = work.get("changeset_id")
        if latest_id and latest_id != row["id"]:
            latest = delivery.store.get("changesets", latest_id)
            if (
                latest.get("change_request") == row["change_request"]
                and latest.get("candidate", {}).get("commit") == observed["head"]
            ):
                delivery.store.update(
                    "changesets",
                    row["id"],
                    state="superseded",
                    observation_terminal=True,
                    observed=observed,
                )
                return True
        delivery.store.update(
            "changesets",
            row["id"],
            state="verification_blocked"
            if observed["merged"]
            else "integration_required",
            observed=observed,
            next_merge=time.time() + delay,
            error="merged_unaccepted_version"
            if observed["merged"]
            else "pr_version_changed",
            foreign_merge=receipt(row["change_request"], observed, observed["head"])
            if observed["merged"]
            else None,
        )
        delivery.store.invalidate_merges(row["id"])
        return True
    if observed["merged"]:
        value = receipt(row["change_request"], observed, row["candidate"]["commit"])
        delivery.store.record_merge(row["id"], value)
        return True
    if observed["state"] == "closed":
        delivery.store.update(
            "changesets",
            row["id"],
            state="closed_unmerged",
            observed=observed,
            next_merge=time.time() + delay,
        )
        delivery.store.invalidate_merges(row["id"])
        return True
    if row["state"] == "closed_unmerged":
        delivery.store.update(
            "changesets",
            row["id"],
            state="ci_waiting",
            next_ci=0,
            ci_started=time.time(),
            next_merge=0,
        )
        return True
    delivery.store.observation(row["id"], observed, time.time() + delay)
    if (
        row["state"] == "merge_ready"
        and observed["base"] != row["candidate"]["target_base"]
    ):
        delivery.store.update(
            "changesets",
            row["id"],
            state="integration_required",
            error="target_base_changed",
        )
        delivery.store.invalidate_merges(row["id"])
    return False


def verify_result(delivery, row):
    from .integration import prove
    from .quality import verify

    if row.get("next_merge", 0) > time.time():
        return
    value = row["merge"]
    identity = fingerprint([row["id"], value["head"], value["result_commit"]])
    directory = (
        delivery.runtime.store.root / ".harness/delivery/integrations" / identity
    )
    observation = {**value, "merged": True, "method": value.get("method")}
    from homebrew_harness.execution.execution_control import cleanup_scope

    with delivery.runtime.lock:
        delivery.preparing = True
    try:
        try:
            feedback = delivery.adapter.feedback(row["change_request"])
        except Exception as error:
            with cleanup_scope():
                delivery.store.read_failure(row["id"], "feedback", error)
            return
        delivery.store.clear_read_failure(row["id"], "feedback")
        delivery.store.clear_read_failure(row["id"], "pr")
        evaluation = gate(feedback, delivery.spec)
        if (
            feedback["head"] != value["head"]
            or feedback["result_commit"] != value["result_commit"]
            or not evaluation["ci_ready"]
            or not evaluation["review_gate"]["ready"]
        ):
            raise ValueError(
                "Merged PR lacks the required candidate CI or review evidence."
            )
        if delivery.spec["require_review"] and (
            not row.get("review")
            or any(f["severity"] == "blocking" for f in row["review"]["findings"])
        ):
            raise ValueError("Merged candidate lacks a passing independent review.")
        actual = prove(delivery.git, row["candidate"], observation, directory)
        work = delivery.runtime.store.work(row["work_id"], delivery.ms.id)
        # Verify the requirements accepted for this ChangeSet, including after
        # a later Work revision has made this delivery historical.
        work = {**work, "run_spec": {**work["run_spec"], "goal": row.get("goal")}}
        checked = verify(delivery, actual, work)
        if not checked["ok"]:
            raise ValueError("Merged result failed the original Goal.")
        delivery.store.complete_integration(row["id"], actual, checked)
    except Exception as error:
        if delivery.stop.is_set():
            return
        with cleanup_scope():
            delivery.store.update(
                "changesets",
                row["id"],
                state="verification_blocked",
                error=type(error).__name__,
                next_merge=time.time() + delivery.spec["merge"]["poll_seconds"],
            )
    finally:
        with cleanup_scope(), delivery.runtime.lock:
            delivery.preparing = False


def refresh(delivery, identity):
    from homebrew_harness.execution.execution_control import (
        cleanup_scope,
        control_scope,
    )
    from homebrew_harness.execution.model_gateway import Deadline
    from .integration import refresh_candidate

    row = delivery.store.get("changesets", identity)
    if row["state"] == "verification_blocked" and row.get("merge"):
        return delivery.store.update(
            "changesets", identity, state="merged_verifying", error=None, next_merge=0
        )
    if row["state"] not in {
        "integration_required",
        "ci_waiting",
        "review_waiting",
        "merge_ready",
        "blocked",
        "closed_unmerged",
    }:
        raise ValueError("Refresh a waiting PR or a blocked integration verification.")
    delivery.store.current(row)
    with delivery.runtime.lock:
        if delivery.preparing:
            raise ValueError("Delivery already owns a slow preparation.")
        delivery.preparing = True
    try:
        with control_scope(
            Deadline.after(delivery.spec["gate_seconds"]),
            cancel_events=(delivery.stop,),
        ):
            observed = delivery.adapter.metadata(row["change_request"])
            if observed["merged"]:
                return delivery.store.record_merge(
                    identity,
                    receipt(
                        row["change_request"], observed, row["candidate"]["commit"]
                    ),
                )
            if (
                observed["state"] != "open"
                or observed["source_branch"] != row["branch"]
                or observed["base_branch"] != delivery.spec["target_branch"]
            ):
                raise ValueError(
                    "Reopen or restore the registered PR before refreshing."
                )
            if (
                observed["head"] == row["candidate"]["commit"]
                and observed["base"] == row["candidate"]["target_base"]
            ):
                return delivery.store.update(
                    "changesets",
                    identity,
                    state="ci_waiting",
                    next_ci=0,
                    next_merge=0,
                    ci_started=time.time(),
                )
            attempts = row["attempts"] + 1
            directory = (
                delivery.runtime.store.root
                / ".harness/delivery/candidates"
                / row["id"]
                / str(attempts)
            )
            candidate, published = refresh_candidate(
                delivery.git,
                row["candidate"],
                observed,
                directory,
                linear=delivery.spec["merge"].get("method") == "rebase",
            )
            with delivery.ms.db.write() as conn:
                delivery.runtime.store.guard(conn, delivery.runtime.header)
                current = delivery.store.get("changesets", identity, conn)
                delivery.store.current(current, conn)
                if current["frozen"] != row["frozen"] or current.get("merge"):
                    raise ValueError(
                        "Delivery changed while refreshing the fixed candidate."
                    )
                history = {
                    k: row["candidate"][k]
                    for k in (
                        "commit",
                        "tree",
                        "target_base",
                        "content_hash",
                        "repo",
                        "workspace",
                    )
                }
                history["attempt"] = row["attempts"]
                current.update(
                    candidate=candidate,
                    candidate_history=[*current.get("candidate_history", []), history],
                    attempts=attempts,
                    state="validating",
                    quality=None,
                    review=None,
                    frozen=None,
                    revision=current["revision"] + 1,
                    publish_base=published,
                    review_commit=None,
                    next_merge=0,
                    next_ci=0,
                    error=None,
                )
                delivery.store.put(conn, "changesets", current)
            delivery.store.invalidate_merges(identity)
            return current
    finally:
        with cleanup_scope(), delivery.runtime.lock:
            delivery.preparing = False
