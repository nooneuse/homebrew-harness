"""Content evidence applicability is distinct from execution and authorization."""

from homebrew_harness.execution.source_snapshot import content_digest, snapshot
from homebrew_harness.execution.test_selection import validate_selection


_COMMON = ("version", "inputs", "backend", "role")
_FAILURES = {"failed", "timeout", "cancelled"}
UNRESOLVED_FULL_FAILURE = "unresolved_full_scope_failure"


def basis(receipt):
    return {
        "version": 1,
        "inputs": content_digest(receipt["inputs"]),
        "contract": receipt["fingerprint"],
        "backend": receipt["execution_backend"],
        "selection": receipt.get("selection"),
        "test_scope": receipt["test_scope"],
        "role": receipt.get("role", "lead"),
    }


def receipt_bucket(row, current, contracts):
    """Classify portable receipts by what they can establish for this request.

    The paged evidence reader can keep just the newest valid row in each
    bucket. In particular, an unrelated failure must not evict a full failure.
    """
    observed = row.get("verification_basis") or {}
    if any(observed.get(key) != current[key] for key in _COMMON):
        return None
    contract = observed.get("contract")
    if contracts is not None and contract not in contracts:
        return None
    isolation = row.get("isolation") or {}
    if not (row.get("executed_at") and row.get("source_stable")
            and isolation.get("cleanup_complete") and isolation.get("os_isolation_verified")
            and not row.get("error")):
        return None
    status = row.get("status")
    if status == "passed" and row.get("exit_code") == 0:
        outcome = "pass"
    elif status in _FAILURES:
        outcome = "failure"
    else:
        return None

    scope, selection = observed.get("test_scope"), observed.get("selection")
    if scope == "full" and selection is None:
        coverage = "full"
    elif scope == "selected" and selection is not None:
        if selection == current["selection"]:
            coverage = "exact_selection"
        elif current["test_scope"] == "full" and outcome == "failure":
            # A newer focused failure can invalidate a broad pass. A focused
            # pass, including one on the same selector, cannot renew it.
            coverage = "related_selection"
        else:
            return None
    else:
        return None
    return contract, coverage, outcome


def applicable(records, runner, name, cwd, *, selection=None):
    recipe = runner._recipe(name)
    if selection is not None:
        selection = validate_selection(selection, recipe.selector, recipe.test_roots)
    current = {
        "version": 1,
        "inputs": content_digest(
            snapshot(cwd, recipe.input_globs, **recipe.snapshot_limits)
        ),
        "contract": runner._fingerprint(recipe),
        "backend": runner.backend.name,
        "selection": selection,
        "test_scope": "selected" if selection is not None else "full",
        "role": "lead",
    }
    if current["backend"] != "container":
        return {"ok": False, "reason": "environment_not_portable", "basis": current}
    contracts = runner.evidence_contracts(recipe) if hasattr(runner, "evidence_contracts") else {current["contract"]: None}
    if callable(records):
        records = records(current, tuple(contracts))
    latest = {}
    for row in records:
        if row.get("recipe") != name:
            continue
        bucket = receipt_bucket(row, current, contracts)
        if bucket is None:
            continue
        _, coverage, outcome = bucket
        previous = latest.get((coverage, outcome))
        if previous is None or row["executed_at"] > previous["executed_at"]:
            latest[coverage, outcome] = row

    def not_cleared(failure, passing):
        return failure is not None and (passing is None or failure["executed_at"] >= passing["executed_at"])

    def evidence(row):
        historical = row["verification_basis"]
        return {"receipt_ref": row["run_id"], "executed_at": row["executed_at"],
                "historical_basis": historical, "compatibility": contracts[historical["contract"]],
                "receipt": row}

    # Only a newer full pass can clear a full failure; it still does not
    # establish an exact selected run (selection can change test discovery).
    full_pass = latest.get(("full", "pass"))
    full_failure = latest.get(("full", "failure"))
    if selection is not None and not_cleared(full_failure, full_pass):
        return {"ok": False, "reason": UNRESOLVED_FULL_FAILURE, "basis": current,
                **evidence(full_failure)}

    coverage = "full" if selection is None else "exact_selection"
    passing = latest.get((coverage, "pass"))
    if passing is None:
        return {"ok": False, "reason": "no_applicable_passing_evidence", "basis": current}
    failures = [latest.get((coverage, "failure"))]
    if selection is None:
        failures.append(latest.get(("related_selection", "failure")))
    failure = max((row for row in failures if row is not None), key=lambda row: row["executed_at"], default=None)
    if not_cleared(failure, passing):
        return {"ok": False, "reason": "newer_failure", "basis": current, **evidence(failure)}
    return {"ok": True, "mode": "reused", "basis": current, **evidence(passing)}
