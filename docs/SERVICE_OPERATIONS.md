# Service operations

[简体中文](SERVICE_OPERATIONS.zh-CN.md)

Service operations add health facts, writer parking, retained execution references, backup sets, repository audits and compatible runtime upgrades. They use the existing Service, Run, TestRunner and storage controllers. Operations return persistent IDs, and CLI, Text UI and Python use the same authenticated control API.

## Host policy

Add operations settings to the external ServiceSpec:

```json
{
  "operations": {
    "mode": "isolated",
    "git_executable": "/ABSOLUTE/PATH/TO/GIT",
    "backup_directory": "/path/to/private/backups",
    "operation_seconds": 120,
    "stale_seconds": 30,
    "min_free_bytes": 16000000,
    "retention_days": 15,
    "keep_recent": 10,
    "candidate_retention_days": 30,
    "audit_globs": ["src/**", "tests/**"],
    "diagnose_incidents": false
  },
  "schedules": [{
    "name": "repository-review",
    "every_seconds": 43200,
    "action": "review_repository"
  }]
}
```

Backup and candidate paths remain outside the project and private control directory. Create the backup directory with owner-only access. The Git executable is fixed by the host. `audit_model` optionally selects a model catalog alias. Repository audit schedules require the maintenance profile.

`mode="isolated"` requires actual ContainerBackend evidence for candidate runtime tests. Explicit `local` mode supports trusted, credential-free test fixtures with native execution. Fees remain provider-managed; deadlines and bounded attempts control execution.

## Health and operation records

```sh
homebrew-harness service ops SERVICE_ID health
homebrew-harness service ops SERVICE_ID list
homebrew-harness service ops SERVICE_ID show OPERATION_ID
homebrew-harness service ops SERVICE_ID read OPERATION_ID --offset 0 --limit 4000
```

Health separates service liveness, execution readiness, pending operator decisions and progress facts. It includes worker errors, source collection positions, work/request/action counts, heartbeat age, disk space and unresolved delivery or workspace synchronization. Ordinary approval waiting is visible as a waiting state. Health queries make no model calls.

Incidents retain their identity and resolution. Optional incident diagnosis uses the existing model connection and `service-incident-triage` skill, producing an assessment and proposed verification. Model text grants no recovery authority.

Operations progress through queued, running, completed or blocked. Stable request IDs retain acceptance receipts. A completed preparation operation may reference an independent audit/model job; inspect that referenced job for its final result. Lists return summaries, and long plans use paginated record reads.

## Park and resume

```sh
homebrew-harness service ops SERVICE_ID park --request-id maintenance-1
homebrew-harness service ops SERVICE_ID resume --request-id resume-1
```

Parking stops new business work, closes source/delivery workers, pauses and drains owned native Runs, and retains the HTTP control server. `draining` means resources are still ending; `parked` confirms the local writer boundary. Queries and approved maintenance operations remain available. Business-changing commands wait for explicit resumption.

Parking and restart preserve native work identity and pending decisions. Resumption verifies the installed runtime/model/skill binding and reconstructs business workers. Arbitrary extension threads still need cooperative cancellation; ownership remains retained until they finish. `stop` closes the control server and the service itself.

## Retention and backups

```sh
homebrew-harness service ops SERVICE_ID retention-preview --request-id retention-1
homebrew-harness service ops SERVICE_ID retention-apply PLAN_OPERATION_ID --request-id prune-1
homebrew-harness service ops SERVICE_ID backup --request-id backup-1
homebrew-harness service ops SERVICE_ID verify-backup BACKUP_ID --request-id verify-1
homebrew-harness service ops SERVICE_ID restore-backup BACKUP_ID --request-id restore-1
```

Retention extends existing GC roots into Service-owned execution workspaces. Unfinished work, applicable decisions, unread reports, uncertain external actions and pending workspace synchronization protect required sessions and evidence. Closing a service retains these references. Existing checkpoint retirement keeps its policy of completed cleanup older than 15 days and exclusion of the most recent ten sessions. Old unreferenced candidate payloads can also retire while their work, commit identity and confirmed delivery facts remain queryable.

Preview and application bind to current business facts, retained references and file versions. Host application rechecks those facts and records progress. Models cannot select arbitrary directories for deletion.

A backup set contains consistent SQLite copies and necessary attachments for the control and owned execution roots, with a complete hash manifest. Connection credentials, HTTP tokens/endpoints, locks and external host files are excluded. Verification can run while business execution is active. Creation and restoration require parked writers.

Restoration preserves current maintenance receipts and the live control connection. Its journal records progress across roots. The first implementation restores original directory relationships and requires unchanged business facts; newly accepted work, inputs or delivery prevent an older snapshot from replacing current state. Keep complete backup sets for host-led disaster investigation or migration when current facts cannot be established.

## Repository review

```sh
homebrew-harness service ops SERVICE_ID audit --request-id review-1
homebrew-harness service ops SERVICE_ID audit --force --request-id full-review-1
```

The audit captures a fixed working snapshot and its observed Git HEAD, then uses an independent model context to review that snapshot. The `code-review` skill produces structured findings. Findings enter normal requirement triage and work authorization; duplicate unresolved findings link to existing work. The same content and scope reuse a successful review unless the host requests a forced review. A changed working snapshot requires new evidence even when HEAD is unchanged.

Each ordinary development Run uses a separate review branch; CI repairs keep the same branch and change request for that lineage. A subsequent work version captures a fresh input baseline. Confirmed CI-repair delivery synchronizes changed files back to the primary workspace only after other writers stop and the expected old contents match. Conflicting new local changes are preserved and surfaced through health/work state.

## Compatible runtime upgrades

Declare upgrade candidates by host alias:

```json
{
  "upgrade_candidates": {
    "candidate": {
      "workspace": "/path/to/verified-source",
      "package": "/path/to/installed/homebrew_harness",
      "recipe": "candidate-tests",
      "version": "CANDIDATE_VERSION"
    }
  }
}
```

Place this catalog inside `operations`. The source uses the project `src/homebrew_harness` layout. Its fixed test recipe must watch every installed Python and Skill file. Preparation verifies source/package equality and fresh passing tests; isolated mode uses ContainerBackend. Candidate tests receive no publishing credentials.

```sh
homebrew-harness service ops SERVICE_ID upgrade-prepare candidate --request-id upgrade-1
homebrew-harness service ops SERVICE_ID upgrade-arm PLAN_ID --request-id authorize-1
homebrew-harness service stop SERVICE_ID
```

The host installs/selects the approved version and starts the saved service using that environment. The recorded handoff permits only the exact approved binding. The new instance remains parked in probation:

```sh
homebrew-harness service ops SERVICE_ID upgrade-confirm PLAN_ID --request-id confirm-1
homebrew-harness service ops SERVICE_ID resume --request-id resume-new-1
```

The first upgrade path preserves current state schema and execution configuration contracts. Keep the interpreter/test-command identity compatible with saved execution bindings. It updates Python runtime and Skill bindings explicitly; it does not install packages or replace modules in a live Python process. `service-upgrade-assessment` explains evidence and compatibility before the operator arms a plan.

An unconfirmed handoff with unchanged business facts can roll back after the process has stopped. The following is an explicit host-local maintenance command:

```sh
homebrew-harness service ops SERVICE_ID upgrade-rollback PLAN_ID --request-id rollback-1 --cwd CONTROL_DIR
```

Start the recorded old installed version afterward; it remains parked. Confirmed upgrades or newly accepted business effects require a new recovery decision, preserving their journals and receipts.

## Python and demonstration

```python
health = service.health()
park = service.quiesce(request_id="maintenance-1")
state = service.operation(park["id"])
# After state confirms parked:
backup = service.backup(request_id="backup-1")
print(service.operation(backup["id"]))
service.resume_operations(request_id="resume-1")
```

The Service view also exposes audit, diagnosis, retention, backup verification/restoration and upgrade methods. AsyncHarness provides corresponding awaitable calls. Offline rollback uses `host.admin.service_upgrade_rollback` with the Service reference and stable request ID.

```sh
python examples/service_operations_demo.py --output /tmp/harness-service-operations-demo
```

Use a new output directory. The scenario extends real local Git/Run/CI delivery with another audited iteration, unchanged-review skipping, discussion, live HTTP parking, retention, verified backup/restoration and upgrade preparation/authorization. Actual new-version probation, native execution and rollback are covered by separate process tests. The synthetic model/platform peer makes no paid model calls.

Queued repository audits retain their intent while writers are active. Their execution deadline begins when claimed, and repeated schedule ticks coalesce into one pending audit. Test cleanup has independent bounded attempts; unfinished resources keep their lease and closing state until a later attempt confirms release.
