# Work sources, continuation and interactive service execution

[简体中文](WORK_CONTROL.zh-CN.md)

A Work retains its accepted requirements, owned source versions, Run history and actual verification/publication receipts. Pausing or replacing a Run does not erase those facts. A recovery proposal is a model decision; applying it is a separate native operation.

## Source views

`work.sources()` identifies the current draft, fixed candidates, historical candidates and preserved sources. `read_source(source_ref, path, offset=0, limit=10000, expected_version=None)` reads Unicode character pages. Keep the returned version when following `next_offset`. `compare_sources(before, after, ...)` compares complete trees and pages the file directory; it names both comparison sources and reports the total number of changed paths.

An unstaged-file list describes changes against the current checkout, not all changes against an older feature candidate. Source references are scoped to their Work; paths cannot escape that source or address runtime/credential directories.

`work.observe_target(execution=...)` observes the registered target once. Its execution result contains a fixed target source reference. Observation does not replace a development branch or choose when to integrate.

## Plan, prepare and start

`work.plan_recovery(instruction)` remains read-only. The model may return text and a concrete `action` selecting `resume`, `fork` or `adopt_candidate`, an owned `source_ref`, its reason, and optional integration `{method: merge/rebase, target_ref}`. `execute=True` explicitly asks the service to apply the model's scoped choice after planning. A plan without an action does not start work.

`work.apply_recovery(job_id=..., start=False, execution=...)` applies the current versioned proposal. `work.continue_from(action, ...)` applies an explicit native selection. Forking preserves the entire current draft, copies the selected source to a new owned workspace, preserves requirements/permissions/history, and prepares a fresh Run. `start=False` leaves it paused for inspection. A returned start request does not claim the Run has already begun.

Resume keeps the current binding/source. A different source requires an explicit fork/adoption. Existing counters, required acceptance and publication authority are not reset by a new Run. A source or instruction change invalidates a stale proposal; inspect the current facts rather than substituting another branch.

## Integration

Integration chooses a fixed target explicitly. Main movement is an observation; automatic base refresh is an opt-in policy. A Git rebase can return conflicts. After resolving them, use `work.control_integration("continue")`; inspect the result before completing development, since replaying another commit may produce another conflict. `abort` abandons that integration, retaining preserved source history. These operations never publish a branch or merge a PR.

## Execution views

`work.executions(cursor=0, limit=20)` pages owned executions including final candidate checks and Service jobs. Inspect `work.execution(id)`, read incremental `execution_output`, wait with `wait_execution(id, seconds)`, request `stop_execution`, or send input where supported. An observation wait expiring leaves the operation alive. Check actual cleanup and results before restarting conflicting work.

Source/continuation operations accept `execution={"mode":"interactive","wait_seconds":0}` or `{"mode":"one_shot"}`. Interactive returns an execution reference promptly; one-shot waits. `timeout_seconds` is an optional explicit operation limit, not an observation window.

The development Lead receives scoped source and execution tools when bound to a live Service Work. They do not reveal raw platform credentials or grant arbitrary host Git/Bash access. Final acceptance still uses real required test results, fixed-source verification and any explicitly required independent review. Optional checks and workflow order remain model choices.

## Persistent state and failures

Service jobs preserve unfinished phase conversation/tool facts and current instruction context; complete phase caches remain version checked. Inbox polling has its own worker, so a long reasoning job does not suspend collection. `service.control_job(id, "pause"/"resume"/"stop"/"input")` controls an admitted job without stopping unrelated Work.

Execution instructions are retained, and historical verification is discoverable beyond a short preview. Stopped budget migrations record admission before their Run step. If a crash or conflict interrupts the two-database operation, inspect/replay that same request while stopped; unresolved admission prevents startup. Reported partial migration never means configuration consistency or renewed authority.

Legacy `service.refresh_delivery(...)` and publication `inspect_*` reconciliation keep their immediate-result behavior when `execution` is omitted. Pass `execution={"mode":"interactive","wait_seconds":0}` to receive a Service-owned execution reference instead. Use `service.executions()`, `execution(id)`, `execution_output(id)`, `wait_execution(id, seconds)`, and `stop_execution(id)` to observe and control it. Work-owned executions use the corresponding `work` methods; a reference from another owner is rejected. Immediate confirmation of non-execution does not take execution options.

An explicit failed-job retry opens a fresh call window and retains cumulative attempts, previous errors, dialogue and tool observations. Replaying an admitted stopped-budget change reapplies only its requested fields to current settings; it preserves unrelated newer settings and Work instructions. It remains bound to the original paused Run and does not renew authority.

Recovery, review and other proposal jobs can observe source/execution facts; their tool contracts do not apply Git changes or stop/steer executions during a read-only proposal. An owning development Run and an explicit focused conversation use the control variants within the same Work. A recovery choice is applied through the native versioned action when requested, preserving the distinction between deciding and executing.

`published_head` names the last confirmed harness branch publication; `observed_pr_head` is the latest recorded PR observation. A branch rewrite binds to the confirmed publication receipt, while a different remote head still requires inspection.
