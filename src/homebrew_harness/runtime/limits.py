"""Configurable storage windows and advisory execution observations."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
import math


def positive(value, name, *, nullable=False, integer=False, zero=False):
    if value is None and nullable:
        return value
    if value is None or isinstance(value, bool) or not isinstance(value, int if integer else (int, float)):
        raise ValueError(name + " must be a finite number" + (" or null." if nullable else "."))
    if (not integer and not math.isfinite(value)) or value < 0 or not zero and value == 0:
        raise ValueError(name + " must be " + ("nonnegative." if zero else "positive."))
    return value


def expiry(seconds, now):
    return None if seconds is None else now + seconds


def expired(value, now):
    return value is not None and value <= now


def remaining(value, now):
    return None if value is None else max(0, value - now)


def reached(value, count):
    return value is not None and count >= value


def minimum(*values):
    values = [v for v in values if v is not None]
    return min(values) if values else None


@dataclass(frozen=True)
class RuntimeLimits:
    transport_attempts: int = 3
    transport_delay_seconds: float = 0.1
    transport_delay_cap_seconds: float = 1
    config_file_bytes: int = 65536
    protocol_replay_bytes: int = 2_000_000
    replay_cache_entries: int = 32
    replay_cache_bytes: int = 8_000_000
    model_response_bytes: int = 8_000_000
    model_stream_events: int | None = None
    model_tool_calls: int | None = None
    tool_pool_size: int | None = None
    todo_active_items: int | None = None
    progress_summary_chars: int = 32000
    progress_remaining_chars: int = 64000
    progress_items: int | None = None
    todo_items: int | None = None
    service_sources: int | None = None
    service_scopes: int | None = None
    service_schedules: int | None = None
    service_text_chars: int | None = None
    service_proposal_chars: int = 96000
    review_evidence_paths: int | None = None
    checkpoint_archive_bytes: int = 64 * 1024 * 1024
    project_guidance_bytes: int = 32768
    project_guidance_chars: int = 16000
    skill_description_chars: int = 300
    memory_recall_chars: int = 6000
    skill_files: int = 1000
    skill_file_bytes: int = 1_000_000
    memory_record_chars: int | None = None
    memory_file_bytes: int = 1_000_000
    hook_payload_bytes: int = 1024 * 1024
    hook_response_chars: int = 8000
    query_pattern_chars: int = 1000
    query_results: int = 100
    query_file_bytes: int = 2_000_000
    query_total_bytes: int = 8_000_000
    git_metadata_entries: int = 100000
    git_status_entries: int = 200
    git_diff_files: int = 32
    git_diff_chars: int = 12000
    git_diff_file_bytes: int = 1_000_000
    operation_input_chars: int = 16000
    operation_read_chars: int = 16000
    goal_predicates: int | None = None
    goal_condition_chars: int | None = None
    goal_file_bytes: int = 16_000_000
    test_selection_targets: int | None = None
    test_selection_chars: int = 2000
    fixed_argv_items: int | None = None
    fixed_argv_chars: int = 262144
    binding_catalog_entries: int | None = None
    routing_items: int | None = None
    routing_reason_chars: int = 2000
    routing_prompt_chars: int = 4000
    routing_output_chars: int = 24000
    platform_response_bytes: int = 2_000_000
    platform_error_bytes: int = 8192
    platform_connect_seconds: float = 30
    platform_read_seconds: float = 60
    platform_write_seconds: float = 60
    platform_pool_seconds: float = 30
    platform_request_seconds: float = 120
    platform_read_attempts: int = 3
    platform_error_seconds: float = 1
    platform_close_seconds: float = 5
    evidence_page_rows: int = 100
    communication_poll_attempts: int | None = None
    communication_poll_warning: int = 3
    communication_inline_comments: int | None = None
    communication_question_ids: int | None = None
    archive_inherited_ids: int | None = None
    context_overflow_retries: int = 1
    manual_approval_seconds: float | None = None
    goal_evidence_records: int | None = None
    goal_evidence_chars: int = 1600
    goal_reason_chars: int | None = None
    approval_reason_chars: int | None = None
    workflow_state_bytes: int = 16 * 1024 * 1024
    workflow_journal_bytes: int = 64 * 1024 * 1024
    workflow_batch_items: int | None = None
    workflow_result_chars: int = 6000
    workflow_output_attempts: int | None = None
    workflow_output_warning: int | None = 2
    review_tracked_paths: int | None = None
    input_text_chars: int = 64000
    source_record_chars: int = 64000
    source_cursor_chars: int = 64000
    source_batch_events: int | None = None
    source_recovery_events: int | None = None
    source_control_bytes: int = 100000
    benchmark_task_seconds: float | None = None
    benchmark_model_call_warning: int | None = 4
    artifact_freeze_seconds: float = 2
    observation_poll_seconds: float = 0.05
    auto_compact: bool = False
    hard_context_chars: int | None = None
    run_retry_windows: int | None = None
    retry_window_warning: int = 32
    managed_members: int | None = None
    managed_member_warning: int = 4
    run_prompt_chars: int = 2_000_000
    work_context_chars: int = 32_000
    summary_restore_chars: int = 640000
    summary_chunks: int | None = None
    summary_rewrites: int = 2
    summary_input_chars: int = 80_000
    summary_target_chars: int = 8_000
    summary_chars: int = 10_000
    summary_min_chars: int = 300
    tool_result_batch_chars: int = 200_000
    large_result_chars: int = 30_000
    file_read_chars: int = 10_000
    glob_results: int = 200
    saved_output_parse_chars: int = 64_000
    saved_output_links: int | None = None
    control_frame_bytes: int = 256 * 1024
    communication_facts: int | None = None
    communication_questions: int | None = None
    communication_findings: int | None = None
    artifact_files: int | None = None
    artifact_profiles: int | None = None
    mcp_tool_name_chars: int = 64
    subagent_call_warning: int | None = 30
    persistence_attempts: int = 3
    lock_seconds: float = 120
    persistence_seconds: float = 180
    storage_open_seconds: float = 15
    storage_lock_seconds: float = 30
    storage_transaction_seconds: float = 60
    container_control_seconds: float = 90
    container_inspect_seconds: float = 15
    git_query_seconds: float = 120
    retry_delay_cap_seconds: float = 60
    review_files: int | None = None
    review_pages: int | None = None
    review_file_bytes: int = 8 * 1024 * 1024
    review_total_bytes: int = 16 * 1024 * 1024
    review_page_chars: int = 10_000
    review_navigation_paths: int = 10_000
    review_navigation_bytes: int = 8 * 1024 * 1024
    review_navigation_file_bytes: int = 2 * 1024 * 1024
    mcp_selected_servers: int | None = None
    mcp_selected_tools: int | None = None
    mcp_server_tools: int | None = None
    execution_output_chars: int = 262144
    team_members: int | None = 8
    mcp_container_connections: int = 8
    mcp_discovery_pages: int | None = None
    mcp_discovery_tools: int | None = None
    mcp_discovery_bytes: int = 8 * 1024 * 1024
    declared_stages: int | None = None
    stage_read_paths: int | None = None
    stage_inputs: int | None = None
    batch_decisions: int | None = None
    user_input_queue: int | None = 64
    memory_consolidation_chars: int = 20000
    progress_paths: int = 64
    progress_versions: int = 16
    backup_manifest_bytes: int = 4_000_000
    backup_manifest_files: int | None = None
    merge_history_commits: int | None = None
    candidate_file_bytes: int = 64_000_000
    discussion_source_refs: int | None = None
    assessment_evidence_paths: int | None = None
    operation_audit_patterns: int | None = None
    upgrade_candidates: int | None = None
    delivery_batch_items: int | None = None
    communication_prompt_chars: int = 8000
    storage_record_bytes: int = 64 * 1024 * 1024
    storage_inventory_versions: int = 100000
    storage_inventory_views: int = 1000000
    storage_reference_depth: int | None = 32
    operation_result_chars: int = 24000
    project_context_chars: int = 8000
    project_context_paths: int = 16
    review_hint_paths: int = 16

    def __post_init__(self):
        nullable = {"benchmark_task_seconds", "benchmark_model_call_warning", "goal_evidence_records", "goal_reason_chars", "approval_reason_chars", "workflow_batch_items", "review_tracked_paths", "source_batch_events", "source_recovery_events", "manual_approval_seconds", "memory_record_chars", "todo_active_items", "progress_items", "goal_predicates", "goal_condition_chars", "test_selection_targets", "fixed_argv_items", "binding_catalog_entries", "routing_items", "communication_inline_comments", "communication_question_ids", "archive_inherited_ids", "model_tool_calls", "model_stream_events", "tool_pool_size", "todo_items", "service_sources", "service_scopes", "service_schedules", "service_text_chars", "review_evidence_paths", "run_retry_windows", "managed_members", "summary_chunks", "saved_output_links",
                    "communication_facts", "communication_questions", "communication_findings",
                    "artifact_files", "artifact_profiles", "hard_context_chars",
                    "review_files", "review_pages", "mcp_selected_servers", "mcp_selected_tools", "mcp_server_tools", "team_members",
                    "mcp_discovery_pages", "mcp_discovery_tools", "subagent_call_warning", "declared_stages", "stage_read_paths", "stage_inputs", "batch_decisions",
                    "user_input_queue", "backup_manifest_files", "merge_history_commits", "discussion_source_refs", "assessment_evidence_paths", "operation_audit_patterns", "upgrade_candidates", "delivery_batch_items", "storage_reference_depth", "workflow_output_attempts", "workflow_output_warning", "communication_poll_attempts"}
        for name, value in asdict(self).items():
            if name == "auto_compact":
                if type(value) is not bool:
                    raise ValueError("auto_compact must be Boolean.")
                continue
            positive(value, name, nullable=name in nullable, integer=not name.endswith("seconds"), zero=name in {"context_overflow_retries", "summary_rewrites", "run_retry_windows", "managed_members", "team_members", "declared_stages", "todo_items", "todo_active_items", "stage_inputs", "stage_read_paths", "batch_decisions"})
        if self.summary_min_chars > self.summary_target_chars or self.summary_target_chars > self.summary_chars:
            raise ValueError("Summary minimum, target and capture window must be ordered.")

    @classmethod
    def parse(cls, value=None):
        if isinstance(value, cls):
            return value
        value = {} if value is None else value
        if not isinstance(value, dict) or set(value) - set(cls.__dataclass_fields__):
            raise ValueError("Unknown runtime_limits fields.")
        return cls(**value)

    def data(self):
        return asdict(self)


_CURRENT = ContextVar("runtime_limits", default=RuntimeLimits())


def current_limits():
    return _CURRENT.get()


@contextmanager
def limits_scope(value):
    token = _CURRENT.set(RuntimeLimits.parse(value))
    try:
        yield _CURRENT.get()
    finally:
        _CURRENT.reset(token)


def advisory(observed, threshold, name, *, unit="calls"):
    if threshold is None or observed < threshold:
        return None
    return {"limit_name": name, "recommended_value": threshold, "observed_value": observed,
            "unit": unit, "enforcement": "advisory",
            "instruction": "This is a progress observation, not a stop or approval requirement. "
                           "Decide whether to continue, change the approach, wait, stop or request help. "
                           "Existing authorization and explicit acceptance remain in effect."}


def observe(messages, metrics, seen):
    """Replace advisory context only on a new exponentially spaced milestone."""
    import json
    from homebrew_harness.context.tracked_history import set_context
    notices = []
    changed = False
    for name, observed, threshold, unit in metrics:
        value = advisory(observed, threshold, name, unit=unit)
        if value is None:
            continue
        bucket = max(1, int(observed / threshold)).bit_length()
        changed |= seen.get(name) != bucket
        seen[name] = bucket
        notices.append(value)
    if changed:
        set_context(messages, "execution_advisories", {"role": "user", "name": "execution_advisories",
            "content": json.dumps({"observations": notices}, ensure_ascii=False)})


def backoff(attempt, initial=1, maximum=None):
    maximum = current_limits().retry_delay_cap_seconds if maximum is None else maximum
    if initial >= maximum or attempt >= math.log2(maximum / initial):
        return maximum
    return initial * 2 ** attempt
