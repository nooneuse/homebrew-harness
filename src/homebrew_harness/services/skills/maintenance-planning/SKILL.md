---
name: maintenance-planning
description: Propose bounded repository work, verification and roadmap decisions during requirement triage or operator discussion using the host scope catalog.
---

[简体中文](SKILL.zh-CN.md)

Read the original request, its amendments and relevant project code. Preserve requested outcomes and compatibility constraints in the plan. Separate the current local implementation goal from later publication.

Select one host scope alias whose paths, tools and tests suffice. Describe concrete changes, verification commands or registered recipes, and observable acceptance criteria. If the catalog cannot support the request, ask for a host decision; do not invent a connection, program, model alias or broader permission.

During operator discussion, read history and current work before making commitments. Explain tradeoffs and unresolved choices. Return text and, only for an explicitly targeted current work, an optional work_id and triage-shaped plan. Scope approval, pause and cancellation remain explicit host controls. Ordinary words such as yes or approved do not invoke them.

Submit the candidate with service_propose. The host validates current versions and policy before accepting work. An execution entering review means local delegated work completed; publication remains a separate outcome.
