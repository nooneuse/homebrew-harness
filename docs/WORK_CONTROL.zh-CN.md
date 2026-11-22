# Work 源码、续作与 Service 交互执行

[English](WORK_CONTROL.md)

Work 保存已接受需求、拥有的源码版本、Run 历史和真实验收/发布回执。暂停或更换 Run 不会抹掉这些事实。恢复方案由模型提出，应用方案是独立的原生操作。

## 源码查询

`work.sources()` 区分当前草稿、固定候选、历史候选和保存的源码。`read_source(source_ref, path, offset=0, limit=10000, expected_version=None)` 按 Unicode 字符分页；继续读取时携带返回的版本。`compare_sources(before, after, ...)` 比较完整源码树，分页列出文件目录，明确双方来源和差异文件总数。

未提交文件列表只表示相对当前检出版本的变化，不能代表相对旧功能候选的全部变化。源码引用只能属于当前 Work，文件不能越出该源码或读取运行时/凭据目录。

`work.observe_target(execution=...)` 只观察已登记目标一次，执行结果包含固定目标源码引用。观察不会换开发分支，也不会决定整合时机。

## 方案、准备和启动

`work.plan_recovery(instruction)` 默认只读。模型可以返回 text 和具体 action，选择 `resume`、`fork`、`adopt_candidate`、拥有的 source_ref、理由，以及可选整合 `{method: merge/rebase, target_ref}`。`execute=True` 明确请求 Service 在研判完成后应用模型选择；没有动作的文本方案不会启动工作。

`work.apply_recovery(job_id=..., start=False, execution=...)` 应用当前版本方案；`work.continue_from(action, ...)` 应用明确原生选择。换 Run 时完整保存当前草稿，再把选定来源复制到新的拥有工作区，保留需求、权限及历史。`start=False` 保持暂停供核验；“请求启动”不是“已经启动”的证明。

Resume 继续当前绑定与源码，换来源需要明确 fork/adopt。新 Run 不重置既有计数、必需验收或发布权限。源码或有效指令变化后应重读当前事实，不自动替换成另一个版本。

## 整合

整合明确选择固定目标，main 前进只是观察；自动刷新基线是显式配置项。真正的 Git rebase 可能返回冲突，模型修改后调用 `work.control_integration("continue")` 并检查结果，因为后续提交还可能产生冲突。`abort` 放弃该次整合，已保存源码仍保留。这些动作不发布分支，也不合并 PR。

## 执行视图

`work.executions(cursor=0, limit=20)` 分页列出拥有的执行，包括最终候选验收和 Service job。通过 `execution` 查看状态、`execution_output` 读增量输出、`wait_execution` 等待、`stop_execution` 停止，支持输入时用 `send_execution_input`。观察等待到期不停止操作；重启冲突工作之前先查看实际清理和结果。

源码/续作操作支持 `execution={"mode":"interactive","wait_seconds":0}` 或 `{"mode":"one_shot"}`。交互模式快速返回执行引用，一次性模式等待结束；可选 timeout_seconds 是明确的单次操作边界，与观察等待不同。

绑定 Service Work 的开发 Lead 获得对应源码和执行工具，不能因此读取原始平台凭据或任意操作宿主 Git/Bash。明确的必需测试、源码一致性及独立审查仍使用真实证据；可选检查和计划顺序由模型选择。

## 持久化与错误恢复

未完成 Service 阶段保留对话、工具事实和当前指令；完整阶段缓存仍检验版本。收件有独立 worker，长研判不暂停收件。`service.control_job` 控制已受理 job 的暂停、恢复、停止或输入。

执行说明完整保留，历史验收可按需查询。停止态预算迁移在 Run 步骤前持久化受理记录；两数据库之间中断或冲突时，保持停止并检查/重放同一个请求。未完成迁移阻挡启动，部分提交不等于配置一致或授权已续期。

旧的 `service.refresh_delivery(...)` 和发布 `inspect_*` 查证接口在省略 `execution` 时仍返回原结果。传入 `execution={"mode":"interactive","wait_seconds":0}` 可受理为 Service 拥有的执行，通过 `service.executions()`、`execution(id)`、`execution_output(id)`、`wait_execution(id, seconds)` 和 `stop_execution(id)` 观察或停止。Work 拥有的执行使用对应 `work` 方法；其他所有者的引用会被拒绝。立即确认未执行的操作不接受执行模式参数。

显式重试失败 job 会开启新的调用窗口，同时保留累计尝试、旧错误、对话与工具事实。同请求重放已受理的停止态预算修改时，仅将该请求的字段应用到当前配置，保留其他新设置和 Work 说明；它仍绑定原暂停 Run，也不会续授权。

恢复方案、审查等只读提案 job 可观察源码与执行事实，其工具合同不允许在提案时直接改 Git 或停止／输入执行。同一 Work 的开发 Run 和显式聚焦对话使用受作用域约束的控制版本。需要应用恢复选择时，通过原生版本化动作执行，保留决定与执行的边界。
