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
    for stream in ("stdout", "stderr"):
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
    for receipt in row.get("quality") or []:
        for output in receipt.get("outputs", {}).values():
            if output["id"] == identity:
                return output
    raise ValueError("Test output is outside this candidate's recorded evidence.")


def read_output(row, identity, offset, limit):
    output = output_reference(row, identity)
    with SafeFile(Path(row["candidate"]["workspace"]), output["path"]) as target:
        raw = target.snapshot(max_bytes=output["bytes"])[0]
    if raw is None or hashlib.sha256(raw).hexdigest() != output["sha256"]:
        raise ValueError("Recorded test output changed or is unavailable.")
    text = redact(raw.decode("utf-8", errors="replace"))
    return {"id": identity, "offset": offset, "length": len(text),
            "text": text[offset:offset + limit],
            "next_offset": offset + limit if offset + limit < len(text) else None}


def verify(delivery, candidate, work):
    delivery.git.verify(candidate)
    runner = load_test_runner(
        delivery.runtime.core.config.settings.test_config, Path(candidate["workspace"])
    )
    delivery.tests.own(runner)
    receipts = []
    reused = {}
    try:
        for name in delivery.spec["required_tests"]:
            if delivery.stop.is_set():
                raise RuntimeError("Service stopped during fixed-content verification.")
            from homebrew_harness.services.evidence import receipts as historical
            from homebrew_harness.execution.verification import applicable

            reuse = applicable(
                historical(delivery.runtime, work),
                runner,
                name,
                Path(candidate["workspace"]),
            )
            if reuse["ok"]:
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
                receipt = runner.run(name)
                current = runner.verify(name, Path(candidate["workspace"]))
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
