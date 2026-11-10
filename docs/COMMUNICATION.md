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
    "review_model": null
  }
}
```

The private credential file must be owner-only and outside both the control and project directories. Write credentials are separate from the model/development environment. Each publisher is bound to its authenticated platform identity. `local` mode accepts only a synthetic loopback peer for demonstrations.

`auto_intents` can preauthorize `acknowledge`, `clarify` and `followup` within configured projects. The default requires approval for every public action. Formal reviews and project direction commitments require explicit decisions. A submitted review does not merge or close a PR.

## Discussion rounds

The default discovery interval is twelve hours. A completed source scan or tracked-PR observation marks relevant discussions ready for assessment. No new material means no model calls; waiting for an author does not keep a model request or development Run active. Authorized messages enter the sending queue without waiting for another discovery interval.

The discussion preserves source events, unresolved questions, facts, drafts, findings and each assessment round. New messages update the discussion input, while accepted requirement versions belong to Work. Community replies and the service's own comments do not directly change a Goal or grant development permission.

Service backups include discussion records, publication receipts and materialized PR snapshots. Existing retention operations keep this communication history; they do not retire review snapshots or discussion records.

Once a draft is clear, the service presents a requirement-acceptance request. Acceptance creates or updates a proposed Work, and its development authority follows the existing Service controller. Active work must settle before accepting replacement requirements. Direction disputes and repeated lack of progress are surfaced to the operator.

Each public plan fixes its subject, text, intent and discussion basis. A new answer or code version can supersede an unstarted message or approval. Confirmed publication and current applicability are separate facts. If a write response is lost, inspect the unknown action to reconcile it; the runtime does not blindly resend it.

## External PR input and review

GitHub is the first implemented communication provider. It observes ordinary comments, reviews, inline comments and PR head/base revisions. Snapshots retrieve fixed Git objects through the platform API, verify blob identities and materialize read-only head/base files. The review model reads this snapshot, including base files through `@base/path`.

Coverage records unavailable diffs, unsupported modes, binary/large files and the configured review scope. Approval needs complete recorded coverage and no unresolved findings; `require_tests_for_approval` can additionally require fixed test evidence. Tests, when configured, use the host TestRunner registry. Remote third-party code execution requires its container backend. Static review can operate without Docker and reports that tests were not run.

An author's explanation at the same head reuses the snapshot. New code creates new evidence; existing findings remain queryable across rounds. Resolving a platform thread or claiming a fix does not independently prove a defect was fixed. Inline review positions must belong to the fixed diff.

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
