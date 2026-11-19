"""Content evidence applicability is distinct from execution and authorization."""

from homebrew_harness.execution.source_snapshot import content_digest, snapshot


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


def applicable(records, runner, name, cwd, *, selection=None):
    recipe = runner._recipe(name)
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
    matching = [
        row
        for row in records
        if row.get("recipe") == name
        and row.get("verification_basis") == current
        and row.get("executed_at")
    ]
    good = [
        row
        for row in matching
        if row.get("status") == "passed"
        and row.get("exit_code") == 0
        and row.get("source_stable")
        and row.get("isolation", {}).get("cleanup_complete")
        and row.get("isolation", {}).get("os_isolation_verified")
        and not row.get("error")
    ]
    if not good:
        return {
            "ok": False,
            "reason": "no_applicable_passing_evidence",
            "basis": current,
        }
    selected = max(good, key=lambda row: row["executed_at"])
    # A newer failure against the same watched input/contract cannot be hidden
    # by an older broad pass. Cache misses remain observations, not human verdicts.
    for row in records:
        other = row.get("verification_basis") or {}
        if (
            row.get("recipe") == name
            and row.get("status") in {"failed", "timeout", "cancelled"}
            and row.get("executed_at", 0) > selected["executed_at"]
            and all(
                other.get(key) == current[key]
                for key in ("version", "inputs", "contract", "backend", "role")
            )
        ):
            return {
                "ok": False,
                "reason": "newer_failure",
                "receipt_ref": row["run_id"],
                "basis": current,
            }
    return {
        "ok": True,
        "mode": "reused",
        "receipt_ref": selected["run_id"],
        "executed_at": selected["executed_at"],
        "basis": current,
        "receipt": selected,
    }
