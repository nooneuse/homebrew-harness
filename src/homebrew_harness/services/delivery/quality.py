"""One fixed-content test and original Goal verifier for candidates and merges."""

from pathlib import Path

from homebrew_harness.execution.test_runner import load_test_runner, test_context
from homebrew_harness.orchestration.hard_conditions import HardVerifier


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
            if (
                receipt["status"] != "passed"
                or not receipt["source_stable"]
                or not current["ok"]
            ):
                raise ValueError(
                    "Fixed-content test did not produce fresh passing evidence."
                )
            receipts.append(
                {
                    k: v
                    for k, v in receipt.items()
                    if k
                    not in {
                        "stdout",
                        "stderr",
                        "stdout_preview",
                        "stderr_preview",
                        "inputs",
                    }
                }
            )
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
