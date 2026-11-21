---
name: ci-failure-triage
description: Diagnose recorded checks for the exact delivered candidate SHA and propose bounded repair or operator action for CI failures.
---

[简体中文](SKILL.zh-CN.md)

Read the ChangeSet's local quality or remote CI evidence and verify it belongs to the fixed candidate. For local tests, `quality[].outputs` supplies exact IDs for paginated `service_read(kind="quality_output", identity=...)` log access. Inspect candidate code with service_read_code. Distinguish code defects, baseline failures, flaky tests, infrastructure failures and uncertain evidence. A passing focused rerun alone does not prove load-flakiness.

Start with the supplied `work_context` and `validation_history`. Current operator instructions, accepted scope, the completed handoff and prior failed receipts are relevant evidence for this job. A published framework fix may need integration into the frozen candidate; deployment alone does not change that candidate. Use `service_read(kind="changesets", identity=job.target)` for the same evidence projection and `work` for its bound context. Code offsets are character positions: follow `next_offset` and retain `expected_version` when paging. After sufficient evidence, submit the diagnosis and bounded plan rather than reopening unrelated completed work.

Propose a scoped code or conflict-integration repair when the recorded evidence supports it. Preserve the original task, approved file scope, fixed tests and authorization deadline. Do not disable tests, broaden permissions or treat a new Run as a reset of previous failures.

Return classification (code/baseline/flaky/infrastructure/unknown), reason and plan through service_propose. A local flaky/infrastructure diagnosis requests a bounded recheck of the same candidate; explain the evidence and expected recovery. For baseline conflicts, propose an integration plan within the existing scope; a native repair Run receives a read-only source index for the exact target base and proposed head. Compare those versions rather than guessing the clean baseline from crossing conflict markers. Explain missing evidence and remaining uncertainty; request operator action when direction or authority is needed. The proposal does not waive tests or approve delivery.
