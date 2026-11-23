# Runtime budgets and model decisions

[中文](LIMITS.zh-CN.md)

Progress thresholds are observations. By default the model decides whether to continue, change its approach, retry, wait or stop. A threshold does not prove failure or require approval. Finite budgets explicitly selected by the host remain enforceable, along with cancellation, authorization and actual acceptance.

## Work budgets

| Scope | Default hard budget | Advisory default |
|---|---|---|
| Session / ordinary subagent model calls | `max_turns=null` | Session 20 calls; subagent 30 calls |
| Session turn | `turn_seconds=null` | 1500 seconds |
| Run total window | `total_seconds=null` | `total_warning_seconds=10800` |
| Run recovery / repeated failures | `retries=null`, `repeat_limit=null`, `replan_limit=null` | `retry_warning=3`, `repeat_warning=3` |
| Managed member recovery | `retries=null` | Run recovery observations |
| Service reasoning job | `job_turns=null`, `job_attempts=null`, `job_seconds=null` | 40 calls, 3 attempts, 1500 seconds |
| Goal Stop feedback | `block_cap=null` | 8 blocked completion attempts |
| Registered Workflow | `timeout_seconds=null` | 1500 seconds |
| Auto-review | `max_turns=null`, `timeout_seconds=null` | 6 calls, 500 seconds |
| Test runner / recipe | `max_runs=null`, `total_seconds=null`, per-test `timeout=null` | Runner 20 executions, recipe 10 executions |
| Delivery CI repair / CI wait | `ci_fix_limit=null`, `ci_wait_seconds=null` | 2 attempts, 3600 seconds of pending CI |

Run Episode call/time windows yield another Episode of the same Run. They do not fail the Work. Count and elapsed-time notices are spaced further apart as work continues. A bounded transport recovery window returns the error to the owning conversation or job; it does not disable future calls to that model. Service job retries include the previous error and unapplied result in the next input. Unexpected harness program errors retain their diagnostic and stop that job for inspection.

`/review`, `/init` and ordinary Team members use the same advisory subtask defaults; an explicitly selected Session call budget is retained. Workflow output corrections continue by default, with progress feedback after two invalid results; `workflow_output_attempts` selects a hard attempt budget. Feedback contains the latest output and replaces the previous correction instead of accumulating repeated instructions. The accepted workflow schema remains required.

## Validation is a model proposal

Delivery asks for `validation_plan={tests, selections, review, reason}` for the fixed candidate. The model may select authorized full or local checks, static verification, and independent review. `required_tests`, `require_review=true`, explicit Goal predicates, trusted platform checks and merge policy are operator acceptance; they cannot be skipped or recorded as passing without real evidence. A static proposal records an empty test list and `review.mode=not_performed` when applicable. It does not manufacture a pass.

CI diagnosis requires `classification` and a separate `next_action`. Classification describes the observation; it does not determine execution. Actions are `repair`, `recheck`, `reobserve`, `wait`, `defer`, `stop`, `human_required`, `plan_validation`, `reconsider_review`, `proceed`, and `retry_prepare`. Optional `wait_seconds` selects when to reconsider. `plan_validation` keeps the failed evidence and asks for a new validation choice. Blocking review findings return to Lead: it may repair, request an evidence-based second review, or proceed if review is advisory and explicit acceptance is already met. Historical diagnoses without `next_action` retain their legacy interpretation during recovery. Declared fallback models can receive a settled failed request before any hard retry ceiling; model-use history does not blacklist an eligible profile, and retry backoff still applies.

## Configuring limits

Use `runtime_limits` in the external interaction JSON or `RunSpec`, or pass `--runtime-limits '{"file_read_chars":24000,"storage_lock_seconds":60}'`. Python hosts accept `HostConfig.from_files(runtime_limits={...})` and `HostConfig.from_components(runtime_limits={...}, ...)`. Service Run settings are in `run_defaults`; control and service reasoning inherit their host settings. Worker processes and delegated conversations inherit the selected settings. Independent host scopes do not share mutable defaults.

```json
{
  "max_turns": null,
  "total_seconds": null,
  "turn_seconds": null,
  "model_call_warning": 20,
  "runtime_limits": {
    "auto_compact": true,
    "file_read_chars": 24000,
    "run_retry_windows": null,
    "team_members": 8,
    "storage_lock_seconds": 60
  }
}
```

The file remains outside the workspace. Missing fields use defaults; unknown fields, invalid types, negative values and non-finite numbers are rejected. `null` is accepted only by optional budgets. There is no arbitrary upper software ceiling on counts, model output tokens, durations or resource quotas. The selected provider and operating system may impose their own real limits.

`auto_compact=true` maintains the working context by default, using `context_chars` as a reference target. The model can also call `compact`. Failed optional reduction retains history with a diagnostic and a configurable automatic retry interval; it does not claim a summary was accepted. Explicit `auto_compact=false` keeps pressure advisory only. `hard_context_chars` separately selects a local capacity boundary. Confirmed provider overflow uses the existing recovery path. Summary targets, rewrites, read coverage and archive windows are configurable.

## Execution boundaries retained

- Permission scope, revocation, cancellation and explicitly finite grants: continuing would exceed the operator's authorization.
- Task dependencies, explicitly registered workflows/stages, current versions, ownership and commit receipts: these define accepted execution and prevent stale writes or duplicate effects.
- Real required tests, explicit Goals and provider merge policy: a proposal cannot substitute for acceptance evidence. Zero collected tests, cancellation and changed source remain distinct from passing.
- Single network/model connection requests and local commit/cleanup windows: an unreturned call cannot ask the model for another decision. Their configurable timeout returns control or retains the unsettled resource for inspection.
- Container CPU allocation, memory, PID, open-file/file-size and disk quotas; source snapshots, response frames, evidence buffers and archive memory windows: an operation can exhaust the host before the next model turn. These physical budgets are configurable. Concurrent Team members default to 8 to bound retained live contexts and worker resources; `team_members=null` explicitly removes that host admission cap.
- Fixed identifier grammar and real system ranges, such as port 0–65535 and SHA-256 syntax: they describe valid addresses and identities, not progress budgets. Stream chunk sizes are implementation details, not maximum operation sizes.

The configurable `user_input_queue` admission window protects retained host inputs, while `storage_reference_depth` protects recursive document traversal. Increasing either is a host resource choice; `null` removes that local cap. Actual reference cycles remain invalid. Storage inventories, record and backup-manifest memory, candidate-file reads and repository-context excerpts also have configurable windows. Native merge proof has no default commit-count ceiling; `merge_history_commits` explicitly selects one. Short stable identifiers and a selected workflow's versioned output schema retain their published grammar for compatibility. Display snippets and polling chunk sizes do not terminate work or cap its total size.

Output capture is separate from process lifetime. Default Bash and test `output_bytes` retain a bounded prefix and tail while draining further output. Receipts expose received bytes, truncation and tail offsets/references. The middle may be omitted; these artifacts are not a complete log. Only an explicit `hard_output_bytes` stops execution for excess output. Process exit, source stability and cleanup are checked independently.

## Existing state and migration

Stored integer budgets and finite expiries retain their original meaning. New defaults do not widen accepted grants or rewrite history. Set the intended budget to `null` through an authorized configuration change before creating/recovering execution with that policy. Approval and authority windows now default to `null` (valid for the bound version/scope until revoked); existing finite windows still expire. Disabling a child budget does not cancel a finite parent deadline.

## Runtime limit reference

The complete programmatic reference is `RuntimeLimits` in `homebrew_harness.runtime.limits`. The following values are storage/response windows or optional host admission budgets. They can all be set through the same JSON object.

| Field | Default |
|---|---:|
| `transport_attempts` | `3` |
| `control_request_seconds` | `30` |
| `control_socket_seconds` | `5` |
| `transport_delay_seconds` | `0.1` |
| `transport_delay_cap_seconds` | `1` |
| `config_file_bytes` | `65536` |
| `protocol_replay_bytes` | `2000000` |
| `replay_cache_entries` | `32` |
| `replay_cache_bytes` | `8000000` |
| `model_response_bytes` | `8000000` |
| `model_stream_events` | `null` |
| `model_tool_calls` | `null` |
| `tool_pool_size` | `null` |
| `todo_active_items` | `null` |
| `progress_summary_chars` | `32000` |
| `progress_remaining_chars` | `64000` |
| `progress_items` | `null` |
| `todo_items` | `null` |
| `service_sources` | `null` |
| `service_scopes` | `null` |
| `service_schedules` | `null` |
| `service_text_chars` | `null` |
| `service_proposal_chars` | `96000` |
| `review_evidence_paths` | `null` |
| `checkpoint_archive_bytes` | `67108864` |
| `project_guidance_bytes` | `32768` |
| `project_guidance_chars` | `16000` |
| `skill_description_chars` | `300` |
| `memory_recall_chars` | `6000` |
| `skill_files` | `1000` |
| `skill_file_bytes` | `1000000` |
| `memory_record_chars` | `null` |
| `memory_file_bytes` | `1000000` |
| `hook_payload_bytes` | `1048576` |
| `hook_response_chars` | `8000` |
| `query_pattern_chars` | `1000` |
| `query_results` | `100` |
| `query_file_bytes` | `2000000` |
| `query_total_bytes` | `8000000` |
| `git_metadata_entries` | `100000` |
| `git_status_entries` | `200` |
| `git_diff_files` | `32` |
| `git_diff_chars` | `12000` |
| `git_output_bytes` | `64000000` |
| `git_error_bytes` | `65536` |
| `git_diff_file_bytes` | `1000000` |
| `operation_input_chars` | `16000` |
| `operation_read_chars` | `16000` |
| `goal_predicates` | `null` |
| `goal_condition_chars` | `null` |
| `goal_file_bytes` | `16000000` |
| `test_selection_targets` | `null` |
| `test_selection_chars` | `2000` |
| `fixed_argv_items` | `null` |
| `fixed_argv_chars` | `262144` |
| `binding_catalog_entries` | `null` |
| `routing_items` | `null` |
| `routing_reason_chars` | `2000` |
| `routing_prompt_chars` | `4000` |
| `routing_output_chars` | `24000` |
| `platform_response_bytes` | `2000000` |
| `platform_error_bytes` | `8192` |
| `platform_connect_seconds` | `30` |
| `platform_read_seconds` | `60` |
| `platform_write_seconds` | `60` |
| `platform_pool_seconds` | `30` |
| `platform_request_seconds` | `120` |
| `platform_read_attempts` | `3` |
| `platform_error_seconds` | `1` |
| `platform_close_seconds` | `5` |
| `evidence_page_rows` | `100` |
| `communication_poll_attempts` | `null` |
| `communication_poll_warning` | `3` |
| `communication_inline_comments` | `null` |
| `communication_question_ids` | `null` |
| `archive_inherited_ids` | `null` |
| `context_overflow_retries` | `1` |
| `manual_approval_seconds` | `null` |
| `goal_evidence_records` | `null` |
| `goal_evidence_chars` | `1600` |
| `goal_reason_chars` | `null` |
| `approval_reason_chars` | `null` |
| `workflow_state_bytes` | `16777216` |
| `workflow_journal_bytes` | `67108864` |
| `workflow_batch_items` | `null` |
| `workflow_result_chars` | `6000` |
| `workflow_output_attempts` | `null` |
| `workflow_output_warning` | `2` |
| `review_tracked_paths` | `null` |
| `input_text_chars` | `64000` |
| `source_record_chars` | `64000` |
| `source_cursor_chars` | `64000` |
| `source_batch_events` | `null` |
| `source_recovery_events` | `null` |
| `source_control_bytes` | `100000` |
| `benchmark_task_seconds` | `null` |
| `benchmark_model_call_warning` | `4` |
| `artifact_freeze_seconds` | `2` |
| `observation_poll_seconds` | `0.05` |
| `auto_compact` | `true` |
| `auto_compact_retry_turns` | `4` |
| `usage_observations` | `true` |
| `usage_ledger_bytes` | `67108864` |
| `usage_queue_records` | `256` |
| `usage_recent_records` | `128` |
| `usage_close_seconds` | `1` |
| `hard_context_chars` | `null` |
| `run_retry_windows` | `null` |
| `retry_window_warning` | `32` |
| `managed_members` | `null` |
| `managed_member_warning` | `4` |
| `run_prompt_chars` | `2000000` |
| `work_context_chars` | `32000` |
| `summary_restore_chars` | `640000` |
| `summary_chunks` | `null` |
| `summary_rewrites` | `2` |
| `summary_input_chars` | `80000` |
| `summary_target_chars` | `8000` |
| `summary_chars` | `10000` |
| `summary_min_chars` | `300` |
| `tool_result_batch_chars` | `200000` |
| `large_result_chars` | `30000` |
| `file_read_chars` | `10000` |
| `glob_results` | `200` |
| `saved_output_parse_chars` | `64000` |
| `saved_output_links` | `null` |
| `control_frame_bytes` | `262144` |
| `communication_facts` | `null` |
| `communication_questions` | `null` |
| `communication_findings` | `null` |
| `artifact_files` | `null` |
| `artifact_profiles` | `null` |
| `mcp_tool_name_chars` | `64` |
| `subagent_call_warning` | `30` |
| `persistence_attempts` | `3` |
| `lock_seconds` | `120` |
| `persistence_seconds` | `180` |
| `storage_open_seconds` | `15` |
| `storage_lock_seconds` | `30` |
| `storage_transaction_seconds` | `60` |
| `container_control_seconds` | `90` |
| `container_inspect_seconds` | `15` |
| `git_query_seconds` | `120` |
| `retry_delay_cap_seconds` | `60` |
| `review_files` | `null` |
| `review_pages` | `null` |
| `review_file_bytes` | `8388608` |
| `review_total_bytes` | `16777216` |
| `review_page_chars` | `10000` |
| `review_navigation_paths` | `10000` |
| `review_navigation_bytes` | `8388608` |
| `review_navigation_file_bytes` | `2097152` |
| `mcp_selected_servers` | `null` |
| `mcp_selected_tools` | `null` |
| `mcp_server_tools` | `null` |
| `execution_output_chars` | `262144` |
| `team_members` | `8` |
| `mcp_container_connections` | `8` |
| `mcp_discovery_pages` | `null` |
| `mcp_discovery_tools` | `null` |
| `mcp_discovery_bytes` | `8388608` |
| `declared_stages` | `null` |
| `stage_read_paths` | `null` |
| `stage_inputs` | `null` |
| `batch_decisions` | `null` |
| `user_input_queue` | `64` |
| `memory_consolidation_chars` | `20000` |
| `progress_paths` | `64` |
| `progress_versions` | `16` |
| `backup_manifest_bytes` | `4000000` |
| `backup_manifest_files` | `null` |
| `merge_history_commits` | `null` |
| `candidate_file_bytes` | `64000000` |
| `discussion_source_refs` | `null` |
| `assessment_evidence_paths` | `null` |
| `operation_audit_patterns` | `null` |
| `upgrade_candidates` | `null` |
| `delivery_batch_items` | `null` |
| `communication_prompt_chars` | `8000` |
| `storage_record_bytes` | `67108864` |
| `storage_inventory_versions` | `100000` |
| `storage_inventory_views` | `1000000` |
| `storage_reference_depth` | `32` |
| `operation_result_chars` | `24000` |
| `project_context_chars` | `8000` |
| `project_context_paths` | `16` |
| `review_hint_paths` | `16` |
| `service_page_rows` | `100` |
| `service_job_batch` | `32` |
| `execution_slots` | `8` |
| `platform_query_pages` | `null` |
| `blocker_reason_chars` | `null` |
| `blocker_action_chars` | `null` |
| `progress_item_chars` | `null` |
| `progress_ref_chars` | `null` |


## Maintenance defaults and explicit feature choices

Automatic compaction is enabled by default. `context_chars` is its working target; optional failure retains history with a diagnostic and spaces automatic retries using `auto_compact_retry_turns`. The model can call `compact` immediately or the host can choose `auto_compact=false`. An explicit `hard_context_chars` remains a separate capacity contract. Provider-confirmed overflow uses the existing recovery path.

Usage metadata recording is enabled independently of verbose output. It records model/request/provider IDs, purpose, Work/Run/job/phase, input/output/cache/reasoning tokens and completeness, without prompts, source, credentials or endpoint addresses. Explicit `usage_observations=false` disables persistence while retaining counters. Observation file, queue and recent-memory windows are configurable; observation loss is reported without stopping the model. Provider billing still requires reconciliation with actual prices.

Optional modules and MCP scope, memory writes, detached lifetime, host execution mode, demonstration workflows, automatic public actions and independent incident diagnosis remain explicit choices. They affect authority, lifecycle, external effects or additional model jobs. `details=false` keeps output concise and does not disable usage collection. False lifecycle/status fields are observations, not disabled features.
