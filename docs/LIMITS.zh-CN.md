# 运行预算与模型判断

[English](LIMITS.md)

默认的工程阈值只提供进度观察，由模型决定继续、换方法、重试、等待或停止。达到建议值不等于任务失败，也不要求重新审批。部署者明确选择的有限预算仍然执行；取消、授权和真实验收也继续生效。

## 任务预算

| 范围 | 默认硬预算 | 默认建议值 |
|---|---|---|
| Session／普通子任务调用次数 | `max_turns=null` | Session 20 次、子任务 30 次 |
| Session 单轮时间 | `turn_seconds=null` | 1500 秒 |
| Run 总窗口 | `total_seconds=null` | `total_warning_seconds=10800` 秒 |
| Run 恢复／重复失败 | `retries=null`、`repeat_limit=null`、`replan_limit=null` | 恢复与重复失败各 3 次 |
| 声明成员恢复 | `retries=null` | 通过 Run 的恢复观察反馈 |
| Service 研判 job | `job_turns=null`、`job_attempts=null`、`job_seconds=null` | 40 次调用、3 次尝试、1500 秒 |
| Goal Stop 反馈 | `block_cap=null` | 8 次未完成反馈 |
| 注册 Workflow | `timeout_seconds=null` | 1500 秒 |
| Auto-review | `max_turns=null`、`timeout_seconds=null` | 6 次调用、500 秒 |
| 测试运行器／recipe | `max_runs=null`、`total_seconds=null`、单次 `timeout=null` | 运行器 20 次、recipe 10 次 |
| Delivery CI 修复／等待 | `ci_fix_limit=null`、`ci_wait_seconds=null` | 2 次尝试、CI 等待 3600 秒 |

Run 的 Episode 次数／时间窗口用于交接到同一 Run 的下一段，不会因此判定 Work 失败。次数和耗时提醒随进度拉开间隔。单次传输恢复有有限尝试窗口，耗尽后将错误交回所属任务；不会把模型永久标记为不可用。Service job 的后续输入包含上次错误和未提交结果。harness 自身的意外程序错误保留诊断并停止该 job，供检查。

`/review`、`/init` 和普通 Team 成员采用同样的子任务建议默认值，保留 Session 明确设置的调用预算。Workflow 默认继续纠正不符合合同的输出，两次后提供进度观察；`workflow_output_attempts` 可选硬性尝试预算。反馈保留最近输出，并替换旧纠正提示，不反复累积。已接受的 Workflow Schema 仍须满足。

## 验证方案由模型提出

Delivery 为固定候选请求 `validation_plan={tests,selections,review,reason}`。模型可选择已授权的完整／局部测试、静态验证及是否独立审查。`required_tests`、`require_review=true`、显式 Goal、可信平台检查和合并规则属于部署者的验收要求，不能跳过或虚报通过。静态方案保留空测试列表，必要时记录 `review.mode=not_performed`，不会生成假测试回执。

CI 研判要求分别返回 `classification` 与 `next_action`：前者描述现象，后者才决定动作。可选动作包括 `repair`、`recheck`、`reobserve`、`wait`、`defer`、`stop`、`human_required`、`plan_validation`、`reconsider_review` 和 `proceed`。`wait_seconds` 决定何时重新考虑。`plan_validation` 保留已有失败证据并重新选择验证。blocking 审查意见返回 Lead，可修复、提供反证请求复审，或在审查仅为建议且显式验收已满足时继续。旧持久化诊断没有 `next_action` 时保留其历史恢复语义。当前模型调用失败并确认收尾后，可交接到已声明的备用模型，无需等次数硬上限；使用历史不会永久排除该模型，重试退避仍然有效。

## 配置方法

在仓库外的交互 JSON、`RunSpec` 中设置 `runtime_limits`，或者传入 `--runtime-limits '{"file_read_chars":24000,"storage_lock_seconds":60}'`。Python 支持 `HostConfig.from_files(runtime_limits={...})` 和 `HostConfig.from_components(runtime_limits={...},...)`。Service 的 Run 配置位于 `run_defaults`；控制和研判使用 Host 设置。设置传到工作线程、子进程及被委派的上下文，各个 Host 的默认值互不串扰。

```json
{
  "max_turns": null,
  "total_seconds": null,
  "turn_seconds": null,
  "model_call_warning": 20,
  "runtime_limits": {
    "auto_compact": false,
    "file_read_chars": 24000,
    "run_retry_windows": null,
    "team_members": 8,
    "storage_lock_seconds": 60
  }
}
```

缺省字段采用默认值；未知字段、错误类型、负数及非有限数会被拒绝。只有可空预算接受 `null`。次数、模型输出额度、时长和资源配额不再有任意的软件数值天花板；供应商和操作系统仍可能有实际约束。

`context_chars` 默认只提示工作上下文压力，模型可以调用 `compact`。供应商确认上下文溢出时使用既有恢复流程。`auto_compact=true` 显式启用主动压缩；`hard_context_chars` 设置明确的本地输入边界。摘要目标、重写次数、读取覆盖和归档窗口均可配置。可选压缩失败会保留原始历史并返回观察，不会声称压缩成功。

## 保留执行边界的具体理由

- 权限范围、撤销、取消和明确有限的授权：继续执行会超出部署者授权。
- 已声明任务依赖、注册 Workflow／阶段、当前版本、所有权和提交回执：这些定义已接受的执行对象，避免旧判断覆盖新状态或重复执行外部动作。
- 必需测试、显式 Goal 和平台合并要求：方案不能替代验收证据；零测试、取消或源码变化不能算通过。
- 单次网络／模型连接请求、持久化和回收窗口：调用不返回时模型无法做下一步判断。可配置超时用于交回控制或保留尚未确认释放的资源。
- 容器 CPU 配额、内存、PID、打开文件／单文件容量和磁盘配额；快照、响应帧、证据和归档窗口：操作可能在下一轮模型判断前耗尽宿主资源。这些物理预算均可配置。活动 Team 默认最多 8 个成员，用于限制同时保留的上下文及工作线程资源；显式 `team_members=null` 可取消该层受理上限。
- 标识符语法与真实系统范围，例如端口 0～65535、SHA-256 形状：这些定义合法地址和身份，不是进度预算。流式读取块大小属于实现细节，不是操作总大小上限。

可配置的 `user_input_queue` 限制保留中的用户输入，`storage_reference_depth` 保护递归文档遍历；调大属于宿主资源选择，`null` 可取消该层上限。真实引用循环仍属非法。存储盘点、记录和备份清单的内存、候选文件读取以及仓库上下文摘录也都有可配置窗口。原生合并证明默认不再限制提交数量，`merge_history_commits` 可显式设置。短稳定标识符和已选 Workflow 的版本化输出 Schema 保留公开语法，保证兼容。显示摘录和轮询块大小不会终止任务，也不限制任务总大小。

输出捕获与进程寿命分开。Bash 和测试的默认 `output_bytes` 保留有界前缀／尾部，并持续排空其余输出。回执提供实际收到的字节数、截断、尾部偏移／引用。中间部分可能遗漏，因此不能把归档称为完整日志。仅显式 `hard_output_bytes` 会因为输出量而停止执行；退出状态、源码稳定性及资源释放独立检查。

## 既有状态与迁移

已保存的整数预算和有限期限继续有效，新默认值不会扩大旧授权或改写历史。通过已授权配置操作将目标预算设为 `null`，再用对应策略创建／恢复执行。新的审批／工作授权默认期限为 `null`，仅对已批准版本和范围有效，直到撤销；原有限授权仍然到期。取消子预算不会取消明确的父期限。

## 配置字段参考

完整程序接口为 `homebrew_harness.runtime.limits.RuntimeLimits`。以下字段属于存储／响应窗口或可选受理预算，均能通过同一个 JSON 对象调整。

| 字段 | 默认值 |
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
| `auto_compact` | `false` |
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
