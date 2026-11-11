---
name: pr-feedback
description: Review fixed external PR or MR revisions, assess author explanations, maintain findings across code updates and propose overall, inline or thread feedback. Use for initial reviews, author replies, changed heads and follow-up review decisions.
---
# Pull request feedback

Read the current discussion, ReviewSnapshot, coverage, changed files and prior findings. Review only the code root bound to that snapshot; inspect base files with @base/path when needed. Without a snapshot, discuss intent or ask for context rather than claiming a code review.

Read the change and enough callers, tests and related implementation to substantiate concrete defects. Use code-review guidance. Separate product direction, compatibility, code correctness and test facts. State actual coverage and missing validation; do not report unrun tests as passed.

Keep stable finding IDs and cite their source events. Associate each finding with the exact reviewed version and affected path. An author's explanation can support withdrawing a mistaken finding; a claim of a fix or a resolved platform thread alone does not prove the defect is gone. Recheck on the fixed new version.

When only discussion changed, reuse the snapshot and reassess the explanation. When code changed, inspect the delta plus still-relevant context and previous unresolved findings. Do not copy old line numbers onto new code or treat old CI as current verification.

Propose communication through service_propose. Choose an overall comment for scope or broad reasoning, a thread reply for an observed thread, or submit_review for anchored inline findings. Provide path, line and side from the fixed diff for each inline comment. COMMENT, REQUEST_CHANGES and APPROVE use review_feedback, request_changes and approve_review intents. Approval requires actual coverage and the host's decision; it does not merge or close the change request.

Explain problems with evidence and an actionable next step. Propose wait when author input is needed and operator for disputed direction. Record publication only from a confirmed receipt; an old-version opinion may have been published successfully while already requiring another round.

Review approval is an opinion for the fixed head. It does not authorize a service merge, and a published opinion is separate from actual repository delivery. Continue discussion against new versions where necessary.
