# 托管仓库

[English](HOSTING.md) | **简体中文**

内置 host-project Skill 引导用户部署受监督的仓库维护 Service。在项目 checkout 启动默认交互：

```sh
homebrew-harness
```

然后告诉它：

```text
帮我托管这个项目：回应 Issue、审查贡献，并通过 PR 开发新功能。
```

也可以明确要求使用 host-project。Skill 随安装包分发，与工作区 Skill 一起出现在目录中，无需另行安装。Python 调用者显式传入自定义 SkillLoader 时仍保留其选定目录。

## 配置对话

已有仓库先调研代码，自动生成项目理解和维护／配置草案，在创建有效配置前由用户 review。只对阻挡读取代码的仓库／访问问题提前提问，其他缺失选择在统一 review 时补齐。建议半天收件和汇报一次，每周审查代码。

## 基于仓库生成草案

README／产品文档、公开接口与代表性实现、测试／构建／CI 定义、贡献指南、PR 模板和路线记录提供依据。草案覆盖用途、已实现能力、架构、当前缺口、验证策略、建议维护目标／排除事项、写范围、审查重点和写作约定，用户明确偏好优先。

记录所检查分支／commit 和 checkout 状态，重要结论区分观察事实、有依据推断、建议默认值及未知项。文档中的未来功能不当作已实现；存在测试不称已通过，未实际观察的 CI／分支规则保留为待验证。调研阶段不为发现配置而执行项目脚本或测试。

用户统一 review／修订生成的内容，只补充仓库无法提供的部署／凭据引用、模型偏好和授权选择。接受后确定项目方向与配置；基线发生实质变化时更新相关草案，再生成有效私有配置并验证。草案待 review 时不启动 Service，review 也不替代之后的原生工具或公开动作审批。

使用专用执行 checkout、独立私有控制目录，以及位于两者之外的配置文件。运行时安装在被维护 checkout 之外。提供凭据的**文件路径和变量名**，真实 token、地址及提交身份由用户填写到属主私有外部文件；发布凭据与模型／开发环境分开。

配置启用仓库收件、多轮 Issue 讨论、外部 PR 审查和 PR 优先交付。默认开发提案和公开动作需要明确决定，由维护者在平台合并。用户可预授权已有窄范围开发类别、审查分支／PR 发布及常规澄清回复；正式 Review 和 Service 合并保留明确授权。

## 定制部署

告诉模型所需的模型分工、提交身份、PR 格式、语言、代码习惯和其他要求。Skill 会将每项选择映射到实际配置，在部署前说明限制；已给出的选择优先于推荐默认值，新增模型先登记到私有目录再使用。

| 职责 | ServiceSpec 字段 |
| --- | --- |
| 计划、部署者对话及默认推理 | maintenance.model_profile |
| 编码 | run_defaults.model_profile |
| Issue／社区回复 | communication.model_profile |
| 外部 PR 审查 | communication.review_model |
| 自身候选审查与 PR 草案 | delivery.review_model |
| 定期代码审查 | operations.audit_model |

字段值为宿主模型别名。默认 Service 推理任务共用一个选择，目前没有逐项 triage／report／conversation 独立模型设置。未填写的角色覆盖使用原有回退模型，Skill 会在汇总中展示。

Git author／committer 使用 delivery.author_name_env／author_email_env；push 认证另行配置，GitHub PR 作者由 Delivery API 凭据决定。公开回复／Review 的作者来自 Communication 凭据。不同身份可使用不同私有凭据文件，修改 Git 姓名不会改变 PR 作者。

模型偏好持久保存在 maintenance.instructions，例如：

```json
{"instructions": {
  "development": "遵循项目类型标注约定，避免无关重构。",
  "discussion": "简洁使用社区语言回复，提出具体问题。",
  "review": "提供 PR title/body，正文含 Summary、Changes、Validation。",
  "report": "汇总修改、待决定事项和下一步。"
}}
```

公共指引使用 common，每项任务只收到 common 与对应条目；编码使用 development，自身 PR 描述使用 review。外部 PR 审查和 Issue 回复共用 discussion，可通过条件文字区分格式。完整任务名见 [Services](SERVICES.zh-CN.md)。

写作格式属于模型指引，没有强制模板渲染器。自定义 PR 草案沿用 publication 提案与准确正文审批，即使固定分支／PR 动作已预授权也如此；目前自动关闭 Issue 的关键词会被拒绝。Skill 将每项要求标记为已支持、模型指引、待补信息或不支持，并解释替代方式，不静默忽略无法满足的必须条件。路径、测试、审批等强制约束使用已有结构化配置字段。

交付执行宿主登记的固定测试。外部 PR 可只做静态审查，执行第三方 PR 测试需要容器 TestRunner。目前原生 Service 部署面向受支持 Unix 宿主，Windows 客户端可通过同一 HTTP 接口监督。

## 验证与部署

模型准备私有配置并执行：

```sh
homebrew-harness service --cwd CONTROL_DIR validate --spec SERVICE_JSON --host-config HOST_JSON
homebrew-harness service --cwd CONTROL_DIR start --spec SERVICE_JSON --host-config HOST_JSON --detach --request-id DEPLOYMENT_ID
```

把标记替换为部署实际路径和标识，并正确引用路径。验证复用宿主／Service 配置合同，检查文件位置、凭据分离、模型别名和固定测试，不调用模型或平台、不执行测试、不创建运行状态。平台权限、Git remote、分支规则、Docker／镜像可用性仍需运行验证。前提或宿主执行批准尚未具备时，模型交付配置和剩余步骤。

启动后保存 Service ID。模型检查实际 owner 与收件读取结果，再提供该部署的命令。OS 开机启动／重启单独按用户要求配置。

## 监督项目

使用 `homebrew-harness service --cwd CONTROL_DIR attach SERVICE_ID` 连接 Text UI：

```text
service> status
service> 这个月优先修复兼容性问题。
service> requests
service> approve REQUEST_ID REVISION --decision-id DECISION_ID
service> work list
service> reports
service> pause execution
service> resume
service> exit
```

普通输入讨论方向，work submit 请求开发，work steer 调整活动工作。请求绑定准确对象和版本，通过审批命令决定。exit 断开 UI，detached Service 继续；stop 才关闭。停止后用 `service --cwd CONTROL_DIR start SERVICE_ID --detach` 重启。

Service 发现 Issue 和 PR 版本，多轮讨论不清晰需求，开发已接受的范围内工作，验证候选、开 PR、跟进审查／CI，并观察维护者合并，核验实际集成后完成交付。一个 Service 使用一个串行工作区。

具体配置和控制见 [Services](SERVICES.zh-CN.md)、[Communication](COMMUNICATION.zh-CN.md)、[Delivery](DELIVERY.zh-CN.md) 和[运维](SERVICE_OPERATIONS.zh-CN.md)。工作区 Skill 放在 skills/NAME/SKILL.md，名称与内置项保持不同，冲突名称会被排除，不静默替换内置项。
