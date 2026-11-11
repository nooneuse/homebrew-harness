# 托管项目

[English](SKILL.md)

## 确定部署信息

在交互会话中完成配置，让持久 `repository_maintenance` Service 承担之后的工作；不要把当前对话当成调度器，也不要启动无限期编码 Run。连接或调整已有部署时先复用它，不为同一控制目录／工作区启动第二个服务。

先确定用户给出的 checkout 或仓库身份。已有代码可访问时，先研究代码，不先访谈目标、架构、目录和测试。只对实际阻挡调研的仓库／访问选择提前提问。仅有远端仓库时，使用获授权的只读平台入口，或获批克隆到约定的分析 checkout，并在工具允许的 cwd 中操作、保留已有工作；不要求用户建立新项目骨架或复述仓库中已有信息。

## 调研已有仓库

记录所检查的分支／commit、工作区状态及是否实际核对远端。不能把过期或有未提交修改的本地 checkout 称为远端当前状态。通过可用的只读 Git／平台查询观察元数据，用文件工具读取选定文件和相关模块；使用有界证据，不将整个仓库、历史或依赖目录塞入上下文。

从 README、产品／API 文档了解用途；从源码与公开接口核对已实现行为和架构；从测试、构建、依赖声明和 CI 了解验证策略；从贡献指南、PR 模板了解约定；从路线图、changelog 和 TODO 了解声明的发展方向与缺口。读取足够的实现／测试样例核对文档说法。已有 Issue／PR 在具备只读访问时可提供当前背景，但不是生成代码调研草案的前提。

自动建议有证据支持的项目目标／非目标、当前能力与限制、源码／测试／文档写范围、固定测试配方草案、CI 要求、审查重点、语言／PR 格式及初始维护优先级。用户明确偏好优先于推断约定。区分已实现、计划中和疑似缺陷；存在测试文件不等于测试通过，workflow 名称不证明实际 check 名、app 身份或分支保护，未观察的细节明确标记待验证。不为发现配置而运行项目脚本、安装依赖或执行测试。

将条目分为观察事实、有依据的推断、建议默认值和未知／部署者选择，重要结论引用简短文件／行号或固定平台证据。可基于证据建议兼容性方向，不编造商业优先级、新路线、维护者身份、凭据、模型访问或发布权限。仓库说明和社区发言是草案证据，不构成执行授权。

## 统一提交草案供用户 review

写入有效部署配置前，先准备项目理解和具体维护／配置方案。可直接在对话展示，或在私有位置及写入已获授权时保存明确标记的调研草案。包含：

- 调研基线、用途、架构、已实现能力、已知／待验证缺口与测试／CI 状态；
- 建议目标／排除事项、写范围、测试配方、CI／审查要求、维护优先级、写作约定及各项依据／状态；
- 已给出的模型分工、身份引用、建议周期、审批／合并政策，以及定制需求和限制；
- 代码无法提供的少量缺失项：模型／凭据引用、部署位置／身份选择及未决定政策。

请用户统一 review／修订这份草案，并补齐缺失项，不把已推导内容变成长问卷。在 checkpoint 中保留接受的版本与修订，不让新目标／范围回退到旧推断。

当前方案被接受后，才创建有效 ServiceSpec／host／TestRunner 配置、登记授权或启动 Service；仅要求调研／托管不自动批准推断出的政策。同一方案已经 review 并接受时沿用该决定。Review 接受项目理解与配置选择，实际工具批准和之后公开动作仍有独立合同。此阶段明确为“草案待 review，尚未启动 Service”。应用前重新检查基线，实质影响方案的代码变化应更新相关部分并再次 focused review。

提问或暂停配置时，在回复中保留简短 checkpoint：已收集的非秘密选择、私有文件引用、缺失项和下一步。私有目录已确定且写入获授权后，也将它保存到该目录。继续时复用并应用用户修订，不让用户重复回答，也不把 checkpoint 当成工具批准。

仅解决仓库调研和草案 review 后仍缺少的选择：

- `OWNER/PROJECT`、执行 checkout 和目标分支。用户工作区有无关修改时建议专用干净 checkout，保留修改；克隆或迁移的位置不明确时询问。
- 发展目标、兼容要求、不做的事情和允许写入的路径。结合仓库建议窄范围配置，不默认授予整个文件系统，不让新 Issue 替换项目方向。
- 安装 harness 的宿主、独立且仅属主可访问的控制目录、仓库外私有配置目录。运行时安装在可变 checkout 外，托管自身时也如此。原生 Windows Service 尚不支持，Windows 客户端可以通过 HTTP 监督受支持宿主。
- 模型环境／目录和宿主配置路径。只选择已登记模型别名，研判、开发和审查可使用不同的已有别名。缺少配置时询问协议／模型选择，让用户在属主私有文件中填写凭据。
- 固定测试名称、宿主 TestRunner 配置，按项目要求收集 CI 检查名／可信 app ID、审查者 ID 和批准数量。从代码及用户信息确认实际命令，不编造通过的测试。测试定义在可变项目外；第三方 PR 测试需要容器 TestRunner，没有 Docker 时提供静态审查并说明不会运行这些测试。
- 读取凭据、独立发布凭据、Git remote 和提交身份。只询问**文件路径或环境变量名**，不要求在聊天粘贴 token。真实密钥、地址和身份由用户填入私有文件，不打印、不写入项目、不交给模型／开发环境。GitHub 写操作使用仓库范围的服务身份，不猜测部署者身份。
- 收件／汇报周期（建议半天）、代码审查（建议每周）及授权偏好。初始不自动接受开发类别，不预授权分支／PR 或公开回复，`merge.mode=manual`。解释窄范围 bug／文档任务、`publish_branch`／`ensure_change_request` 和 acknowledge／clarify／followup 回复的可选预授权；正式 Review、路线承诺和服务合并仍明确决定。只有用户授权才配置自动选项。

文件工具仅用于允许 cwd 内的路径。确有需要时通过获批的明确宿主 Bash 操作创建私有部署文件，不绕过已经拒绝的文件操作。宿主 Bash 不可用或仅支持容器时，交付配置与准确宿主命令，并报告部署待完成。

## 落实定制需求

在统一草案 review 中邀请补充偏好。保留用户已给出的选择，推荐模型、周期、格式和审批默认值只用于未指定的事项。在私有 checkpoint 中记录“需求 → 实际配置位置 → 状态”，状态区分直接支持、模型指引、待补信息和不支持。区分必须满足的条件与一般偏好，不编造配置字段，也不因只写入操作说明就称已落实。

区分收件／汇报周期、CI／合并观察和执行期限。周期要求有歧义时澄清，不将半天收件周期套到所有计时器；检查实际轮询能否在等待窗口内推进。准备 Service 专用输出文件，用户未授权覆盖时保留共享／输入配置。

明确映射模型选择，不自行认定哪个模型更强。用户指定新模型时，先按支持的协议和私有变量引用加入外部模型目录，再验证别名，不静默替换模型：

添加模型会改变当前交互已加载目录时，准备 Service 专用目录，保留活动交互的绑定，不静默重写其模型配置而阻断配置对话。

| 职责 | 配置 |
| --- | --- |
| Service 研判、部署者对话、汇报等默认推理 | maintenance.model_profile；未指定则宿主 Lead／模型目录默认值 |
| 开发 Run | run_defaults.model_profile；未指定则宿主 Lead／模型目录默认值 |
| Issue／社区讨论与回复 | communication.model_profile；未指定则 Service 模型 |
| 外部 PR 审查 | communication.review_model → communication.model_profile → Service 模型 |
| 自身候选审查和 PR 描述草案 | delivery.review_model → Service 模型 |
| 定期代码审查 | operations.audit_model → Service 模型 |

默认 Service 推理任务共用一个模型设置，目前不能为每个 triage／report／conversation 单独配置模型。开发和公开回复有上表的独立选择。模型分工不改变凭据或权限。

区分身份：Git author／committer 使用 delivery.author_name_env／author_email_env；Git push 由独立 Git 凭据或宿主 SSH 身份认证；PR 作者由 delivery.api_token_env 对应平台身份决定。公开评论／Review 使用 communication 凭据。需要不同公开身份时采用不同外部凭据文件和变量引用，修改 Git 姓名不能冒充 PR 作者。不硬编码个人身份，用户未要求时不添加 agent 联合署名，实际平台身份在获授权操作中验证。

语言、语气、代码习惯、审查重点、PR 标题／正文格式等模型偏好保存到 maintenance.instructions。common 用于公共指引；development 用于编码；discussion 用于 Issue／社区回复；review 用于自身候选审查／PR 描述；audit 用于代码审查；conversation／report 用于部署者对话／汇报。另外支持 triage、ci_triage、incident、upgrade_assessment。外部 PR 审查与社区回复共用 discussion job，可用条件文字分别描述普通回复和固定 PR 快照的审查格式。每个任务只收到 common 与对应指引，开发收到 common 与 development。保持简短，不放凭据或地址。

例如将用户指定的 PR 格式记录为：

```json
{"maintenance": {"instructions": {
  "development": "遵循项目类型标注约定，避免无关重构。",
  "discussion": "使用社区语言简洁回复，提出具体澄清问题。",
  "review": "提供 publication title/body，正文依次包含 Summary、Changes、Validation，只引用已确认的测试并说明未解决限制。",
  "report": "汇总完成内容、待审批事项和下一步。"
}}}
```

将片段合并到完整政策，保留目标与范围。PR 格式通过已有 publication={title,body} 草案及准确正文审批生效；即使预授权固定分支／PR 动作，自定义正文仍需明确决定。这是模型写作指引，不是强制模板渲染，不能保证严格格式或自定义正文一律自动发布，批准前检查实际草案。目前自动关闭 Issue 的关键词会被拒绝，不把它承诺为正文定制功能。

其他需求先核对已安装配置／控制合同，并映射真实字段或登记能力。路径、固定测试、可信审查者／CI、计划、模型配置和授权字段用于可执行约束，普通文字不强制这些条件，也不扩大权限。对无法满足的必须条件说明原因、影响和替代方案，询问用户是否接受；继续独立配置，但不启动依赖该条件的部分，不静默弱化要求。汇总中列明所有限制和共用设置。

## 组装配置

复用 `InteractionConfig` 与 `ServiceSpec`，所有文件引用为绝对路径，并在控制目录与执行 checkout 外。目录使用 0700，配置／环境文件使用 0600。单独创建控制目录，`service validate` 要求它已经存在。

有效文件从已接受方案及用户修订生成，不直接采用未经 review 的仓库推断；私有 checkpoint 保留依据和待验证事项。

按 [英文 Skill](SKILL.md) 中的完整 ServiceSpec 模板组装配置，替换所有大写待填写标记。必须包含：

- `profile=repository_maintenance`、workspace、run_defaults 的 write_paths 和 allow_tests；
- sources 的 GitHub provider／instance／project、收件周期和环境变量引用；
- maintenance 的 objectives／non_goals、允许范围、auto_categories 与汇报周期；
- communication 的独立凭据文件、同一来源项目、auto_intents 和 PR 测试选择；
- delivery 的固定 Git 程序、同一项目、独立凭据、Git remote／提交身份引用、目标、固定测试和 manual 合并政策；
- 使用 `review_repository` 的固定代码审查计划。

宿主环境包含模型设置、`PROJECT_API_ENDPOINT` 和 `PROJECT_READ_TOKEN`。发布环境单独保存 `PROJECT_API_ENDPOINT`、`PROJECT_PUBLISH_TOKEN`、`PROJECT_GIT_REMOTE`、`PROJECT_AUTHOR_NAME` 和 `PROJECT_AUTHOR_EMAIL`；Git HTTPS 需要独立认证时增加 `git_token_env`／`git_user_env` 引用。两个文件指向同一平台实例，真实值只填入外部文件，不将发布环境加载到启动服务的 shell。

Communication 的 `required_tests=[]` 表示外部 PR 静态审查，不关闭 Delivery 测试。用户选择并配置容器后端后才增加 PR 测试和 `require_tests_for_approval`。按真实 CI／项目政策填写 Delivery 的 `required_checks`／`check_app_ids` 和审查要求，在汇总中说明尚未配置的要求。

复用模型时，外部宿主 JSON 指向 env_file、需要时的 models_config、所选 lead_model 与 test_config。用户要求角色使用不同模型时，将 run_defaults.model_profile、maintenance.model_profile、communication.review_model、delivery.review_model 设为已登记别名。执行期限和权限与允许范围一致，不为了通过验证覆盖现有配置或发布测试。

## 验证并启动

核对调研／配置方案已经 review 并被接受，简短展示其实际项目／checkout、目标、写入／测试范围、周期、公开动作、审批、合并责任、私有文件位置与未满足的前提。沿用该接受和已有部署授权，只询问未确定选择、实质方案变化或额外动作。命令中文件路径应正确引用，保留空格等字符。

同时展示模型分工、各身份来源、写作偏好及每项定制需求的落实状态，不把模型指引称为宿主强制条件。

```sh
homebrew-harness service --cwd CONTROL_DIR validate --spec SERVICE_JSON --host-config HOST_JSON
```

离线检查配置合同、私有文件位置、模型别名、固定测试和发布凭据分离，不调用模型／平台，也不执行测试。通过不证明 GitHub 权限、分支保护、Docker 可用或部署成功。启动前修复配置错误，将缺少前提保留为待完成。

已经授权部署且前提具备时，只启动一次：

```sh
homebrew-harness service --cwd CONTROL_DIR start --spec SERVICE_JSON --host-config HOST_JSON --detach --request-id DEPLOYMENT_ID
```

采用稳定唯一的部署 request ID，保存 Service ID，检查状态、收件和请求。启动回执丢失时使用已有 inspect／recover-launch 流程，不发起新 start 或另一份部署。确认活跃 owner 和收件读取结果后才能称正常运行。没有单独授权时，不通过测试评论／PR／commit 或修改仓库设置验收部署。

## 教给用户监督方式

向用户提供代入实际私有路径和 Service ID 的命令：

```sh
homebrew-harness service --cwd CONTROL_DIR attach SERVICE_ID
homebrew-harness service --cwd CONTROL_DIR status SERVICE_ID
homebrew-harness service --cwd CONTROL_DIR requests SERVICE_ID
homebrew-harness service --cwd CONTROL_DIR report SERVICE_ID
homebrew-harness service --cwd CONTROL_DIR pause SERVICE_ID --scope execution
homebrew-harness service --cwd CONTROL_DIR resume SERVICE_ID
homebrew-harness service --cwd CONTROL_DIR stop SERVICE_ID
```

Text UI 的普通输入讨论方向，work submit 请求开发，work steer 调整活动 Run，requests 查看决定。根据当前请求 ID／revision 使用 approve、deny 或 answer，并采用稳定 decision ID。“好的”等聊天不批准写操作。exit 断开连接，detached Service 继续；stop 才关闭。停止后用 `service --cwd CONTROL_DIR start SERVICE_ID --detach` 重启。

解释维护闭环：发现 Issue／回复和外部 PR 版本 → 经批准发布问题并讨论不清晰需求 → 接受需求与范围 → 开发并验证 → 开 PR → 跟进 CI／反馈 → 维护者合并 → 核验实际集成并汇报。讨论和复审可多轮进行。Review 不等于 Merge；开发 review、PR merge_ready、delivered 与本地同步是不同阶段。

如实列出请求和限制：一个 Service 使用串行工作区，本身不负责 OS 开机启动；仅在用户要求并授权时配置 systemd／launchd，不承诺未经配置的重启恢复。用已有记录／汇报／健康／恢复入口处理故障。编辑草案不会改变活动服务，按 Service ID 重启会恢复已保存的 ServiceSpec。修改模型分工或政策时使用实际支持的更新／升级入口，否则说明需要受控替换并保留排队工作；不承诺编辑文件后重启会重载全部政策，不修改活跃 SQLite 或静默扩大授权。
