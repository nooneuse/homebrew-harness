# Repository delivery

[简体中文](DELIVERY.zh-CN.md)

Optional Service delivery turns completed development into a fixed Git candidate, local quality checks, a review branch and a PR. The default waits for a project maintainer to merge through the platform. An optional mode lets the service perform a native merge after an operator approves that exact plan. The runtime then verifies the actual merged contents before completing the Work.

## Configuration

Add `delivery` to an external `repository_maintenance` ServiceSpec. Keep connection values, publishing credentials and commit identity outside the project and control directories, in an owner-only credentials file. Declarations contain environment-variable names.

```json
{
  "mode": "isolated",
  "git_executable": "/ABSOLUTE/PATH/TO/GIT",
  "provider": "github",
  "instance": "primary",
  "project": "OWNER/PROJECT",
  "credentials_file": "/path/to/private/publish.env",
  "remote_env": "PROJECT_GIT_REMOTE",
  "api_url_env": "PROJECT_API_ENDPOINT",
  "api_token_env": "PROJECT_WRITE_TOKEN",
  "author_name_env": "PROJECT_AUTHOR_NAME",
  "author_email_env": "PROJECT_AUTHOR_EMAIL",
  "git_user_env": "PROJECT_GIT_USER",
  "git_token_env": "PROJECT_GIT_TOKEN",
  "target_branch": "main",
  "review_prefix": "harness/",
  "required_tests": ["unit-tests"],
  "required_checks": ["CI"],
  "check_app_ids": [11],
  "require_review": true,
  "auto_actions": ["publish_branch", "ensure_change_request"],
  "merge": {"mode": "manual", "poll_seconds": 300},
  "auto_ci_fix": false,
  "ci_fix_limit": 2
}
```

`mode="isolated"` requires the ContainerBackend and a fixed test image. Coding scopes use file tools and registered tests; host Bash, executable MCP, background commands and worktree commands are unavailable in this service. Publishing credentials are separate from model and development credentials. `ca_file_env` selects an external CA file, and `review_model` selects a configured model alias for independent review.

`mode="local"` supports offline demonstrations with a local bare Git remote, a loopback platform fixture and a synthetic token. It can use the native TestRunner. The remote remains outside both mutable directories. This mode does not provide container isolation.

### Merge responsibility

| Mode | Who merges | Service behavior |
| --- | --- | --- |
| `manual`, default | A platform-authorized maintainer | Create and maintain the PR, observe its merge, verify the result; no service merge approval request |
| `service_approved` | The configured service identity | Prepare a fixed native merge plan, obtain operator approval, execute and reconcile it |

For service execution, configure `"merge": {"mode": "service_approved", "method": "merge", "poll_seconds": 300}`. The available GitHub methods are `merge`, `squash` and `rebase`. `method` belongs to service execution and is not accepted in manual mode. The service never directly pushes the target branch.

PR Review Approve and Service merge authorization are separate decisions. `reviewer_ids` and `required_approvals` declare trusted current-head reviews. Requested changes or insufficient approvals keep the PR waiting. Defaults add no required reviewers and do not infer the owner. Configure these values and the platform rules to match the project, including independent approval where required.

Service execution additionally requires native platform merge capability and enforced PR/current-base CI rules without bypass. The adapter checks effective rulesets and legacy protection. A missing or unreadable rule, unavailable method or mandatory merge queue produces a waiting state. GitHub's merge request pins the head SHA; it does not provide an atomic expected-base parameter. The platform's enforced integration rules therefore remain part of the merge contract.

## Fixed candidates and actual results

Before a native Run starts, the service captures its input baseline. After cleanup, delivery applies that Work's approved delta to the target baseline, or to the previous candidate during CI repair. Unrelated existing workspace changes are excluded.

The candidate has a fixed commit, tree, file manifest and target baseline in a private Git repository. Registered tests, original Goal hard conditions and an independent model review examine that content. An optional review `publication={title,body}` provides PR text. Custom model text uses an explicit publication decision; preauthorized PR creation uses the fixed runtime summary. Text cannot implicitly close issues.

```text
completed development → fixed candidate → tests / Goal / review
                      → review branch + PR → CI / Review → merge_ready
                      → maintainer merge OR approved native service merge
                      → merged_verifying → delivered → workspace synchronization
```

`merge_ready` is a legitimate persistent waiting state. It does not consume model calls or expire with the CI execution window. `merged_verifying` records the platform fact while actual verification remains pending. `delivered` confirms that version's integration receipt; Work state `review` means local development completed.

The actual merge commit can differ from the reviewed head. Merge commits are checked against their source/target history and resulting tree; squash checks the accepted delta against its result; rebase uses bounded source history and content replay. A matching message or patch ID alone is insufficient. The result must be reachable in the registered target history, even when the target has advanced further.

Tests and original Goal hard conditions verify a materialized, fixed result. Incomplete content proof or missing CI/review/quality evidence enters `verification_blocked`, retaining the actual merge fact. An old requirement's receipt cannot complete a newer Work revision. No automatic rollback or repeated merge follows a failed verification.

## CI, review and base updates

CI feedback belongs to the exact published head and admitted apps. CI diagnosis distinguishes code, baseline, flaky, infrastructure and unknown failures. Bounded code repair uses native Runs, the original scope, valid Work authority and cumulative attempts; the next candidate updates the same PR and repeats its checks.

A changed PR head or target base enters `integration_required`. Explicit `delivery refresh` can integrate the accepted delta with the current target, or adopt a platform branch update only when its content is the same deterministic integration. It preserves the PR and original conditions and repeats tests/review. New code outside that delta requires a Work decision. A service rebase refresh may replace its review head using an exact-head conditional lease; target-branch writes remain unavailable.

Closing without merge enters `closed_unmerged`. Reopening is observed from the platform. After a successful merge, deletion of the source branch does not remove its recorded SHA. Temporary PR/CI read failures retain the state and retry with backoff; they do not resend a merge or invoke the model repeatedly.

## Operator controls

CLI, Text UI and synchronous/asynchronous Python use the same authenticated HTTP control API:

```sh
homebrew-harness service delivery SERVICE_ID list
homebrew-harness service delivery SERVICE_ID show CHANGESET_ID
homebrew-harness service requests SERVICE_ID
homebrew-harness service batch SERVICE_ID create --ids REQUEST_ID --request-id group-1
homebrew-harness service batch SERVICE_ID decide BATCH_ID --approve REQUEST_ID --request-id decision-1
homebrew-harness service delivery SERVICE_ID inspect ACTION_ID --request-id inspection-1
homebrew-harness service delivery SERVICE_ID refresh CHANGESET_ID --request-id refresh-1
```

Inside Text UI, omit `homebrew-harness service` and `SERVICE_ID`. `delivery prepare` prepares current reviewed Work; normal maintenance does this automatically. `delivery plan CHANGESET_ID merge_change_request` prepares a registered plan in service-approved mode, without granting execution. `delivery request ACTION_ID` renews an unstarted or definitively failed action's decision. Public comments retain their own exact-target/body approval.

```python
change = service.delivery(changeset_id)
plan = service.plan_delivery(changeset_id, "merge_change_request")
# Present the matching request to the operator; their explicit decision executes it.
request = next(r for r in service.requests()
               if r.arguments.get("action_id") == plan["id"])
service.decide(request, "approve", decision_id="merge-decision")
```

`service.delivery()`, `plan_delivery()`, `inspect_delivery()`, `refresh_delivery()`, `request_delivery()`, `batch()` and `decide_batch()` expose the same controls. Large records and full candidate history are available through `service.record("changesets", id, offset=..., limit=...)`.

## Recovery, synchronization and retention

Merge actions use the durable action journal and original request identity. A lost response remains `unknown` until read-only inspection confirms its result. An open PR does not prove non-execution. Only explicit confirmation of non-execution permits a new approval cycle. A maintainer/service race that reaches the same fixed result completes once and records the actual actor.

Pause, stop and revoked authority prevent new service writes. Late facts remain historical and do not resume paused work. Physical owners and leases remain until their operations actually end; ordinary waiting does not hold a test process or repeatedly call a model.

Workspace synchronization uses the verified result, with the original per-file expectations. Conflicts preserve local edits and the independent confirmed remote delivery. They do not trigger another merge. Pending merge/verification/synchronization references protect original and actual evidence from retirement. Finished candidate, refresh-history and integration artifacts use the existing retention process; their receipt identities remain available.

## Demonstrations and platforms

```sh
# Simulated maintainer merge, actual local Git history, no paid model access
python examples/delivery_demo.py --output /tmp/harness-pr-demo
# Explicit operator approval and native service merge
python examples/delivery_demo.py --output /tmp/harness-service-merge-demo --service-merge
```

Use fresh output directories. Both scenarios cover CI repair, one reused PR, restart, merge-result verification, a separately approved comment and resource closure. The platform side of the fixture constructs the real Git merge; product code does not push the target.

GitHub is the implemented adapter. Other platforms implement the same observation, fixed plan and receipt contracts. Native queue/stacked-PR execution, release/tag creation and issue closing remain separate capabilities. A deployment update must settle or inspect old direct-target actions before switching runtimes; their approval is not converted into a native merge authorization. See [Contributing](../CONTRIBUTING.md) for the project's PR-first contribution workflow.
