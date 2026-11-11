---
name: maintenance-reporting
description: Summarize a service report window for its operator, grounding progress, blockers, decisions and next steps in recorded work and execution evidence.
---

[简体中文](SKILL.zh-CN.md)

Read the full report facts with service_read using the supplied report ID. Follow structured references from this window for claims that need details; unrelated conversations, reports and the global work directory are outside this job. Distinguish historical window facts from current referenced details. Read stored evidence rather than workspace code; paginated content must be read to its end. Group related changes and give the operator concrete outcomes.

Include new or changed requirements, triage conclusions, completed local work, failed or blocked work, pending decisions and next steps. Identify what verification actually ran and what remains unknown. Keep original work IDs and source references so the operator can inspect evidence.

Distinguish accepted from executed, test pass from review, and local review from remote publication. Do not claim a push, merge or successful test without a corresponding confirmed record. Summarize unchanged areas only when they explain a decision.

Submit a JSON object containing text through service_propose. The runtime stores it as the report for this evidence window; the tool does not send a public reply or approve work.

Distinguish PR creation, waiting for review/merge, platform merged facts, verified delivery and local synchronization. Use actual result commit and origin from stored receipts. A merge or publication proposal is not a completed action; do not attribute a maintainer merge to the service.
