# Community discussion and external PR review

**English** | [简体中文](COMMUNICATION.zh-CN.md) · [Services](SERVICES.md)

Optional Service communication manages issues and change requests through multiple discussion rounds. It records questions, facts, requirement drafts and review findings, then publishes separately authorized comments, thread replies or formal reviews. Communication can run before development and requires no ChangeSet.

## Host configuration

Add `communication` to a `repository_maintenance` ServiceSpec. The project must already have a registered repository source. Connection declarations reference host environment variables; keep credentials and endpoint values outside the workspaces.

```json
{
  "communication": {
    "mode": "remote",
    "credentials_file": "/path/to/private/communication.env",
    "projects": [{
      "name": "project",
      "provider": "github",
      "instance": "primary",
      "project": "OWNER/PROJECT",
      "api_url_env": "PROJECT_API_ENDPOINT",
      "token_env": "PROJECT_COMMENT_TOKEN"
    }],
    "poll_seconds": 43200,
    "own_pr_poll_seconds": 120,
    "auto_intents": [],
    "review_globs": ["src/**", "tests/**"],
    "required_tests": [],
    "model_profile": null,
    "review_model": null,
    "assessment_model": null,
    "prompt_files": {}
  }
}
```

The private credential file must be owner-only and outside both the control and project directories. Write credentials are separate from the model/development environment. Each publisher is bound to its authenticated platform identity. `local` mode accepts only a synthetic loopback peer for demonstrations.

`auto_intents` defaults to `[]`, so no public action is preauthorized. It can preauthorize `acknowledge`, `clarify` and `followup` within configured projects, and `own_pr_reply` for routine comments and thread replies on the Service's confirmed PRs under current Work authority. Add `review_feedback` to preauthorize advisory feedback. For `submit_review`, this permits only the `COMMENT` event. `APPROVE` and `REQUEST_CHANGES` still require explicit operator decisions, including when independent approval review is enabled. Project direction commitments also require explicit decisions. Submitting a review does not merge or close the PR.

## Following the Service's own PRs

When Delivery confirms PR publication, communication starts tracking that PR immediately. Open PRs belonging to unfinished Work use the shorter of `poll_seconds` and `own_pr_poll_seconds` (120 seconds by default). Ordinary discussions keep their configured interval. Observations do not call the model unless discussion input or an explicit analysis request changes.

The discussion model receives the linked Work, accepted scope, published head and repair status. It may explain existing behavior, ask a specific question, or propose `next_step: "repair"` with `repair: {"plan": "...", "sources": ["event_..."]}`. Repairs preserve the accepted requirements, Work, branch and PR; they use Delivery's existing `auto_ci_fix`, repair-attempt policy and Work authority. A real scope change still uses the requirements-acceptance process.

New feedback or a changed candidate requires reassessment before a pending repair starts. Paused Work and uncertain publication effects defer repair. Once a replacement candidate is published, its receipt wakes the discussion to respond with the actual result. A proposed repair is not proof that a fix has been published.

Service-executed merge admission checks for unassessed feedback, unresolved questions/findings and pending replies, and binds authorization to the handled feedback version. A PR publication or a completed development Run does not complete its Work; delivery still follows the configured merge policy and verifies the actual merged result.

## Repository context and reply preparation

Code-reading jobs receive a bounded `project_context` with a source version, entry-point observations, directory navigation and document excerpts. The agent can retrieve it through `service_read(kind="project_context")` and follow relevant source paths. Excerpts are evidence to investigate, rather than complete descriptions of behavior or verification.

For issues, `assessment_model` selects the requirement assessor; its default is the maintenance Lead. When the assessor and discussion model use different bindings, the Lead prepares facts, question recipients, scope and the next step; `model_profile` drafts the public message without changing that assessment. Matching bindings use a combined pass. External PR review continues to use `review_model`.

Question entries carry `recipient: "requester"` or `"maintainer"` (default: requester). The Lead chooses who knows the answer and who has decision authority. Participants can be proactively asked about their own PR or testing contribution; prior volunteering is not required. Repository facts and routine implementation/document choices within delegated policy are handled by the agent. Actual undelegated direction, authorization, credentials and resource decisions go to maintainers.

Contribution invitations are optional and do not make clear requirements incomplete. A `develop` candidate and acceptance draft can coexist with an open contribution question, which neither accepts requirements nor grants development authority. A contributor's willingness or expected timing is source evidence, and silence is not a refusal. The model can choose to wait or propose follow-up through the maintenance process. Agent implementation commitments require existing work authority. Only open requester IDs may appear in public `communication.question_ids`; drafting cannot invent a question absent from the Lead assessment. Necessary maintainer questions use `operator`. Free-text meaning remains a model judgment; the runtime checks identities and authorization contracts.

Drafting also receives `work_status` from native Work and authority records. A proposed `develop` candidate is a recommendation; it does not mean implementation has been scheduled or authorized. Public replies describe actual progress plainly and keep internal approval machinery out of the body. Routine replies usually need 100–160 words, with more room for substantive explanations.

The Lead can supply a compact `reply_brief` with `scope`, `next_step` and optional `verification`. Drafting receives this outline and open public questions instead of automatically receiving the entire acceptance checklist. Full requirements, internal reasoning and source references remain in the original assessment; accepted Work still uses its complete draft. Saved plans without a brief keep their existing full projection. The existing fields serve distinct audiences: `draft.text` describes proposed work for the maintainer, while `reply_brief` selects the participant's decisive scope and public next step. Requester questions and contribution invitations stay in `public_questions`, so the outline need not repeat them. A proposed `develop` candidate expresses the model's recommendation; formal acceptance and delivery wording follow the recorded Work state.

For acknowledgements, public guidance summarizes the workflow and material differences from the request. Detailed unchanged capability inventories stay in the complete work draft; lists explain actual scope changes or decisions. Code findings are expressed through their consequences, and a contribution invitation uses the already stated target when appropriate.

Public replies briefly confirm decisive scope, distinguish preparatory work from the functional milestone, and state an actionable next step. Verification is labelled for the actual environment and checks. Internal staffing, document placement and infrastructure decisions stay in the maintenance channel; agent execution authorization does not prevent voluntary contributor PRs. Text quality remains a model judgment, separate from permission approval.

The job saves completed assessments before drafting. Retries reuse valid assessment input, source evidence, policy, prompt and model versions. Pure acknowledgements can consume the new event without repeating questions or calling the model; substantive changes require a new assessment. Cached history is optional, and its absence causes fresh assessment. Preparation stages share the existing job deadline and retain the per-stage model-turn limit. Assessment does not grant work or publication authority, and the existing approval workflow remains separate.

Single model requests default to request_seconds=500. maintenance.job_seconds and job_turns default to null; their advisory thresholds are 1500 seconds and 40 calls. Explicit finite budgets remain effective and assessment/drafting share that selected job window. These settings are separate from platform HTTP request and approval-review configuration. See [runtime budgets](LIMITS.md).

## Configurable discussion prompts

Built-in assessment and drafting guidance lives in `services/prompts/discussion-assessment.md` and `discussion-draft.md`. To tune it without editing Python, select external UTF-8 files:

```json
{
  "communication": {
    "prompt_files": {
      "assessment": "/path/to/private/discussion-assessment.md",
      "draft": "/path/to/private/discussion-draft.md"
    }
  }
}
```

Merge this fragment into the full communication declaration. Files must stay outside both workspaces, cannot use symlinks, and are bounded to 16 KiB and 8,000 characters each. New jobs read a fresh snapshot. An existing job, including its retries, retains its original snapshot across assessment and drafting. To apply edited guidance to the current topic, request a new assessment through the existing discussion `assess` control. Changing the selected file paths requires the existing configuration replacement process. Templates cannot alter tool scope, result validation or authorization rules.

Reasoning intensity remains an option of the selected model profile. A dedicated assessment profile can use provider-supported higher effort without applying it to every public reply.

## Discussion rounds

A validated standalone `service_propose` completes its reasoning phase without an additional model acknowledgement. Prepared phase outputs retain input, prompt, model, Lead projection and evidence bindings so restart can reuse them. Ordinary source changes are returned as observations for reassessment; a change outside the actual knowledge basis does not discard the job. Fixed PR and Git candidate checks continue to bind their original versions.

The frozen job includes `no_progress`, the count of rounds reporting no progress. This informs the model; the runtime preserves its chosen next step rather than forcing an operator handoff after a fixed count.

The default discovery interval is twelve hours. A completed source scan or tracked-PR observation marks relevant discussions ready for assessment. No new material means no model calls; waiting for an author does not keep a model request or development Run active. Authorized messages enter the sending queue without waiting for another discovery interval.

The discussion preserves source events, unresolved questions, facts, drafts, findings and each assessment round. New messages update the discussion input, while accepted requirement versions belong to Work. Community replies and the service's own comments do not directly change a Goal or grant development permission.

Service backups include discussion records, publication receipts and materialized PR snapshots. Existing retention operations keep this communication history; they do not retire review snapshots or discussion records.

Once a draft is clear, the service presents a requirement-acceptance request unless existing policy already authorizes it. Acceptance creates or updates Work. Development normally requires a separate authority decision; the opt-in [`maintenance.grant_on_requirements_acceptance`](SERVICES.md#repository-maintenance-p2) grants scoped development authority immediately on acceptance. Active work must settle before accepting replacement requirements. The model can surface direction disputes and repeated lack of progress to the operator.

Acceptance freezes the complete draft, including acceptance criteria and exclusions, with source references and operator clarification. The Run receives these accepted conditions and read-only source inputs. Direction questions and requirement-acceptance decisions remain valid for their discussion version; publication authority has a separate expiry measured from approval.

Each public plan fixes its subject, text, intent and discussion basis. A new answer or code version can supersede an unstarted message or approval. Confirmed publication and current applicability are separate facts. If a write response is lost, inspect the unknown action to reconcile it; the runtime does not blindly resend it.

## External PR input and review

GitHub is the first implemented communication provider. It observes ordinary comments, reviews, inline comments and PR head/base revisions. Snapshots retrieve fixed Git objects through the platform API, verify blob identities and materialize read-only head/base files. The review model reads this snapshot, including base files through `@base/path`.

Coverage records unavailable diffs, unsupported modes, binary/large files and the configured review scope. Approval needs complete recorded coverage and no unresolved findings; `require_tests_for_approval` can additionally require fixed test evidence. Tests, when configured, use the host TestRunner registry. Remote third-party code execution requires its container backend. Static review can operate without Docker and reports that tests were not run.

An author's explanation at the same head reuses the snapshot. New code creates new evidence; existing findings remain queryable across rounds. Resolving a platform thread or claiming a fix does not independently prove a defect was fixed. Inline review positions must belong to the fixed diff.

Coverage accounts for each changed head/base side, including deletions and renames. Unmodified files outside the review scope are optional context. Snapshot files retain read and execute permissions where needed; content and modes are verified before use. Preparation results are applied only to the still-current operation and discussion version, so a late result cannot reopen a paused topic.

## Python and control interface

```python
page = service.discussions()
discussion = service.discussion(discussion_id)
events = service.discussion_events(discussion_id)
operation = service.control_discussion(discussion_id, "prepare_review", request_id="review-1")
record = service.communication_record("review_operations", operation["id"])

plan = service.plan_communication(
    discussion_id,
    {"kind": "post_comment", "intent": "clarify", "body": "Which compatibility behavior is required?"},
    request_id="question-1",
)
requests = service.requests()
# Use the existing single or grouped request decisions to authorize publication.
status = service.communication(plan["id"])
```

`prepare_review` returns a durable operation ID. Publication kinds are `post_comment`, `reply_thread` and `submit_review`; formal events are `COMMENT`, `REQUEST_CHANGES` and `APPROVE`. Read full records in chunks with `communication_record`. Python, remote clients and Text UI use the same authenticated HTTP methods.

```sh
homebrew-harness service discussion SERVICE_ID list
homebrew-harness service discussion SERVICE_ID show DISCUSSION_ID
homebrew-harness service discussion SERVICE_ID assess DISCUSSION_ID --request-id assess-1
homebrew-harness service discussion SERVICE_ID prepare-review DISCUSSION_ID --request-id review-1
homebrew-harness service communication SERVICE_ID list
homebrew-harness service communication SERVICE_ID inspect ACTION_ID --request-id inspect-1
```

The Text UI accepts the same commands without the command prefix or Service ID. `discussion pause` and `discussion reopen` control the selected topic. Discussion and PR-feedback skills propose candidates; authorization, delivery receipts, snapshots, deadlines and recovery remain runtime responsibilities.

## Recovery and request pages

Definite platform rejection marks the action failed and allows a corrected plan. An uncertain write stays `unknown`: reconciliation can confirm it, while explicitly verified non-execution requires its current revision and creates a new authority request.

```python
page = service.requests_page(limit=20)
fault = next(row for row in service.models() if row["failures"])
service.recover_model(fault["profile"], revision=fault["revision"], request_id="recover-1")
```

Model recovery optionally clears recorded connection errors; historical errors do not prevent new requests. Failed jobs are retried separately. Restart the service when connection credentials must be reloaded. Discussion jobs read their frozen topic and associated evidence; internal conversations and reports use their own input contracts.

```sh
homebrew-harness service requests SERVICE_ID --limit 20
homebrew-harness service models SERVICE_ID list
homebrew-harness service models SERVICE_ID recover PROFILE --revision FAULT_REVISION --request-id recover-1
homebrew-harness service communication SERVICE_ID inspect ACTION_ID --conclusion retry_not_executed --confirm-not-executed --revision ACTION_REVISION --request-id inspect-2
```
