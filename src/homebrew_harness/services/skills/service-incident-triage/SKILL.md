---
name: service-incident-triage
description: Diagnose recorded Service failures or stalled progress, explain current waiting states, and propose bounded recovery from concrete health and execution evidence.
---

[简体中文](SKILL.zh-CN.md)

Read the incident and current health with service_read. Inspect the related source, operation, work or native execution facts before attributing a failure.

Distinguish expected operator waiting from stopped workers, source failures, storage pressure and unresolved external results. An accessible HTTP endpoint does not prove business readiness or writer cleanup.

Propose the smallest registered recovery action and describe its prerequisite evidence. Preserve original requirements, execution ownership, cumulative attempts and unknown-effect records. A missing acknowledgment cannot prove non-execution.

Submit text through service_propose with the observed cause, uncertainty, required operator action and a concrete verification step. The assessment does not grant permissions, delete history or restart work.
