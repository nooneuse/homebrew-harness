# 仓库交付

当前预算默认值与迁移见[运行预算](LIMITS.zh-CN.md)。

[English](DELIVERY.md)

Service 的可选交付功能把已完成开发的工作组织为固定 Git 候选、本地质量检查、审查分支和 PR。默认等待项目维护者通过平台合并；可选模式允许服务在部署者批准具体计划后调用平台原生合并。运行时核对实际合并内容，再完成对应版本的 Work。

## 配置与合并责任

在仓库外的 repository_maintenance ServiceSpec 中增加 delivery，完整示例见[英文文档](DELIVERY.md#configuration)。连接值、发布凭据和提交身份保存在项目与控制目录之外的属主专用文件中，配置只保存环境变量名。

| 模式 | 合并者 | 服务行为 |
| --- | --- | --- |
| manual，默认 | 平台允许的项目维护者 | 开 PR、跟进反馈、观察合并并核验；不创建服务合并审批请求 |
| service_approved | 已配置的服务身份 | 准备固定合并计划，取得部署者批准后执行与查证 |

默认配置为 `"merge": {"mode": "manual", "poll_seconds": 300}`。服务执行可用 `"merge": {"mode": "service_approved", "method": "merge", "poll_seconds": 300}`；GitHub 策略包括 merge、squash 和 rebase。method 只用于服务执行，manual 不替人员选择策略。服务正常交付只发布审查分支，不直接推送目标分支。

PR 的 Review Approve 和 Service 合并授权分别管理。reviewer_ids／required_approvals 声明可信且对应当前 head 的审查；请求修改或批准不足时等待。默认不额外要求审查者，也不推断 owner 身份，应按项目和平台规则配置独立批准要求。

服务执行还需要平台原生合并能力，以及不可绕过的 PR／当前基线 CI 规则。适配器检查生效 rulesets 和旧分支保护；规则不可读、策略不可用或必须入队时明确等待。GitHub 的 sha 参数绑定 head，不提供目标基线的原子参数，服务端集成门禁仍属于合并合同。

mode=isolated 使用真实 ContainerBackend 和固定镜像；开发范围使用文件工具和已登记测试，不能启用宿主 Bash、可执行 MCP、后台命令或 worktree 命令。发布凭据与模型／开发凭据分离。mode=local 用于本地 bare Git、loopback 平台和 synthetic Token 的离线演示，不能替代 OS 隔离。

## 候选与实际结果

Run 开始前记录输入基线，收尾后把授权差量应用到目标基线；CI 修复使用上一候选。已有的无关工作区改动不会被打包。

候选固定 commit、tree、文件清单和目标基线。登记的测试、原始 Goal 硬条件和独立模型审查检查这份内容。审查可通过 `publication={title,body}` 提供 PR 标题和正文。默认 `auto_publish_pr_text=false`，将审查或发布任务生成的文案用于正式 PR 仍需另行批准。现有草稿 PR 流程与固定摘要的预授权不变。

设置 `delivery.auto_publish_pr_text: true` 时，`auto_actions` 必须包含 `ensure_change_request`。若 `require_review=true` 且未提供发布文案，发布任务可根据已接受的需求、审查和验证事实起草。交付随后可用审查提供或发布任务起草的文案自动创建非草稿 PR。此设置不授权合并、关闭 Issue 或放宽质量与范围门禁。

```text
完成开发 → 固定候选 → 测试／Goal／审查 → 审查分支与 PR
         → CI／Review → merge_ready
         → 人员合并或批准后的服务原生合并
         → merged_verifying → delivered → 工作区同步
```

merge_ready 是可以长期保留的等待状态，不消耗模型调用，也不因 CI 执行窗口到期而终止。merged_verifying 保存已经合并的事实，表示内容核验尚未完成；delivered 确认这个版本的实际集成回执。Work 的 review 只表示本地开发完成。

实际结果 commit 可以与被审查 head 不同。merge 检查父提交／目标历史与合成 tree；squash 检查已接受差量及结果；rebase 使用有界源提交范围与内容重放。提交说明或 patch-id 相同不能单独证明交付。结果需要位于登记目标的历史中，目标后续前进不要求 tip 仍等于本次结果。

固定的实际结果重新检查测试和原始 Goal。内容证明不完整或 CI／Review／质量条件不成立时进入 verification_blocked，保留合并事实；旧需求回执不能完成新 Work。核验失败不自动回滚主分支，也不重发合并。

## 反馈与基线刷新

CI 绑定当前 head 和可信应用。诊断区分代码、基线、偶发、基础设施和未知失败；代码修复复用原生 Run、原范围、有效 Work 授权和累计尝试，更新同一 PR 后重新检查。默认 `ci_fix_limit=null`、`ci_fix_warning=2`，修复和重测次数作为进度观察，由模型决定继续、调整方案或请求帮助；明确配置整数 `ci_fix_limit` 时才执行次数硬上限。即使在 `approval_review.kinds` 中启用了 `ci_repair`，超出上限的额外修复尝试也须由操作者明确批准。

冲突修复 Run 会在 `.harness/service-inputs` 获得只读 Git 源码快照索引，绑定确切目标基线、原候选 head 和报告的冲突路径，提供 blob 标识、校验值与可读文件位置。模型使用现有文件工具比较版本，工作草稿继续可编辑；缺失或因预算省略的证据交由模型判断。快照不改变范围，也不授予发布权限。

默认采用固定 Git 输入基线开发（`base_update="manual"`）。准备候选时使用 Work 记录的输入 commit；输入未记录 Git commit 时，在首次准备时确定基线。候选测试与独立审查对应确切的代码树。目标分支推进不会改写开发源码，也不会作废这些固定版本的回执。PR 发布与最新目标分支集成属于不同阶段。

默认的 manual 策略下，head 或目标基线变化进入 integration_required，仅记录观察并保留已发布 head、候选和验证历史，不自动刷新或创建冲突修复 Run。显式 `delivery refresh` 授权后续一次集成准备。设置 `base_update="automatic"` 可以主动选择最新目标基线准备和目标变化后的自动刷新。已发布 head 的真实 CI 失败仍走模型研判与既有修复授权。合并动作仍要求当前目标、CI、审查和平台策略证据。显式 delivery refresh 可以把已接受差量与最新基线合成，或仅在内容相同的确定性集成条件下接纳平台更新的分支。它保留 PR 和原条件，重新测试与审查；其他新代码需要 Work 决定。服务 rebase 刷新可通过绑定确切旧 head 的条件租约替换其审查分支，目标分支仍不可直接写入。

### 定期集成评估

设置 `base_update: "periodic"` 保持固定开发基线，仅对已发布且观察到集成风险的 PR 定期评估。平台仍认为可合并的 PR 不因为目标分支前进而自动重做候选；严格的当前基线 CI 等平台门槛仍然生效。`periodic` 按部署选择，`manual` 仍是默认值，`automatic` 的行为不变。

可选的 `periodic_integration` 配置包含 `assessment_seconds`（默认 21600）、`min_integration_seconds`（21600）与 `max_deferral_seconds`（86400）。首次未解决风险的时钟不会被新的目标提交重置。到期在临时 Git 仓库检查固定 head/target 的实际冲突，再向模型提供冲突路径、CI、审查、反馈与上次决定。模型可以选择集成、有理由地延期，或给出具体阻塞；逾期后不能继续延期。其他合并条件已齐备、CI 诊断确认基线阻塞或操作者明确请求时，可提前评估；调度仍遵守普通集成间隔。显式授权的 `delivery refresh` 是独立入口。

执行时固定已评估的目标 SHA。目标分支继续前进不替换本轮计划；目标历史被改写且不再包含选定基线、或远端源 head 被别人更新时，不覆盖对方代码。后续复用现有验证、冲突修复授权和发布流程，不因此增加发布或合并权限。`delivery show` 的 `integration_schedule` 展示风险时间、决定和下次评估；`delivery assess-integration CHANGESET_ID`（Python：`service.assess_integration(id)`）可提前评估，或在具体阻塞消除后重新评估，不会立即刷新或合并。

未合并的关闭保存为 closed_unmerged；重开由平台事实观察。源分支在合并后删除不会丢失已记录 SHA。暂时的 PR／CI 读取故障保留状态并退避读取，不重新合并，也不持续调用模型。

## 操作者入口

CLI、Text UI、同步与异步 Python 共用认证 HTTP 控制。PR 文案起草或 CI 研判返回 `human_required` 时，`service.requests()` 会列出绑定当前候选和阶段的 `delivery_information`。用 `service.decide(request, "answer", text="...")` 或 CLI 的 `answer` 回答。答复会追加到 Work 的执行上下文，服务随后重新起草 PR 文案或启动新的 CI 研判；它不会修改需求、批准发布、授权修复或豁免检查。发布阶段仍可通过 `work.context(...)` 补充说明；重新起草后，旧问题不再待答。

```sh
homebrew-harness service delivery SERVICE_ID list
homebrew-harness service delivery SERVICE_ID show CHANGESET_ID
homebrew-harness service requests SERVICE_ID
homebrew-harness service answer SERVICE_ID REQUEST_ID REVISION "说明预期行为" --decision-id clarify-1
homebrew-harness service delivery SERVICE_ID inspect ACTION_ID --request-id inspection-1
homebrew-harness service delivery SERVICE_ID refresh CHANGESET_ID --request-id refresh-1
```

Text UI 中省略命令前缀与 SERVICE_ID。delivery prepare 准备当前已开发工作，正常维护会自动进行。service_approved 下，delivery plan CHANGESET_ID merge_change_request 准备登记计划，不直接授予执行权。delivery request ACTION_ID 更新未执行或明确失败动作的请求。公开评论仍单独绑定目标及正文批准。

Python 复用 service.delivery、plan_delivery、inspect_delivery、refresh_delivery、request_delivery、batch 和 decide_batch。模型提议和普通聊天不等于批准；具体示例见[英文控制说明](DELIVERY.md#operator-controls)。大记录与完整候选历史通过 service.record("changesets", id, offset=..., limit=...) 分页读取。

## 恢复、同步与退休

动作 journal 保存受理、尝试与原 request_id。响应丢失保持 unknown，只读查证确认后才提交结果；PR 仍 open 不能证明未执行。只有明确确认没有执行后才申请新批准。人员与服务竞争完成同一固定结果时只提交一次，并记录真实 actor。

暂停、停止和撤销阻止新的服务写入，迟到事实不恢复暂停状态。实际操作未结束时保留 owner／lease；普通等待不占着测试进程或反复调用模型。

同步使用已验证结果和原逐文件条件，保留本地改动。冲突与远端已经交付的事实分别保存，不再次合并。待合并、待核验和待同步引用保护原始及实际证据；完成后的候选、刷新历史和集成产物复用原退休机制，回执身份继续可查。

## 演示与平台

```sh
python examples/delivery_demo.py --output /tmp/harness-pr-demo
python examples/delivery_demo.py --output /tmp/harness-service-merge-demo --service-merge
```

使用新的输出目录。分别模拟维护者合并和明确批准后的服务合并，覆盖 CI 修复、复用 PR、重启、实际结果核验、独立评论批准和资源关闭。真实 Git 合并由测试平台一侧构造，产品代码不推主分支，也不调用付费模型。

当前适配 GitHub，其他平台接入同一观察／固定计划／回执合同。原生队列或 stacked PR 执行、release/tag 和 Issue 关闭仍是独立能力。升级部署前先收尾或查证旧直接目标动作，原批准不转换为原生合并授权。项目的日常贡献流程见[贡献指南](../CONTRIBUTING.zh-CN.md)。

暂停所有 Service 活动并等待准备结束后，可通过 `service.configure_base_updates("manual", request_id="fixed-base-1")` 或 `homebrew-harness service delivery SERVICE_ID base-updates manual --request-id fixed-base-1` 修改调度模式。现有候选、冻结回执、动作授权与 Work 执行范围保持有效。此设置不会取消已经启动的冲突修复；延后该次集成时应显式暂停对应 Work，保留草稿和历史。暂停不代表已完成或已合并。
