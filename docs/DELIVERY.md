# Repository delivery

[简体中文](DELIVERY.zh-CN.md)

Optional Service delivery connects reviewed work to a fixed Git candidate, local quality checks, a review branch, a change request, remote CI and an operator-approved target update. It reuses native Runs, TestRunner, Goals, the model catalog and the Service control API.

## Configuration

Add `delivery` to an external `repository_maintenance` ServiceSpec. Set connection values and commit identity in a separate owner-only credential file outside the project and control directory.

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
  "auto_ci_fix": false,
  "ci_fix_limit": 2
}
```

The host test registry must contain the fixed recipes. Isolated delivery requires the ContainerBackend and an immutable test image. Coding scopes can use file tools and registered tests; they cannot enable host Bash, executable MCP, background commands or worktree commands in this service. Publishing credentials must be separate from model and development credentials. `ca_file_env` optionally selects an external CA file. `review_model` optionally selects a configured model alias for independent review.

`request_seconds` bounds each external operation; `gate_seconds` bounds a delivery progression step. Authorization, CI polling and total CI waiting have separate durations. Fees remain provider-managed.

`mode="local"` supports offline demonstrations with a local bare Git remote, loopback platform fixture and synthetic token. It can use the native TestRunner. A local remote resides outside both mutable directories. This mode verifies the delivery protocol; it does not provide container isolation.

## Candidate and quality

Before a native Run starts, the service captures its input baseline. After Run cleanup, delivery applies that work's approved file delta to the target baseline, or to the previous candidate during CI repair. Existing unrelated workspace changes are excluded. Conflicting input or target versions require a new work/candidate decision.

The candidate has a fixed commit, tree, file manifest and target baseline in a private Git repository. Its materialized workspace is separate from development. Fixed tests run against that workspace, original Goal hard conditions are rechecked there, and an independent model context reads the same candidate. File and test conditions must describe the candidate's delivered contents; passing checks in another workspace cannot substitute for this evidence.

The main progression is:

```text
reviewed work → preparing → validating → reviewing → review_branch_ready
             → review branch + change request → ci_waiting → merge_ready
             → operator target approval → delivered
```

The first review-branch publication follows local tests and review. `merge_ready` additionally requires the exact published SHA, every configured check from an admitted app and the expected target baseline. The target action rechecks CI and updates the branch using an ancestry-checked conditional Git push. Credentialed deployments with required CI also need enforced target-branch protection on the platform.

`work.snapshot().state == "review"` describes completed development. `delivered` confirms the target action. Change-request state and public-comment receipts remain separate facts.

## CI continuation

CI polling belongs to the Service and survives the coding Run and supervisor restarts. Failed current-candidate checks create a diagnosis job. The `ci-failure-triage` skill distinguishes code, baseline, flaky, infrastructure and unknown failures.

Code repair needs operator approval or host `auto_ci_fix` policy, current original work authority and remaining attempts. It creates a native Run in a separate repair workspace, preserves the approved paths and original Goal, and records cumulative attempts. Its next candidate advances the same review branch and updates the original change request. Every new candidate repeats local verification, review and CI. Other failure classes remain blocked until an explicit observation or work decision.

## Operator controls

CLI, Text UI and Python use the authenticated HTTP control protocol:

```sh
homebrew-harness service delivery SERVICE_ID list
homebrew-harness service delivery SERVICE_ID show CHANGESET_ID
homebrew-harness service delivery SERVICE_ID action ACTION_ID
homebrew-harness service requests SERVICE_ID
homebrew-harness service batch SERVICE_ID create --ids REQUEST_ID --request-id group-1
homebrew-harness service batch SERVICE_ID decide BATCH_ID --approve REQUEST_ID --request-id decision-1
homebrew-harness service delivery SERVICE_ID inspect ACTION_ID --request-id inspection-1
homebrew-harness service delivery SERVICE_ID refresh CHANGESET_ID --request-id refresh-1
```

Inside Text UI, omit `homebrew-harness service` and `SERVICE_ID`. `delivery prepare` explicitly prepares current reviewed work; normal maintenance delivery creates its ChangeSet automatically. `delivery request` renews an unstarted action's authorization request. `delivery plan` creates a concrete registered action, including an explicitly targeted `post_comment` with approved text.

```python
with RemoteService.from_file(connection_file, control=True) as service:
    changesets = service.records("changesets")
    candidate = service.delivery(changeset_id)
    requests = service.requests()
    batch = service.batch(requests, request_id="group-1")
    receipt = service.decide_batch(
        batch["id"],
        [{"id": request.ref.id, "decision": "approve"} for request in requests],
        request_id="decision-1",
    )
```

A batch freezes membership and request versions; each item uses its existing approval controller and has its own result. Partial expiry or staleness does not invalidate other items. Stable request IDs retain replay receipts. Model proposals and operator discussion text do not grant authority.

Each external action has a durable journal. Lost acknowledgments become `unknown`. Inspection checks the exact Git ref or platform correlation marker. An unconfirmed query leaves the result uncertain. Explicitly confirming non-execution permits a fresh approval request; it does not silently repeat a write. Long queries use paginated `records`/`record` views. The `delivery-planning` skill explains candidate facts and outstanding decisions.

## Demonstration

```sh
python examples/delivery_demo.py --output /tmp/harness-delivery-demo
```

Use a new output directory. The scenario uses real local Git refs and a deterministic model/platform peer. It verifies CI repair, one reused change request, restart, grouped target approval, target publication, a public-comment receipt and resource closure without paid model access.

GitHub is the implemented write adapter. Other platforms implement the same fixed delivery operations and normalized feedback contract. Release/tag creation, issue closing, merge queues and outbound report channels remain separate future capabilities.

Host policy can select trusted `reviewer_ids` and a `required_approvals` count. Their effective decisions must apply to the published head: requested changes or insufficient approvals keep delivery in `review_waiting`, and target publication rechecks the gate. Ordinary comments and untrusted reviews remain discussion evidence. The default adds no required reviewers; configure this policy to match the project's review process.
