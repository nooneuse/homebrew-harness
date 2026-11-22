"""One fixed-content test and original Goal verifier for candidates and merges."""

from pathlib import Path
import hashlib

from homebrew_harness.security.safe_files import SafeFile
from homebrew_harness.security.data_boundary import redact

from homebrew_harness.execution.test_runner import load_test_runner, test_context
from homebrew_harness.orchestration.hard_conditions import HardVerifier


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
    try:
        plan = work.get("validation_plan") or {}
        for name in plan.get("tests", delivery.spec["required_tests"]):
            if delivery.stop.is_set():
                raise RuntimeError("Service stopped during fixed-content verification.")
            from homebrew_harness.services.evidence import receipts as historical
            from homebrew_harness.execution.verification import applicable

            reuse = applicable(
                historical(delivery.runtime, work),
                runner,
                name,
                Path(candidate["workspace"]),
                selection=plan.get("selections", {}).get(name),
            )
            if reuse["ok"] and not work.get("force_test_recheck"):
                reused[name] = reuse
                receipts.append(
                    {
                        **reuse["receipt"],
                        "evidence_use": {
                            "mode": "reused",
                            "receipt_ref": reuse["receipt_ref"],
                            "executed_at": reuse["executed_at"],
                            "checked_basis": reuse["basis"],
                            "candidate_digest": candidate["content_hash"],
                        },
                    }
                )
                continue
            runner.approve(
                name, cwd=Path(candidate["workspace"]), role="lead", max_runs=1
            )
            with test_context("lead"):
                receipt = runner.run(name, selection=(work.get("validation_plan") or {}).get("selections", {}).get(name))
                current = runner.verify(name, Path(candidate["workspace"]), selection=plan.get("selections", {}).get(name))
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
        goal = work["run_spec"].get("goal")
        with test_context("lead"):
            evidence = (
                HardVerifier(
                    candidate["workspace"], test_runner=EvidenceView(runner, reused)
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
    def __init__(self, runner, reused):
        self.runner, self.reused = runner, reused

    def verify(self, name, cwd, **kwargs):
        if name in self.reused and kwargs.get("selection") is None:
            return {
                "ok": True,
                "evidence_use": {
                    k: self.reused[name][k]
                    for k in ("mode", "receipt_ref", "executed_at", "basis")
                },
            }
        return self.runner.verify(name, cwd, **kwargs)

    def __getattr__(self, name):
        return getattr(self.runner, name)
