# Controlling long operations

[中文](EXECUTION.zh-CN.md)

Long tool calls can return control to the model while their execution remains owned by the runtime. The model can inspect output, wait, send supported input, or stop an operation. The same execution tools are assembled for Sessions, managed Runs, subagents, team members, and Service model jobs when their underlying tools are authorized.

## Execution modes

Bash, registered tests, subagent `task` and `workflow` accept an `execution` object:

```json
{
  "command": "make test",
  "execution": {"mode": "interactive", "wait_seconds": 10}
}
```

`interactive` is the default. The call waits briefly and returns either the completed result or an `execution_id`. Waiting does not stop the process. Use `one_shot` to wait for the final result:

```json
{
  "command": "make test",
  "execution": {"mode": "one_shot", "timeout_seconds": 900}
}
```

`timeout_seconds` is an optional execution deadline. Omission or `null` adds no per-call wallclock deadline. The parent Run or task deadline and explicitly configured host limits still apply. `wait_seconds` accepts 0–60 seconds and is only an observation window for interactive calls; it is not accepted in one-shot mode.

Interactive executions can continue across normal Run Episode boundaries. Checkpoints retain their execution IDs, and the next Episode can observe or control the same attempt. A `started` operation with a live owner remains pending; an operation whose owner was lost, or whose effects are unknown, requires recovery inspection. Explicit stops and parent deadlines still end execution.

Execution mode does not change permissions, working directories, or container policy. Short calls normally finish during the initial wait. The existing Bash `run_in_background` endpoint remains available through its background module; use the execution options for the shared observation and input interface.

`delete_file` removes one regular file within the same authorized write scope as `write_file` and `edit_file`. It accepts an optional `expected_version` from `read_file`, reports an already absent file separately, and rejects directories and symlink paths. Deletion does not require Bash access. A failure after removal retains the uncertain result for recovery inspection.

For another registered workspace tool, use the shared wrapper to select the execution mode without repeating options in every tool schema:

```json
{"tool": "read_file", "arguments": {"path": "report.txt"}, "execution": {"mode": "interactive", "wait_seconds": 0}}
```

Submit this object to `invoke_tool`. Only the original tools visible to the current context can be invoked.

## Observation and control

| Tool | Purpose |
| --- | --- |
| `list_executions` | Discover operations owned by the current context |
| `get_execution` | Read state, available controls, and result information |
| `read_execution_output` | Read incremental output using a character cursor |
| `wait_execution` | Wait for settlement without stopping execution on wait expiry |
| `send_execution_input` | Send input with an idempotent request ID when supported |
| `stop_execution` | Request cancellation and then observe actual cleanup |

Control tools use `execution_id`. Output queries accept `cursor` and `limit`, with at most 16,000 characters per response. `stream="result"` pages through a large final result; the default stream reads process output. Completed executions also produce a saved event for the owning model. Output is not sent to the model line by line.

A stopped wait, no new output, and a running execution are observations, not successful verification. A cancellation acknowledgment means the request was accepted; resources may still be stopping. Inspect `execution_pending` and `cleanup` before restarting conflicting work.

Input capability depends on the operation. Bash pipes accept stdin; subagent tasks accept steering for subsequent model steps. Test commands usually have no interactive input. To change a command or test selection, stop or finish the old attempt and start a new one with revised arguments. Input request IDs prevent duplicate application, and input state distinguishes acceptance from application or rejection.

## Selecting tests

A registered test recipe can provide a selector adapter. The host retains its launcher, environment, test roots, roles, and resource policy; the model chooses targets within that scope.

```json
{
  "name": "unit-tests",
  "selection": {"targets": ["tests.test_parser.ParserTests.test_empty_input"]},
  "execution": {"mode": "interactive", "wait_seconds": 0}
}
```

Omit `selection` for the full recipe. The unittest adapter accepts module, class, and method names. The pytest adapter accepts files or node IDs and an optional `filter`; pytest must be installed in the registered execution environment. A recipe with no selector adapter rejects selections rather than ignoring them.

For an external registry:

```json
{
  "version": 1,
  "limits": {"max_runs": 20, "total_seconds": null},
  "recipes": [{
    "name": "unit-tests",
    "builtin": "unittest",
    "test_roots": ["tests"],
    "roles": ["lead", "subagent", "teammate"],
    "timeout": null,
    "cpu_seconds": null,
    "authorization": "confirm"
  }]
}
```

The default cumulative test time, per-test wallclock time, and per-process CPU-seconds ceiling are unset. Test wallclock usage is still recorded. Explicit finite limits, execution counts, parent deadlines, and container CPU allocation, memory, and PID limits remain effective. Existing managed Runs retain their captured policy until an authorized new execution is created.

Receipts record the selection and `test_scope` (`full` or `selected`) as well as the source snapshot, exit code, and cleanup. A selected pass does not satisfy a full-suite `tests_passed` condition or a Service full-quality gate. Zero collected tests, cancellation, timeout, or changed source do not count as passing.

A host-defined `tests_passed` predicate may explicitly include the same `selection` object to require selected verification. Without it, verification requires the full recipe. A later selected failure invalidates an older full pass.

## MCP and workflows

MCP tools retain their original provider schemas. To select the execution mode explicitly, use `invoke_mcp_tool`:

```json
{
  "tool": "mcp__docs__search",
  "arguments": {"query": "execution lifecycle"},
  "execution": {"mode": "interactive", "wait_seconds": 0}
}
```

The wrapper applies the original tool's validation and permissions. Cancelling one request leaves peer calls on the shared connection available. Connections allow four concurrent requests by default; the host can set `concurrency` from 1 to 32. A server's reported progress appears in execution output. Local cancellation cannot prove that a remote side effect was undone; uncertain results retain their recovery contract.

The registered `workflow` tool accepts the same execution options. Workflow steps and checkpoints remain authoritative, and continuation uses the workflow's existing `resume_from_run_id` contract after its resources settle.

## Python and Service controls

A Session or Run exposes scoped execution views:

```python
for snapshot in session.executions.list():
    operation = session.executions.get(snapshot.ref.id)
    print(operation.output(cursor=0, limit=4000))
    operation.stop(request_id="operator-stop")
```

Use `operation.input(text, request_id=...)` for supported input, `operation.wait(...)` for observation, and `operation.result()` for the final result. Snapshot `details.inputs` exposes accepted, applied or rejected input records without including input text. Replaying the same input request ID remains idempotent after settlement. Model stdin is authorized against the process's original working directory. Read-only Run views cannot submit controls. Service Work views provide `executions()`, `execution(id)`, `execution_output(id)`, `stop_execution(id, ...)`, and `send_execution_input(id, text, ...)` through the HTTP control API.

Runtime restarts preserve attempts and results. An execution that cannot be reattached is reported as interrupted with its cleanup and effect uncertainty; it is not automatically replayed. Active and unsettled operations are retained by checkpoint retirement.

## Long host hooks

Short hooks can remain inline. An importable, module-level host callback can use process execution:

```python
hooks.register_hook("PreToolUse", assess_request, execution="process")
```

Process hooks retain the normal Hook contract and can be cancelled with their owning operation. Their source identity is checked before execution. Closures and objects requiring live host state should use the existing cooperative runtime facilities rather than being presented as independently killable processes. Pending cleanup remains visible until the real execution ends.

## Persistence and closing windows

State persistence has a separate 180-second window. Database connection preparation defaults to 15 seconds and write-lock waiting to 30 seconds; after acquiring the write lock, the transaction has a 60-second window. Checkpoint and tool-result writes can retry up to three times when no write started or rollback was confirmed, within the original persistence deadline. Retries preserve the result and execution identity. A commit with uncertain confirmation retains its inspection contract.

The default resource-closing window is 60 seconds. Existing explicit values remain effective. Persistence can save completed facts after a model or Episode deadline, and stays within an enclosing resource-closing window. Storage timeout diagnostics identify the operation, phase, limit, elapsed time and commit/rollback state. A caller's observation wait remains separate from these windows.

Automatic approval review and the Goal model checker default to a 500-second window. Fixed workspace Git queries allow up to 120 seconds. Explicit shorter settings and parent execution deadlines still apply.

MCP connection setup and container management default to 90 seconds; container inspection allows 15 seconds. Service operations such as backup and audit have a 1500-second execution window. Status-query observation waits remain separate.

## Working-state compaction

Compaction preserves the complete task, accepted amendments and plan separately. Its working record prioritizes unfinished work, relevant contracts and evidence; the detailed transcript and operation index remain retrievable. The target is 8,000 characters and the normal ceiling is 10,000. An oversized result receives one focused shortening request with a lower drafting target. A corrected result may use at most 5% extra space, capped at 500 characters across the complete summary, when verified spare context is available. The complete serialized context is checked before installing the replacement. Missing space or larger overflow preserves the original history and reports the applicable limits.

Native Run automatic review is registered before its durable request becomes visible. Normal model or Episode boundaries wait for this critical activity before closing its Session. Explicit pause, cancellation, request invalidation and absolute deadlines still apply; manual decisions win over late model output. New Runs default to a 900-second approval-request window, bounded by the Run deadline; an explicit shorter window remains effective.
