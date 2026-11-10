# Python API

[简体中文](PYTHON_API.zh-CN.md)

`Harness` is the application entry point. It prepares models and tools, owns attached executions, and exposes the same controllers used by the CLI. `AsyncHarness` provides awaitable views of those services.

## Start a Session

```python
from pathlib import Path
from homebrew_harness import Harness, HostConfig, SessionOptions

config = HostConfig.from_files()
with Harness(workdir=Path("project"), config=config) as harness:
    session = harness.sessions.open(SessionOptions(enabled=("subagents",)))
    turn = session.submit("Explain the public API", request_id="explain-api-1")
    report = turn.wait(120)
    if report.snapshot.output_available:
        print(turn.result().text)
    else:
        print(report.data())
```

The working directory is explicit; its state is saved under `.harness/`. The default model environment and tool configuration use the same host files as the CLI. Supply `env_file`, `models_config`, `mcp_config`, `test_config`, or `bash_config` to select other files outside the workspace. Connection values belong in those host files.

`SessionOptions` selects capabilities, a `model_profile`, MCP tools, memory behavior, and per-turn limits. Host configuration defines the available catalog. Enabling a capability retains its ordinary permission checks.

An application can borrow its own client or registry:

```python
config = HostConfig.from_components(client=client, model=model_name)
# Alternatively: HostConfig.from_components(model_registry=registry)
```

Borrowed clients and registries remain open when the Harness closes. Set `own_client=True` to transfer their close responsibility. `workflow_registry`, `hooks`, and an `environment` mapping are optional host components. Passing an environment mapping does not modify the application's environment.

## Services and execution handles

`harness.services` creates persistent services with a serial work queue. Independent applications use `RemoteService` over the authenticated HTTP API, with no local execution workspace or model configuration:

```python
from homebrew_harness import RemoteService

with RemoteService.from_file(connection_file, control=True) as service:
    work = service.work.submit("Implement the requested change", request_id="feature-1")
    print(work.wait(120).data())
```

`RemoteService` closes its own connection when the context ends; the service keeps running. A supplied `ControlClient` is borrowed by default; `own_client=True` transfers its close responsibility. `ControlConnection` carries explicit credentials, while `from_file` reads an external connection file. Client imports do not load POSIX execution backends. Native Windows service execution is a separate adaptation.

See [Services](SERVICES.md) for ServiceSpec, schedules, detached lifetime, connection configuration and the Text UI. AsyncHarness exposes the same nested host work API.

| Service | Main operations | Execution unit |
| --- | --- | --- |
| `sessions` | `open`, `get`, `resume`, `turn`, `list` | Persistent Session and one query Turn |
| `services` | `start`, `get`, `recover_launch` | Persistent service and serial work items |
| `runs` | `start`, `get`, `list` | Managed Run across Episodes and stages |
| `workflows` | `start`, `get`, `resume`, `list` | Registered recoverable script |
| `interactions` | `open`, `get` | Automatic demand routing and its tasks |
| `requests` | `list`, `approve`, `deny`, `answer`, `renew_tests` | Exact host decision |
| `admin` | `inspect`, `export`, `backup` | Explicit recovery and Run maintenance |

Turn, Run, Workflow, and interaction task handles share these observation methods:

| Method | Meaning |
| --- | --- |
| `snapshot()` | Current business state, input revision, requests, persistence and cleanup |
| `wait(timeout, until="actionable")` | Observe until completion, a host decision, or another actionable state |
| `wait(timeout, until="settled")` | Observe until terminal or paused execution has confirmed persistence and finished cleanup |
| `result()` | Read the execution's confirmed text or JSON result |
| `requests()` | List its current host requests |
| `watch(interval=0.1)` | Yield changed snapshots; the first event includes current state |

`ExecutionRef` contains `kind`, `id`, and an optional `owner_id`. `ExecutionSnapshot`, `ExecutionResult`, `WaitReport`, and `CloseReport` provide `data()` for JSON-friendly application responses. Results belong to their original query; a later Session query does not replace an earlier result.

`wait` timing out stops observation. Execution continues until its own deadline or an explicit control. `output_available`, `persistence_confirmed`, and `execution_pending` describe separate facts. A Turn can settle while its reusable Session retains connections. A Workflow can expose a paused state while cleanup is still pending; check `execution_pending` before resuming.

`watch` projects confirmed state and pending requests. Progress can be merged or repeated; the stream is suitable for a UI, while durable state remains authoritative. Closing a watcher does not cancel execution. SDK execution does not print runtime messages to the terminal by default.

## Submission, steering and cancellation

```python
turn = session.submit("Keep the existing public API", request_id="change-17")
print(turn.submission().data())
turn.steer("Also cover failure handling", request_id="change-17-amendment")
# turn.interrupt("Use the smaller implementation", request_id="change-17-interrupt")
# turn.cancel()
```

`submit` accepts durable input and returns without waiting for model output. Repeating a request ID with the same payload returns the same reference; reusing it for another payload fails. Keep caller-supplied IDs when a return may be lost. Run and Workflow handles also expose `submission()` to distinguish acceptance from launch confirmation. `startup_pending` requires inspecting the accepted execution instead of creating a duplicate.

Steering remains attached to the original query, including while that query is queued. A proposal from the old model turn is fenced before executing tools. `cancel()` cancels a Turn while retaining its Session. Session text is always task material: `/approve`, `/goal`, and `yes` do not invoke host commands through the SDK.

`session.execute(text, timeout=...)` combines submission and observation and returns a `WaitReport`. Use `submit` when the application needs a stable handle immediately.

## Host decisions and structured Goals

```python
pending = turn.requests()
# Present each request to the application's authorized user.
# After that user approves the displayed request:
harness.requests.approve(pending[0].ref, decision_id="decision-17")
```

A request reference includes its execution target and revision. Decisions apply to that exact invocation. Refresh requests after steering or a stale-reference error. `deny` handles tool rejection; `answer(ref, text=...)` handles a Run's attention question; `renew_tests(ref, runs=..., seconds=...)` renews its fixed-test grant. These decision types retain the native controllers' validation and expiry.

Immediate Session approvals expire with the live turn. Their response includes `delivery`; an accepted decision with an unknown delivery does not prove a tool ran. Durable Run requests survive worker restarts. An application exposing these methods over a network supplies its own authentication and authorized-user UI.

Pass a structured Goal to `session.submit(..., goal=goal_spec)` or `RunSpec(goal=goal_spec)`. The existing hard-condition contract and verifier remain in effect. See [the design document](DESIGN.md) for Goal composition. `session.goal_status()` reads current Goal state. `session.approve_test(name)` and `session.revoke_test(name)` manage registered Session test recipes explicitly.

## Managed Runs and detached execution

```python
from homebrew_harness.runs import RunSpec

spec = RunSpec(
    "Implement the requested feature and verify it",
    episode_model_calls=12,
    total_seconds=1800,
)
job = harness.runs.start(spec, request_id="feature-17")
print(job.wait(120).data())
# job.steer("Preserve the current behavior", request_id="feature-17-amendment")
# job.pause()
# job.resume()
# job.cancel()
```

RunSpec uses the existing stage, member, Goal, model profile, fallback, deadline, test, and MCP contracts. Run execution preserves the workspace's one-active-Run constraint and execution leases. Fees remain provider-managed.

`runs.get(id)` observes an existing Run. Use `runs.get(id, control=True)` to send controls explicitly. `runs.start(..., lifetime="detached")` launches through the existing detached supervisor; `job.detach()` transfers an attached Run after its execution settles. Closing an observing host leaves a detached Run running.

Workers need reconstructable host-file configuration. Process-local clients, environment mappings, hooks, and Python workflow callables belong to in-process Session or direct Workflow execution. They are not serialized into workers.

## Registered Workflows and automatic routing

```python
from homebrew_harness.orchestration.workflow_runtime import WorkflowRegistry, WorkflowSpec

async def summarize(ctx, args):
    return await ctx.agent(args["text"], label="summary")

schema = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}
registry = WorkflowRegistry([WorkflowSpec("summarize", "Summarize text", summarize, schema)])
config = HostConfig.from_components(workflow_registry=registry)
with Harness(workdir=Path("project"), config=config) as harness:
    job = harness.workflows.start("summarize", {"text": "Material to summarize"},
                                  request_id="summary-17")
    print(job.wait(120).data())
```

Workflow executes a registered script and reuses its native step journal, model binding, locks and cleanup. `job.pause()` requests a pause; `job.resume()` or `workflows.resume(id)` resumes a confirmed, settled pause with the original model profile. Arguments are validated before acceptance. `workflows.get(id)` reads saved output without allocating a model client.

For automatic demand routing:

```python
entrance = harness.interactions.open()
task = entrance.submit("Inspect the project and implement a small improvement", request_id="auto-17")
print(task.wait(120).data())
```

The interaction task keeps its reference when routing or handoff changes the native execution to a Run. Its snapshot reports the current route and target. It exposes `steer`, `interrupt`, `pause`, `resume`, `cancel`, `upgrade`, and `detach` where the native state permits them.

MCP selection uses `SessionOptions(mcp={"docs": ["search"]})` or `RunSpec(mcp={"docs": ["search"]})`; the host file supplies transport and permission policy. Teams and Subagents retain their model alias and optional read-only delegation contracts. Cron is a Session capability.

## Reopening and cleanup

```python
saved = harness.sessions.get(session_id)       # observation only
old_result = harness.sessions.turn(turn_id)   # original query reference
session = harness.sessions.resume(session_id) # explicit execution ownership
```

Resume checks the saved models, capabilities, execution policy and leases. Completed queries are not repeated. For interrupted queries, inspect `session.recovery_status()` and the Turn snapshot. `admin.inspect(session, operation_id, revision)` records an explicit inspection of an uncertain operation; `session.continue_recovery()` releases the native recovery block after its conditions are satisfied. Submit an explicit continuation when the interrupted inference has no confirmed final result. Accepted asynchronous followups remain associated with the original query checkpoint.

Session results and immediate decision receipts follow checkpoint retirement and restoration. A retired Session requires the existing host checkpoint management before reopening.

`harness.close(timeout=...)` requests shutdown and returns a `CloseReport`. It closes owned Sessions and Workflows and pauses attached Runs. `wait_closed(timeout)` observes subsequent cleanup. A pending report retains ownership and leases until real work ends. Context exit raises `ClosePending` when cleanup exceeds the window and preserves an existing application exception. Borrowed clients and external detached executions retain their own ownership.

`SDKError` exposes a stable `code` and optional execution `ref`; subclasses include `InvalidInput`, `StaleReference`, `UnsupportedOperation`, `ResultNotReady`, and `ClosePending`. Ordinary execution failure and host-wait states remain in snapshots/results.

## Async applications

```python
from homebrew_harness import AsyncHarness

async def execute(config):
    async with AsyncHarness(workdir=Path("project"), config=config) as harness:
        session = await harness.sessions.open()
        turn = await session.submit("Explain the modules", request_id="async-17")
        async for event in turn.watch():
            print(event.data())
        return await turn.result()
```

Async views reuse the same execution state. Service and control methods are awaitable; `id` and `ref` are immediate metadata. Cancelling `wait` or `watch` stops observation. Cancelling an accepted API call does not revoke its submission: keep its request ID to query or replay acceptance. The adapter has bounded call slots and keeps accepted resources owned through shutdown.

Run the [application example](../examples/python_api.py) with `--demo` for a deterministic Session, async Session, or registered Workflow without model access.

Maintenance profiles add `converse`, `conversation`, `records`, `record`, `reports`, `report`, `acknowledge_report` and `retry_job` to the same service view. `work.revise` proposes a new inactive work contract; `work.revoke` withdraws its authority. Service work-authority and information requests appear in `requests()` and use the same explicit decision API. See [Services](SERVICES.md#repository-maintenance-p2) for the host policy and workflow.

### Service delivery controls

The Service view adds `delivery`, `prepare_delivery`, `plan_delivery`, `inspect_delivery`, `request_delivery`, `refresh_delivery`, `batch` and `decide_batch`. Local and remote views use the same control protocol, and AsyncHarness exposes the corresponding awaitable methods. [Repository delivery](DELIVERY.md) documents fixed candidate identity, per-item decisions and unknown-action inspection.
