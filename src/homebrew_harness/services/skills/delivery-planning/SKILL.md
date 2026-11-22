---
name: delivery-planning
description: Explain fixed candidate publication plans, change-request descriptions and per-item approval scope for repository delivery.
---

[简体中文](SKILL.zh-CN.md)

Read the current ChangeSet, candidate, quality evidence and delivery actions with service_read. Explain the exact work version, code commit/tree, review branch, target baseline and outstanding decisions.

Distinguish local review readiness from current remote CI/merge readiness. Request only host-registered targets and operations. Keep identity, credentials, programs and remote connection configuration under host control.

Write change-request summaries from confirmed changes and tests. Use related references; do not introduce implicit issue-closing keywords. Explain unresolved warnings and base changes before suggesting delivery.

Group related pending requests for presentation, retaining each exact reference and version. Each exact item uses the configured owner policy or delegated auto-review; do not request a second manual decision for already delegated authority. Text such as approved or yes is not a native approval. Describe unknown external results as unknown and request inspection rather than a new write.

Explain PR-first delivery. In manual mode wait for the platform maintainer; in service-approved mode propose only the registered fixed-head plan. PR Review Approve and service merge authority are separate. Report actual merge commit/content proof, pending verification and synchronization from receipts. Do not suggest a direct target push. Optional publication text uses its exact configured approval path; do not publish internal notes.
