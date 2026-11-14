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

`auto_intents` can preauthorize `acknowledge`, `clarify` and `followup` within configured projects. The default requires approval for every public action. Formal reviews and project direction commitments require explicit decisions. A submitted review does not merge or close a PR.

## Repository context and reply preparation

Code-reading jobs receive a bounded `project_context` with a source version, entry-point observations, directory navigation and document excerpts. The agent can retrieve it through `service_read(kind="project_context")` and follow relevant source paths. Excerpts are evidence to investigate, rather than complete descriptions of behavior or verification.

For issues, `assessment_model` selects the requirement assessor; its default is the maintenance Lead. When the assessor and discussion model use different bindings, the Lead prepares facts, question recipients, scope and the next step; `model_profile` drafts the public message without changing that assessment. Matching bindings use a combined pass. External PR review continues to use `review_model`.

Question entries carry `recipient: "requester"` or `"maintainer"` (default: requester). The Lead chooses who knows the answer and who has decision authority. Participants can be proactively asked about their own PR or testing contribution; prior volunteering is not required. Repository facts and routine implementation/document choices within delegated policy are handled by the agent. Actual undelegated direction, authorization, credentials and resource decisions go to maintainers.

Contribution invitations are optional and do not make clear requirements incomplete. A `develop` candidate and acceptance draft can coexist with an open contribution question; the host's requirement acceptance and work authorization remain separate. A contributor's willingness or expected timing is source evidence, and silence is not a refusal. The model can choose to wait or propose follow-up through the maintenance process. Agent implementation commitments require existing work authority. Only open requester IDs may appear in public `communication.question_ids`; drafting cannot invent a question absent from the Lead assessment. Necessary maintainer questions use `operator`. Free-text meaning remains a model judgment; the runtime checks identities and authorization contracts.

The Lead can supply a compact `reply_brief` with `scope`, `next_step` and optional `verification`. Drafting receives this outline and open public questions instead of automatically receiving the entire acceptance checklist. Full requirements, internal reasoning and source references remain in the original assessment; accepted Work still uses its complete draft. Saved plans without a brief keep their existing full projection.

Public replies briefly confirm decisive scope, distinguish preparatory work from the functional milestone, and state an actionable next step. Verification is labelled for the actual environment and checks. Internal staffing, document placement and infrastructure decisions stay in the maintenance channel; agent execution authorization does not prevent voluntary contributor PRs. Text quality remains a model judgment, separate from permission approval.

The job saves completed assessments before drafting. Retries reuse valid assessment input, source evidence, policy, prompt and model versions. Pure acknowledgements can consume the new event without repeating questions or calling the model; substantive changes require a new assessment. Cached history is optional, and its absence causes fresh assessment. Preparation stages share the existing job deadline and retain the per-stage model-turn limit. Assessment does not grant work or publication authority, and the existing approval workflow remains separate.

For long issue assessments, configure `request_seconds: 500` in the host settings and `maintenance.job_seconds: 1500` in the Service specification. The request limit is passed explicitly to the model transport. Assessment and drafting share the 1,500-second job deadline; each request receives at most 500 seconds or the remaining job time. These settings are separate from platform HTTP requests and approval-review deadlines.

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

Once a draft is clear, the service presents a requirement-acceptance request. Acceptance creates or updates a proposed Work, and its development authority follows the existing Service controller. Active work must settle before accepting replacement requirements. The model can surface direction disputes and repeated lack of progress to the operator.

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
