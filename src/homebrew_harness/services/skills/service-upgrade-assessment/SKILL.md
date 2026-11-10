---
name: service-upgrade-assessment
description: Assess a tested Service runtime candidate, its compatibility, parked resources, version handoff and recorded rollback boundaries before an operator upgrade decision.
---

[简体中文](SKILL.zh-CN.md)

Read the complete upgrade plan and relevant health, test and operation evidence with service_read. Bind statements to the candidate runtime fingerprint, installed package hash, old binding and original service identity.

Check that verified source and installed package match, that test evidence is fresh, and that writers are actually parked. Review compatibility with the recorded execution and state contracts; publication of repository code is separate from changing the running service.

Explain the exact handoff, probation and confirmation steps and the evidence required to resume business work. A candidate needs operator authorization even when tests pass.

Describe rollback using the recorded old version and current accepted facts. New external effects or changed business state require inspection; restoring an older database must not discard them.

Submit an evidence-based text assessment through service_propose. Do not approve, install, arm or activate the candidate.
