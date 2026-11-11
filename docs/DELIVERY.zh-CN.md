# 仓库交付

[English](DELIVERY.md)

Service 的可选交付功能将已完成的开发工作串接到固定 Git 候选、本地质量检查、审查分支、变更请求、远端 CI 和操作者批准的目标分支更新。它复用原生 Run、TestRunner、Goal、模型目录和 Service 控制 API。

## 配置

在仓库外的 `repository_maintenance` ServiceSpec 中增加 `delivery`，配置示例见 [英文文档](DELIVERY.md#configuration)。真实连接值、发布凭据和提交身份保存在项目及控制目录之外、仅属主可读的独立凭据文件中；声明只保存环境变量名。

`mode="isolated"` 使用真实 ContainerBackend 和固定镜像验证候选。开发范围可以使用文件工具和已登记的测试，不能启用宿主 Bash、可执行 MCP、后台命令或工作树命令。发布凭据与模型和开发凭据分离。`review_model` 可以选择模型目录中单独的审查模型；私有 CA 使用 `ca_file_env`。

`required_tests` 指定宿主测试配方；`required_checks` 和 `check_app_ids` 指定远端必需检查及可信应用。`auto_actions` 仅支持宿主预授权的审查分支发布和变更请求创建/更新，目标分支更新及公开评论另行批准。`auto_ci_fix` 和 `ci_fix_limit` 控制有限代码修复。

`request_seconds` 限制单次外部操作，`gate_seconds` 限制一次交付推进；授权期限、CI 轮询和总等待期限独立。费用仍由模型提供商管理。

`mode="local"` 用于无密钥离线演示：只接受工作区和控制目录之外的本地 bare Git 远端、loopback 平台服务及 synthetic Token，可以使用原生 TestRunner。该模式验证交付协议，容器隔离需使用 isolated 模式验收。

## 固定候选和质量

原生 Run 启动前记录输入基线；真实收尾后，把本工作的授权文件差量应用到目标基线，CI 修复则应用到上一候选。工作区已有的其他未交付改动不会被一起打包。输入或目标冲突需要重新确认工作及候选。

候选在私有 Git 仓库中固定 commit、tree、文件清单和目标基线，并创建独立验证目录。固定测试、原始 Goal 硬条件和独立模型审查都检查这份内容。Goal 的文件与测试条件需要描述候选中的实际交付内容，其他工作区通过的检查不能替代候选证据。

```text
完成开发 → preparing → validating → reviewing → review_branch_ready
         → 审查分支 + 变更请求 → ci_waiting → merge_ready
         → 操作者批准目标更新 → delivered
```

审查分支首次发布需要本地测试和独立审查。`merge_ready` 还需要真实已发布 SHA、所有可信应用的必需检查，以及未改变的目标基线。目标动作执行前重新读取 CI，并采用检查快进关系的条件 Git 更新。有必需 CI 的凭据部署还需要平台目标分支保护。

Work 的 `review` 表示开发执行已收尾；`delivered` 确认目标动作完成。PR/MR 状态和公开评论回执分别保存。

## CI 接续

CI 轮询归 Service 所有，可以跨越开发 Run 结束和服务重启。当前候选检查失败后创建诊断任务，`ci-failure-triage` Skill 区分代码、基线、偶发、基础设施和不明失败。

代码修复需要操作者批准或宿主 `auto_ci_fix` 策略、有效原工作授权和剩余尝试。修复在独立目录中创建原生 Run，保留文件范围、原始 Goal 并累计次数。新候选沿原审查分支快进，更新原变更请求，重新进行测试、审查和 CI。其他失败类型保持明确阻挡，等待新的观察或工作决定。

## 操作者入口

CLI、Text UI 和 Python 共用认证 HTTP 控制协议：

```sh
homebrew-harness service delivery SERVICE_ID list
homebrew-harness service delivery SERVICE_ID show CHANGESET_ID
homebrew-harness service delivery SERVICE_ID action ACTION_ID
homebrew-harness service requests SERVICE_ID
homebrew-harness service batch SERVICE_ID create --ids REQUEST_ID --request-id group-1
homebrew-harness service batch SERVICE_ID decide BATCH_ID --approve REQUEST_ID --request-id decision-1
homebrew-harness service delivery SERVICE_ID inspect ACTION_ID --request-id inspection-1
homebrew-harness service delivery SERVICE_ID refresh CHANGESET_ID --request-id refresh-1
```

Text UI 中省略 `homebrew-harness service` 和 `SERVICE_ID`。正常维护会自动创建 ChangeSet；`delivery prepare` 可显式准备当前已完成开发的工作。`delivery request` 更新未执行动作的授权请求；`delivery plan` 创建固定动作，包括明确目标及正文的公开评论。

Python 使用 `service.delivery`、`plan_delivery`、`inspect_delivery`、`refresh_delivery`、`request_delivery`、`batch`、`decide_batch`。完整示例见 [英文文档](DELIVERY.md#operator-controls)。

批次冻结请求成员与版本，逐项复用原审批控制器并保存结果。一项过期或失效不影响其他项。稳定 request_id 支持回执和重放；模型建议及对话文字不会授权执行。

外部动作使用持久 journal。返回丢失后标为 `unknown`；查证读取确切 Git ref 或平台关联标记。查询未确认时继续保留不确定结果。操作者明确确认未执行后，才可发起新批准请求。长记录使用分页查询；`delivery-planning` Skill 用于说明固定候选事实及待决定事项。

## 演示和平台

```sh
python examples/delivery_demo.py --output /tmp/harness-delivery-demo
```

使用不存在的输出目录。场景利用真实本地 Git 远端和确定性模型/平台服务，验证 CI 修复、复用原 PR、服务重启、分组目标审批、目标发布、公开评论回执及资源收尾，不调用付费模型。

当前写适配器实现 GitHub；其他平台接入相同固定交付操作和反馈合同。Release/tag、Issue 关闭、合并队列和外部报告渠道属于后续能力。

宿主可配置可信 `reviewer_ids` 与 `required_approvals` 数量。有效审查决定必须对应已发布 head：请求修改或批准数量不足时，交付进入 `review_waiting`，目标发布前再次核对。普通评论与不可信审查者的意见保留为讨论证据。默认不额外要求审查者，应按项目的审查流程配置这些字段。
