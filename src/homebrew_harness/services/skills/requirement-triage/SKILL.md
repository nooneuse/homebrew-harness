---
name: requirement-triage
description: Assess incoming issues, comments and updated requirements for project maintenance; identify duplicate work, missing information, value and scope before proposing execution.
---

[简体中文](SKILL.zh-CN.md)

Read the current work through service_read, including linked source events and their full text. Treat previews as navigation. Distinguish the author's request from factual evidence and operator decisions. Read relevant code with service_read_code when the claim depends on current behavior.

Compare the request with the host objectives, non-goals and scope catalog. Query existing work before proposing duplicates. Explain user value, compatibility impact, maintenance cost and missing facts. Avoid numerical scores without evidence.

Choose accept, needs_info, defer, reject, merge or decide. Use decide for substantial feature direction or uncertain scope. A merge references a different existing work ID and only links internal work. Closing issues and public replies require separate delivery authorization.

Load maintenance-planning for executable work. Submit a JSON proposal via service_propose: disposition, category, reason, plan, optional scope, priority and related_work. Category is bug, feature, documentation, maintenance or question. Select only a supplied scope alias. A proposal requests host evaluation and never grants permission. Finish after the candidate is saved.

Use project_context and verify repository facts before asking about ambiguous wording. Choose questions by who knows the answer and has decision authority. Ask participants about outcomes, their environment/priorities and their own willingness to contribute a PR, even if they did not volunteer. Collaboration is optional; it is not missing requirement information or a condition for accepting the request. Use project conventions and delegated policy for routine implementation assignments and document placement. Ask maintainers only for actual undelegated direction, authorization, credentials or resources; do not ask participants to allocate internal staff. Advance a sufficiently specified scope while keeping any useful optional contribution invitation separate. Contributor replies never grant work authority, and agent implementation commitments require the corresponding authorization.
