# Persistent services

[简体中文](SERVICES.zh-CN.md)

A service owns a persistent work queue and fixed schedules. Each work item runs through the existing managed Run supervisor. The `manual` profile is the first stage of service support: an operator submits work, watches progress, answers native requests, and pauses or stops execution.

The `repository_maintenance` profile adds GitHub input, requirement triage, scoped work authority, operator discussions and reports. Service work reaches `review` when its delegated Run completes. Grouped publication approval and remote delivery are later stages.

## Host setup

Use separate directories for private service control state and the mutable execution workspace. The control directory must be owner-only. Install the runtime outside the target workspace; this also applies when maintaining the harness itself.

Create the control directory and an external service specification:

```sh
mkdir -m 700 /path/to/private-control
homebrew-harness service --cwd /path/to/private-control start \
  --spec /path/to/private/service.json \
  --host-config /path/to/private/host.json --detach
```

The host configuration uses the same `InteractionConfig` fields as automatic interaction: model/tool file references, capability catalog, model alias and execution defaults. Configuration files stay outside both workspaces. An external environment file can instead be selected with `--env-file`.

The service specification contains:

```json
{
  "version": 1,
  "profile": "manual",
  "workspace": "/path/to/project",
  "run_defaults": {
    "total_seconds": 1800,
    "episode_model_calls": 12,
    "write_paths": ["src", "tests"]
  },
  "schedules": [
    {"name": "checkpoint", "every_seconds": 43200, "action": "record_tick"}
  ]
}
```

Unspecified Run defaults inherit the host settings. Explicit `run_defaults` use the existing RunSpec contract. Its test, model, scope, stage, and permission validation remains in effect.

`record_tick` accepts a durable scheduled marker without using a model. `submit_work` additionally requires a `prompt` and queues the fixed host-declared task. Missed intervals coalesce into one trigger. An outstanding scheduled work item prevents another copy from being queued. Trigger acceptance and task completion are reported separately.

One service uses one serial execution workspace in P1. A nonterminal or unfinished native Run retains its ordinary workspace constraint. The service waits for externally owned work instead of stopping it. Set `control_port` in the service specification to choose a fixed local HTTP port; its default is an automatically allocated loopback port.

## Observe and submit work

The start response contains `svc_...`. Use that ID to reconnect:

```sh
homebrew-harness service --cwd /path/to/private-control status SERVICE_ID
homebrew-harness service --cwd /path/to/private-control attach SERVICE_ID
homebrew-harness service --cwd /path/to/private-control work SERVICE_ID submit \
  "Implement the requested local change" --request-id feature-1
homebrew-harness service --cwd /path/to/private-control work SERVICE_ID list
homebrew-harness service --cwd /path/to/private-control requests SERVICE_ID
```

The line interface uses the same commands without the `service SERVICE_ID` prefix:

```text
service> status
service> work submit "Implement the local change" --request-id feature-1
service> work list
service> work show WORK_ID
service> focus WORK_ID
service> requests
service> answer REQUEST_ID REVISION "Keep current behavior" --decision-id answer-1
service> approve REQUEST_ID REVISION --decision-id approve-1
service> pause execution
service> resume
service> exit
```

Unrecognized text is submitted as ordinary work. `focus WORK_ID` selects the work and captured version for steering; `focus` clears it. Focus is saved per control context and restored on reconnect. A work without native execution reports that condition. Text is never treated as an approval. `work steer WORK_ID TEXT` and `work interrupt WORK_ID TEXT` target that work's native execution. `work pause`, `work resume`, and `work cancel` send explicit controls.

Submission output confirms acceptance with the work ID and request_id. Use status/show to observe execution. Transport errors also display their request_id so a lost acknowledgment can be inspected without creating another task.

`approve`/`deny` handle tool requests; `answer` handles requirements; `renew-tests` renews fixed-test grants with `--runs` and `--seconds`. Requests retain their original target, revision, parameters and expiry. Refresh after a stale-reference error. There is no grouped approval in P1.

`exit` or EOF disconnects the UI and leaves a detached service running. Ctrl-C cancels the current input/observation. `stop` explicitly requests service shutdown. Output is plain JSON/text; external terminal control characters are escaped. No terminal UI library is required. Text UI and individual control commands use the same authenticated HTTP API.

## HTTP connections and remote clients

The service binds to loopback. A private control credential is generated once per service, and each instance publishes a private endpoint descriptor in `.harness/control/endpoint.json`. Local commands discover only this connection configuration; they do not read the service database.

Remote clients use an external connection file:

```json
{
  "version": 1,
  "service_id": "SERVICE_ID",
  "endpoint": "CONTROL_ENDPOINT",
  "token_env": "HARNESS_CONTROL_TOKEN"
}
```

Replace the ID and endpoint in that private file. `token_file` can instead reference an owner-protected credential JSON containing version, service_id and token; use exactly one credential source. A local descriptor also binds its instance. Do not put connection values or credentials in committed files, command-line token arguments or application logs.

```sh
homebrew-harness service --connection /path/to/private/client.json status SERVICE_ID
homebrew-harness service --connection /path/to/private/client.json attach SERVICE_ID
```

Use an SSH tunnel to reach the loopback server remotely. Plain HTTP client connections are restricted to loopback; an explicitly deployed remote endpoint must use HTTPS. The client retains certificate validation and disables environment proxies, redirects and automatic command retries.

The versioned interface uses `POST /v1/control` for registered queries/commands, `GET /v1/services/{id}` for snapshots and authenticated `GET /v1/health`. The command body retains version, service_id, method, payload and request_id. All API calls require Bearer authentication; Host/Origin checks reject unexpected browser access and CORS is not enabled. A future Web UI can use the same API and same-origin hosting.

HTTP and JSON sizes, connection count and read time are bounded. Keep large input/diff/report queries paginated. An HTTP timeout or lost response retains the original request_id and does not prove the command was unaccepted. Query its receipt or repeat that same ID/payload when the service is available.

The service may become unreachable after `stop`; remote clients report connection uncertainty rather than guessing `stopped`. `service inspect SERVICE_ID --cwd CONTROL_DIR` is an explicit host-local inspection operation for saved state. There is no local database fallback in remote clients or Text UI.

## Pause, stop and restart

```sh
homebrew-harness service --cwd /path/to/private-control pause SERVICE_ID --scope execution
homebrew-harness service --cwd /path/to/private-control resume SERVICE_ID
homebrew-harness service --cwd /path/to/private-control stop SERVICE_ID
homebrew-harness service --cwd /path/to/private-control start SERVICE_ID --detach
```

Pausing stops new work and requests native pause at its safe boundary. Work submitted while paused stays queued. Pausing a queued work item does not block other eligible queue entries; a paused native Run still occupies its workspace.

Stopping pauses owned active Runs and waits for actual resources. `state`, `resources_closed`, `live_owner`, and the native execution state are separate facts. A pending close retains ownership. If native cleanup requires inspection, use the existing Run recovery commands and then retry shutdown.

Restart reconstructs the saved host configuration and verifies its runtime and execution binding. It retains work IDs, accepted Run IDs, requests and scheduled positions. A Run paused by the service can continue after restart; an independently paused or blocked Run requires its explicit work control.

An unacknowledged detached launch is `startup_pending`. Inspect its saved token; the explicit `recover-launch SERVICE_ID --token TOKEN` command fences the old token only after acquiring real ownership locks. A delayed old child cannot use that token afterward. A confirmed dead supervisor can be reconstructed while native worker recovery continues through the existing Run fencing.

## Python API

```python
from homebrew_harness import Harness, HostConfig, ServiceSpec, ScheduleSpec

with Harness(workdir=control_dir, config=HostConfig.from_files(env_file=env_file)) as host:
    service = host.services.start(
        ServiceSpec(str(project_dir), schedules=(ScheduleSpec("checkpoint", 43200),)),
        lifetime="detached",
        request_id="maintenance",
    )
    work = service.work.submit("Implement a local change", request_id="feature-1")
    report = work.wait(120)
    print(report.data())
```

`host.services.get(id)` observes; use `control=True` to send controls. The live in-process owner can also start with `lifetime="attached"`; closing its Harness stops the owned service. A detached service remains owned by its supervisor. Host-local SDK inspection can read saved stopped state; remote clients use HTTP only.

`service.work.list(state=..., cursor=..., limit=...)` returns paginated summaries. `work.input(offset=..., limit=...)` reads the original task in bounded chunks. `work.snapshot()` projects the native execution and original work identity; `work.result()` reads its confirmed delegated result. `review` is an actionable state.

`host.requests.list(target=service.ref)` and `host.requests.answer/approve/...` route service-owned native requests to their actual Run controller. `service.receipt(request_id)` queries a control receipt when an acknowledgment may be lost. Repeating a stable ID with another command payload fails.

AsyncHarness includes the same awaitable service and nested work methods:

```python
service = await host.services.start(spec)
work = await service.work.submit("Implement the change", request_id="feature-1")
report = await work.wait(120)
```

Persistent services require reconstructable host files. P1 does not serialize process-local clients, hooks or workflow callables into the supervisor.

An independent application can connect without a local control directory:

```python
from homebrew_harness import RemoteService

with RemoteService.from_file(connection_file, control=True) as service:
    work = service.work.submit("Implement a local change", request_id="feature-1")
    print(work.wait(120).data())
```

RemoteService, ControlConnection and their shared DTO/observation layer do not import POSIX execution backends. This makes the client suitable for a Windows operator connecting to a Linux/macOS service. Native Windows service execution still requires separate file, lease and process adaptation.

Closing RemoteService releases its owned HTTP connection. Passing an existing ControlClient borrows it by default; use `own_client=True` to transfer close responsibility. Closing a local observer Harness also releases its HTTP connections while the external service continues running.

## Deployment and verification

Run a deterministic local demonstration without paid model access:

```sh
python examples/service_demo.py --output /tmp/harness-service-demo
```

System-level startup and restart belong to systemd, launchd or the existing container host. The service itself records execution ownership and preserves paused/stopping intent. The HTTP control protocol has bounded requests and connections and exposes only registered service methods. The local standard-library server is a restricted control endpoint; public Web deployment and browser session authentication require their own validation.

The control store, work queue and schedules use the current SQLite backend. The runtime, model/tool bindings and native deadlines are preserved. Fees remain provider-managed. Future service stages build on these contracts.

## Repository maintenance (P2)

Repository input is selected by an explicit provider/instance/project declaration. The current adapter implements GitHub; common work and authorization remain platform-independent. See [Code platforms](PLATFORMS.md) for configuration, object identity and extension contracts.

The `repository_maintenance` profile adds repository input, requirement triage, work authority, persistent operator discussions and reports. GitHub writes and publication remain separate delivery features. A model proposal selects a host scope; the runtime validates that scope and records authority before starting a native Run.

Add these fields to an external service specification:

```json
{
  "profile": "repository_maintenance",
  "sources": [{
    "name": "project",
    "provider": "github",
    "instance": "primary",
    "project": "OWNER/REPOSITORY",
    "api_url_env": "PROJECT_API_ENDPOINT",
    "token_env": "PROJECT_READ_TOKEN",
    "poll_seconds": 43200,
    "max_pages": 5
  }],
  "maintenance": {
    "objectives": "Maintain documented behavior and compatibility.",
    "non_goals": "Do not publish or add unsupported platforms.",
    "scopes": {
      "code": {
        "write_paths": ["src", "tests"],
        "tools": ["read_file", "write_file", "edit_file", "glob", "search_text"],
        "allow_tests": []
      }
    },
    "auto_categories": ["bug", "documentation"],
    "authority_seconds": 3600,
    "report_seconds": 43200
  }
}
```

The usual workspace, version and Run defaults also apply. Fill connection values only in the external host environment. Source credentials are used by the fixed read adapter. Scopes must fit within the host Run defaults for paths, tools and registered tests. Omitting `auto_categories` sends executable proposals to the operator for approval. Automatic categories choose among already authorized scopes; they do not expand permissions. Native tool approvals remain in effect.

Repository polling records issue/PR identities and issue-comment versions, with durable page positions, overlap and deduplication. A poll observes available versions; it cannot reconstruct an edit or deletion that disappeared between polls. Source failures retain their cursor and show retry state. Unchanged polling makes no model calls.

Triage uses three packaged skills: requirement-triage, maintenance-planning and maintenance-reporting. Their bodies load on demand. Service reasoning can read service records and visible project files, and submit candidates; it cannot execute Bash, approve requests or publish changes. Plans and scopes are validated by the host.

```sh
homebrew-harness service records SERVICE_ID events
homebrew-harness service records SERVICE_ID sources
homebrew-harness service records SERVICE_ID jobs
homebrew-harness service requests SERVICE_ID
homebrew-harness service approve SERVICE_ID REQUEST_ID REVISION --decision-id decision-1
homebrew-harness service chat SERVICE_ID "What should we prioritize next?" --request-id discussion-1
homebrew-harness service message SERVICE_ID MESSAGE_ID
homebrew-harness service report SERVICE_ID
homebrew-harness service records SERVICE_ID reports
homebrew-harness service report SERVICE_ID REPORT_ID
homebrew-harness service report-ack SERVICE_ID REPORT_ID
```

The same commands work with an external `--connection` file. `record KIND ID --offset ... --limit ...` reads long service records in chunks. `work revise WORK_ID TEXT` revises an inactive work contract; `work revoke WORK_ID` revokes its authority. Active execution still uses explicit steering, pause, cancellation and recovery. Authority renewal and execution resumption are separate controls.

For this profile, ordinary Text UI input starts or continues an operator discussion. Use `work submit` to request new work and explicit `work steer` to amend an active Run. Focus binds discussion to the selected work; a proposed plan must still match its current requirements when accepted. Model text is never interpreted as an approval.

Python uses the same remote views:

```python
with RemoteService.from_file(connection_file, control=True) as service:
    message = service.converse("Discuss the next release", request_id="discussion-1")
    print(service.conversation(message["id"]))
    print(service.records("jobs"))
    print(service.records("reports"))
```

Messages are durable and their replies can be read after reconnect. Reports reference recorded work and execution facts; acknowledging one updates that reader's position. Reports are available through the control interface. Public GitHub replies, outbound report delivery and grouped publication approvals are not performed by this profile.

`reports` lists unread ready reports for a control context. `job-retry JOB_ID --request-id ...` explicitly authorizes another bounded reasoning attempt cycle; cumulative attempts remain recorded. `work request-authority WORK_ID` refreshes an expired or denied request for the current plan, without rerunning triage or granting execution. Information requests use `answer`, and their replies amend requirements before a new triage pass.

Run the complete deterministic P2 scenario:

```sh
python examples/maintenance_demo.py --output /tmp/harness-maintenance-demo
```

The scenario polls a local GitHub fixture, automatically handles a scoped bug, retains a feature decision across a service restart, executes fixed tests, restores a discussion, and creates a report. It uses no paid model access.

Work snapshots expose a verification reference. Read its `evidence` record to inspect the actual native test receipts and the requirements version they belong to. When source requirements change during execution, completion of the earlier Run remains historical evidence; the updated work continues through triage and authority. Structured Goals submitted with work remain in the compiled RunSpec. Expired requests stay expired until an explicit refresh. Repository sweeps reconcile page identities before advancing their watermark; an unstable sweep exposes `rescan_required` and keeps the old position.

## Fixed-candidate delivery (P3)

Optional delivery prepares a private Git candidate from reviewed work, rechecks fixed tests and original Goal conditions, runs an independent review, publishes a review branch/change request, and waits for exact-SHA CI. The operator approves target publication and public comments separately. CI code repair reuses native Runs with the original scope and cumulative limits. Grouped decisions, action receipts and restart recovery use the same HTTP/SDK/Text UI. See [Repository delivery](DELIVERY.md) for configuration and the offline demonstration.

## Service operations (P4)

[Service operations](SERVICE_OPERATIONS.md) add deterministic health, native writer parking, cross-workspace retention, backup sets/restoration, fixed repository audits and explicit compatible runtime handoff. CLI, Text UI and Python share the authenticated control methods.

Optional community communication preserves multi-round issues and external PR reviews separately from code delivery. See [Community discussion](COMMUNICATION.md).
