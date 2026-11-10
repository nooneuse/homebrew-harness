---
name: ci-failure-triage
description: Diagnose recorded checks for the exact delivered candidate SHA and propose bounded repair or operator action for CI failures.
---

[简体中文](SKILL.zh-CN.md)

Read the full ChangeSet CI evidence and verify the reported head matches the candidate. Inspect candidate code with service_read_code. Distinguish code defects, baseline failures, flaky tests, infrastructure failures and uncertain evidence.

Propose a minimal repair only for a confirmed code defect. Preserve the original task, approved file scope, fixed tests and authorization deadline. Do not disable tests, broaden permissions or treat a new Run as a reset of previous failures.

Return classification (code/baseline/flaky/infrastructure/unknown), reason and plan through service_propose. For non-code cases, explain the needed observation or operator action. The proposal does not launch repair or approve delivery.
