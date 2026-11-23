"""One fixed-content test and original Goal verifier for candidates and merges."""

from pathlib import Path
import hashlib

from homebrew_harness.security.safe_files import SafeFile
from homebrew_harness.security.data_boundary import redact

from homebrew_harness.execution.test_runner import load_test_runner, test_context
from homebrew_harness.execution.test_selection import validate_selection
from homebrew_harness.execution.verification import UNRESOLVED_FULL_FAILURE, applicable
from homebrew_harness.orchestration.hard_conditions import HardVerifier
from homebrew_harness.services import evidence as work_evidence


def receipt_view(root, receipt):
    """Persist actual test evidence with explicit, immutable output navigation."""
    value = {k: v for k, v in receipt.items() if k not in {
        "inputs", "stdout", "stderr", "stdout_preview", "stderr_preview"
    }}
    outputs = {}
    for stream in ("stdout", "stderr", "stdout_tail", "stderr_tail"):
        path = receipt.get(stream)
        if not path:
            continue
        with SafeFile(Path(root), path) as target:
            raw = target.snapshot()[0]
        if raw is not None:
            outputs[stream] = {
                "id": receipt["run_id"] + ":" + stream,
                "path": path,
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
    value["outputs"] = outputs
    return value


def output_reference(row, identity):
    receipts = [*(row.get("quality") or [])]
    for historical in row.get("validation_history", []):
        if historical.get("candidate_commit") in {None, row["candidate"]["commit"]}:
            receipts.extend(historical.get("quality") or [])
    for receipt in receipts:
        for output in receipt.get("outputs", {}).values():
            if output["id"] == identity:
                return output
    for historical in row.get("candidate_history", []):
        for receipt in historical.get("quality") or []:
            for output in receipt.get("outputs", {}).values():
                if output["id"] == identity:
                    return {**output, "_workspace": historical["workspace"]}
    raise ValueError("Test output is outside this candidate's recorded evidence.")


def read_output(row, identity, offset, limit):
    output = output_reference(row, identity)
    with SafeFile(Path(output.get("_workspace", row["candidate"]["workspace"])), output["path"]) as target:
        raw = target.snapshot(max_bytes=output["bytes"])[0]
    if raw is None or hashlib.sha256(raw).hexdigest() != output["sha256"]:
        raise ValueError("Recorded test output changed or is unavailable.")
    text = redact(raw.decode("utf-8", errors="replace"))
    return {"id": identity, "offset": offset, "length": len(text),
            "text": text[offset:offset + limit],
            "next_offset": offset + limit if offset + limit < len(text) else None}


def historical_receipts(delivery, work, name, basis, contracts):
    yield from work_evidence.receipts(delivery.runtime, work, recipe=name, basis=basis, contracts=contracts)

    # Fixed-candidate tests do not write to a Work Run's receipt pages. Keep
    # their observations when a validation plan changes or a repair is prepared.
    identity = work.get("changeset_id")
    allowed = set(work.get("run_history", [])) | {work.get("run_id")}
    seen = set()
    while identity and identity not in seen:
        seen.add(identity)
        row = delivery.store.get("changesets", identity)
        if row["work_id"] != work["id"]:
            break
        if row["run_id"] in allowed:
            for record in (row, *row.get("validation_history", []), *row.get("candidate_history", [])):
                for receipt in record.get("quality") or []:
                    if not receipt.get("evidence_use"):
                        yield receipt
        identity = row.get("previous")


def evidence_use(candidate, observation, mode):
    value = {
        "mode": mode,
        "receipt_ref": observation["receipt_ref"],
        "executed_at": observation["executed_at"],
        "checked_basis": observation["basis"],
        "candidate_digest": candidate["content_hash"],
        "historical_basis": observation["historical_basis"],
        "compatibility": observation["compatibility"],
    }
    if "reason" in observation:
        value["reason"] = observation["reason"]
    return value


def receipt_reference(observation, use):
    # Historical output paths belong to their original workspaces, not to the
    # current candidate. Their owning validation history remains unchanged.
    return {**{k: v for k, v in observation["receipt"].items() if k != "outputs"},
            "evidence_use": use}


def blocked_by_full_failure(candidate, receipts, observation, *, focused_recheck=None):
    use = evidence_use(candidate, observation, "blocking")
    if focused_recheck is not None:
        use["focused_recheck_ref"] = focused_recheck["run_id"]
    # Use the existing acceptance observation path. There is no new failed
    # execution to report: a forced focused recheck may even have passed.
    return {"ok": False, "quality": [*receipts, receipt_reference(observation, use)],
            "goal_evidence": {"ok": False, "results": [{
                "id": "candidate-full-test-evidence",
                "type": "tests_passed",
                "ok": False,
                "recipe": observation["receipt"]["recipe"],
                "reason": "Focused verification cannot clear an unchanged full-recipe failure. "
                          "Inspect the original receipt and choose full verification or repair.",
                "evidence_use": use,
            }]}}


def check_locked_inputs(delivery, candidate, runner, names):
    # A fresh candidate-side grant must not replace the trusted test definitions.
    from homebrew_harness.execution.source_snapshot import matches

    pinned = delivery.git.tree(candidate["repo"], candidate["target_base"])
    tested = delivery.git.tree(candidate["repo"], candidate["commit"])
    changed = sorted(path for path in pinned.keys() | tested.keys() if pinned.get(path) != tested.get(path))
    return [
        {"id": "protected-tests-" + name, "type": "tests_passed", "ok": False, "recipe": name,
         "reason": "Protected test inputs differ from the fixed target. Restore them or change the host policy explicitly.",
         "paths": locked[:32], "path_count": len(locked)}
        for name in names
        if (locked := [path for path in changed if matches(path, runner.recipes[name].locked_globs)])
    ]


def verify(delivery, candidate, work):
    delivery.git.verify(candidate)
    plan = work.get("validation_plan") or {}
    chosen = plan.get("tests", delivery.spec["required_tests"])
    if not chosen:
        goal = work["run_spec"].get("goal")
        evidence = HardVerifier(candidate["workspace"]).check(goal) if goal else None
        return {"quality": [], "goal_evidence": evidence, "ok": not evidence or evidence["ok"],
                "verification_mode": "no_automated_tests_requested"}
    runner = load_test_runner(
        delivery.runtime.core.config.settings.test_config, Path(candidate["workspace"])
    )
    delivery.tests.own(runner)
    receipts = []
    reused = {}
    cwd = Path(candidate["workspace"])
    force = work.get("force_test_recheck")
    try:
        if getattr(getattr(delivery.runtime, "spec", None), "preset", None) == "self_maintenance":
            from homebrew_harness.services.self_maintenance import verify_test_registry

            verify_test_registry(runner, delivery.runtime.saved_model_bindings["coding_resources"])
            changed = check_locked_inputs(delivery, candidate, runner, chosen)
            if changed:
                return {"ok": False, "quality": [], "goal_evidence": {"ok": False, "results": changed}}
        for name in chosen:
            if delivery.stop.is_set():
                raise RuntimeError("Service stopped during fixed-content verification.")
            reuse = applicable(
                lambda basis, contracts: historical_receipts(delivery, work, name, basis, contracts),
                runner,
                name,
                cwd,
                selection=plan.get("selections", {}).get(name),
            )
            blocked = reuse.get("reason") == UNRESOLVED_FULL_FAILURE
            if not force:
                if blocked:
                    return blocked_by_full_failure(candidate, receipts, reuse)
                if reuse["ok"]:
                    reused[name] = reuse
                    receipts.append(receipt_reference(reuse, evidence_use(candidate, reuse, "reused")))
                    continue
            selection = reuse["basis"]["selection"]
            runner.approve(name, cwd=cwd, role="lead", max_runs=1)
            with test_context("lead"):
                receipt = runner.run(name, selection=selection)
                current = runner.verify(name, cwd, selection=selection)
            receipts.append(receipt_view(candidate["workspace"], receipt))
            if (
                receipt["status"] != "passed"
                or not receipt["source_stable"]
                or not current["ok"]
            ):
                return {"quality": receipts, "goal_evidence": None, "ok": False,
                        "test_failure": {"recipe": name, "run_id": receipt["run_id"],
                            "status": receipt["status"], "source_stable": receipt["source_stable"],
                            "verification": current}}
            if blocked:
                return blocked_by_full_failure(candidate, receipts, reuse, focused_recheck=receipt)
        goal = work["run_spec"].get("goal")
        with test_context("lead"):
            evidence = (
                HardVerifier(
                    candidate["workspace"], test_runner=EvidenceView(runner, reused, cwd)
                ).check(goal)
                if goal
                else None
            )
        delivery.git.verify(candidate)
        return {
            "quality": receipts,
            "goal_evidence": evidence,
            "ok": not evidence or evidence["ok"],
        }
    finally:
        delivery.tests.settle()


class EvidenceView:
    def __init__(self, runner, reused, cwd):
        self.runner, self.reused = runner, reused
        self.cwd = cwd.resolve()

    def verify(self, name, cwd, **kwargs):
        reuse = self.reused.get(name)
        if (reuse and Path(cwd).resolve() == self.cwd
                and not kwargs.get("since") and kwargs.get("task_id") is None):
            selection = kwargs.get("selection")
            if selection is not None:
                recipe = self.runner._recipe(name)
                selection = validate_selection(selection, recipe.selector, recipe.test_roots)
            if selection == reuse["basis"]["selection"]:
                return {
                    "ok": True,
                    "evidence_use": {
                        k: reuse[k] for k in ("mode", "receipt_ref", "executed_at", "basis")
                    },
                }
        return self.runner.verify(name, cwd, **kwargs)

    def __getattr__(self, name):
        return getattr(self.runner, name)
