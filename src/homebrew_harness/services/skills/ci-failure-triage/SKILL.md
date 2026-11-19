---
name: ci-failure-triage
description: Diagnose recorded checks for the exact delivered candidate SHA and propose bounded repair or operator action for CI failures.
---

[简体中文](SKILL.zh-CN.md)

Read the ChangeSet's local quality or remote CI evidence and verify it belongs to the fixed candidate. For local tests, `quality[].outputs` supplies exact IDs for paginated `service_read(kind="quality_output", identity=...)` log access. Inspect candidate code with service_read_code. Distinguish code defects, baseline failures, flaky tests, infrastructure failures and uncertain evidence. A passing focused rerun alone does not prove load-flakiness.

Propose a minimal repair only for a confirmed code defect. Preserve the original task, approved file scope, fixed tests and authorization deadline. Do not disable tests, broaden permissions or treat a new Run as a reset of previous failures.

Return classification (code/baseline/flaky/infrastructure/unknown), reason and plan through service_propose. A local flaky/infrastructure diagnosis requests a bounded recheck of the same candidate; explain the evidence and expected recovery. Baseline/unknown diagnoses explain the required operator action. The proposal does not waive tests or approve delivery.
