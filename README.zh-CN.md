# homebrew-harness

[English](README.md) | **简体中文**

一个可组合的 Python Agent 运行时，适用于交互开发、局部委派和可恢复的任务执行。它将模型请求、工具、上下文管理和持久状态连接起来。

模型提出动作，运行时执行对应工具，结果成为下一轮推理的依据。Task、Goal、Workflow 和 Agent Team 在同一个循环上组织不同形式的工作。

## 功能

| 范围 | 能力 |
| --- | --- |
| 模型接入 | Chat Completions、Responses、DashScope Generation、Anthropic Messages；按角色选择模型 |
| 基础工具 | 文件读取、写入和编辑；路径匹配；Bash；计划更新；技能加载 |
| 上下文 | 按需技能加载、历史压缩、完整结果归档、可选跨会话记忆 |
| 编排 | Subagent、持久任务依赖图、Agent Teams、后台命令、Cron、Workflow |
| 完成检查 | Goal 与文件、测试条件 |
| 交互 | 执行期间的 steering、排队请求、interrupt 和独立工具审批 |
| 执行环境 | 宿主 Bash；可选 Bash、TestRunner、本地 MCP 容器后端；产物收集与发布 |
| 状态管理 | SQLite checkpoint、持久 inbox、恢复检查、备份和历史维护 |

## 安装

需要 Python 3.10 或以上。容器后端需要 Docker 和配置好的镜像；核心运行时可以在宿主上运行。

在本项目目录执行：

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

基础安装通过 HTTP 支持全部四种模型协议，供应商 SDK 为可选依赖。按需安装：

```sh
python -m pip install -e ".[openai]"
python -m pip install -e ".[anthropic]"
python -m pip install -e ".[dashscope]"
```

新连接默认 `transport: "auto"`，优先使用已安装且受支持的 SDK，否则使用 HTTP。也可以显式选择 `"http"` 或 `"sdk"`。详见[模型连接](docs/MODELS.zh-CN.md)。

安装后提供一个命令和五个子命令：

```sh
homebrew-harness --help
homebrew-harness session --help
homebrew-harness run --help
homebrew-harness service --help
homebrew-harness workflow --help
homebrew-harness admin --help
```

也可以使用 `python -m homebrew_harness` 和相同的子命令。无子命令时进入自动交互入口：新任务选择 Session 或受管理 Run 和所需能力，活动任务的普通输入成为 steering；单次任务和会话专属选项使用显式的 `session` 子命令。

`--cwd` 和 `--env-file` 为公共选项，可放在子命令前后；重复提供时，最后一个显式值生效。其他参数属于对应功能组，通过 `homebrew-harness run --help` 或具体操作的帮助页查看。

## 模型配置

配置放在工作区外。创建默认环境文件：

```sh
mkdir -p ~/.config/homebrew-harness
cp examples/config/.env.example ~/.config/homebrew-harness/.env
chmod 600 ~/.config/homebrew-harness/.env
```

在该文件填写 `OPENAI_API_KEY`、`OPENAI_BASE_URL` 和 `OPENAI_MODEL`。单模型入口使用 Chat Completions，已导出的环境变量优先于文件内容。

多模型和其他协议使用 [模型目录模板](examples/config/.models.example.json)。`connections` 声明协议与环境变量名，`profiles` 声明模型及其能力，`defaults` 指定 Lead 和成员角色使用的配置。

```sh
cp examples/config/.models.example.json ~/.config/homebrew-harness/models.json
homebrew-harness session --models-config ~/.config/homebrew-harness/models.json
```

填写模板引用的环境变量，并按实际模型配置工具、结构化输出和思考能力。Lead 可用 `--lead-model` 选择别名；成员和 Subagent 从宿主登记的配置中选择。凭据与模型接入地址由宿主环境提供。

## 快速开始

在默认交互中提出“帮我托管这个项目”，可使用随包安装的 host-project Skill，收集部署信息、准备私有配置并学习监督仓库维护 Service。详见[托管仓库](docs/HOSTING.zh-CN.md)。

```sh
# 自动交互
homebrew-harness

# 单次任务
homebrew-harness session "阅读项目文件，说明核心模块之间的关系"

# 指定工作目录
mkdir -p /tmp/harness-example
homebrew-harness session --cwd /tmp/harness-example \
  "创建 hello.py，打印 Hello, world!，然后读回确认"

# 恢复最近保存的会话
homebrew-harness session --resume-session
```

工作目录默认为启动命令时所在的目录。`--cwd` 选择其他工作区，会话状态保存在该工作区中。

文件查询以有界结果检查源码，默认跳过 Git、依赖和运行时目录。保留的运行时路径及其大小写变体均由宿主管理；显式完整结果引用按所属上下文读取。

默认 `core` 配置包含基础工具。按需启用可选模块：

```sh
homebrew-harness session --enable subagents --enable tasks
homebrew-harness session --enable teams --enable worktrees
homebrew-harness session --enable background --enable cron
homebrew-harness session --memory-mode recall
```

`--profile full` 启用全部可选模块，记忆默认为 `recall`。`--memory-mode auto` 允许正常用户回合结束后的记忆维护；`off` 关闭记忆调用。

项目附带 [Python 代码审查](skills/code-review/SKILL.zh-CN.md) 和 [文本摘要](skills/text-summary/SKILL.zh-CN.md) 两个技能示例。

## 交互 slash commands

默认交互入口与 `homebrew-harness session` 支持以下命令。宿主直接处理命令，未知命令明确报错；Python API 的文本仍是任务材料，不作为 CLI 控制语法。

| 命令 | 行为 |
| --- | --- |
| `/help` | 查看命令语法。 |
| `/model` | 查看当前模型、协议及已登记别名。 |
| `/model NAME` | 从已加载目录选择模型别名；没有目录时，在现有连接上选择模型名称。 |
| `/reasoning` | 查看当前 reasoning／thinking 设置。 |
| `/reasoning LEVEL` | Chat／Responses 使用 `none`、`minimal`、`low`、`medium`、`high`、`xhigh`；有映射的协议可用 `on`／`off` 设置 thinking，`default` 恢复配置值。 |
| `/compact` | 压缩已完成的历史，保留原始要求、当前计划及完整归档。 |
| `/new` | 已接受工作结束后开始空白上下文，保留模型选择，旧历史仍保存。 |
| `/diff [--base REF] [PATH ...]` | 查看固定 Git 基准与工作区的变更，包含未跟踪文本；带空格路径需加引号。 |
| `/review [--base REF] [PATH ...]` | 使用只读文件／搜索／Git 工具审查有界快照；后续代码变化会将结果标记为旧快照。 |
| `/skills` | 列出可用 Skill。 |
| `/skills show NAME` | 查看 Skill 正文。 |
| `/skills use NAME TASK` | 为明确的任务加载所选 Skill 作为指导。 |
| `/worktree [list]` / `/worktree show NAME` | 查看 Git 工作树及任务归属。 |
| `/worktree create NAME --task TASK_ID` | 将干净 HEAD 工作树绑定到待执行任务；需启用 tasks/worktrees。 |
| `/worktree remove NAME` | 任务完成后删除无执行占用、无改动的受管理工作树，保留分支。 |
| `/memory` / `/memory list` / `/memory show NAME` | 查看跨会话知识及当前 Memory 模式。 |
| `/memory correct NAME TEXT` / `/memory delete NAME` | 原子修正或删除知识条目；自动记忆关闭时也可使用。 |
| `/resume` / `/resume list` / `/resume ID` | 继续暂停任务、列出保留上下文或恢复指定上下文。 |
| `/pwd` | 查看基础工作区、实际 Lead 目录、分支和任务工作树归属。 |
| `/ps` / `/ps show ID` | 查看运行操作、资源和待收尾状态。 |
| `/stop` / `/stop ID` | 暂停当前执行，或请求取消指定的本上下文操作。 |
| `/hooks` / `/hooks show EVENT` | 查看回调顺序和最近失败。 |
| `/agents` / `/agents show NAME_OR_ID` | 查看 Lead、团队成员和子 agent 的模型及任务状态。 |
| `/quit` / `/exit` | 保存并关闭界面，与 `q`、`quit`、`exit` 共用退出流程。 |

Lead 忙碌时，模型和推理设置进入队列，在当前轮次结束后生效。切换保留可读历史和用户要求，归档旧协议的私有 replay，并记录到 Session checkpoint。默认入口也保留交互选择，供后续任务及重新打开时使用；恢复仍要求原配置来源一致。已启动成员和独立绑定的 Workflow 保持各自模型。受管理 Run 维持已声明模型合同，切换使用其原生 handoff。

查询命令不调用模型或任务规划器。`/compact`、`/review` 沿用现有模型请求 deadline；审查快照截断时需缩小路径范围。`/new` 要求已接受工作和 Goal 已完成或取消，旧历史可显式恢复，旧审批不会作为新对话的授权。这些命令不配置凭据，也不改全局模型文件。

`/stop` 无需等待模型回复即可受理。取消请求和资源实际释放是不同状态，可用 `/ps` 查看；效果不明确的操作仍需通过 `/recovery` 核查。Session 暂停后，普通输入继续保存在队列中，重新打开后也保持暂停；完成收尾与核查后使用 `/resume`。指定停止目标使用 `/ps` 的操作 ID。受管理 Run 通过原生控制暂停；独立 worker 的观察会标为 checkpoint 快照。

`/resume list` 列出本工作区的上下文。`session:ID` 加载历史后等待下一条要求；交互任务和 Run ID 则继续原任务。恢复需要原来的配置和模型来源。默认入口先结束当前上下文再选择其他上下文；显式 Session 在执行结束并完成收尾后可切换。退休历史继续使用现有 admin 恢复流程。

Memory 查询和人工维护不会打开自动提取，也不调用模型。Hooks 只提供查看；agent 创建和 steering 继续使用现有工具及 `/steer-to`。工作树按任务绑定，认领任务后自动使用对应目录。`/quit` 与 `/exit` 暂停仍附着的 Run；此前明确请求 detached 的 Run 按已登记的后台生命周期执行。

## 自动交互

在项目目录运行 `homebrew-harness` 并输入需求。宿主为新任务规划执行方式、核验能力目录，再准备选中的 Session 或受管理 Run。每项任务保留原始需求、能力、模型和执行策略；执行期间的普通输入修订当前任务，`/queue TEXT` 创建独立后续任务。 已完成问答作为下一任务的参考，完整内容按需读取归档；原生推理和 replay 状态留在源上下文。

可选宿主设置保存在工作区外：

```sh
cp examples/config/interaction.example.json ~/.config/homebrew-harness/interaction.json
homebrew-harness --host-config ~/.config/homebrew-harness/interaction.json
```

也可用 `HARNESS_INTERACTION_CONFIG` 选择该文件。支持外部 `models_config`、`env_file`、`test_config`、`bash_config`、`mcp_config`、`control_config` 引用。`allowed` 声明允许的可选组件，任务从中选取所需部分。`demo_workflows` 登记内置审查脚本；配置 MCP 服务器目录后可在 `allowed` 中加入 `mcp`。`memory_mode` 独立配置。`managed_bash` 为自动 Run 提供 Bash，调用仍需审批，默认关闭。固定测试保持原有授权策略。

新任务使用一次有界规划请求，并将规划结果作为执行参考。steering 和宿主管理操作不调用路由模型。`prefer_session: true` 跳过路由，固定使用核心 Session；`model_planning: false` 使用核心交互任务，显式 `/managed TEXT` 仍可请求 Run。

| 输入 | 自动宿主行为 |
| --- | --- |
| `/status` | 查看当前任务、请求与真实收尾状态 |
| `/managed TEXT` | 排队一项明确的受管理任务 |
| `/pause`、`/resume`、`/cancel` | 控制当前任务；暂停适用于 Run |
| `/upgrade` 或 `转为持续推进` | 在单 Agent 安全边界把 Session 接续为 Run |
| `/detach` 或 `关闭界面后继续` | 请求受管理任务在关闭界面后继续 |
| `/approve ID REVISION yes/no` | 回答一项精确的持久工具请求 |
| `/answer ID REVISION TEXT` | 回答需求问题 |
| `/grant ID REVISION RUNS SECONDS` | 为固定测试续期指定次数和期限 |

唯一已展示的工具请求也接受完整短回答，如 `允许这次操作`、`拒绝`；混合正文作为 steering 保留。唯一已展示的需求问题接受普通文字回答。测试续期仍需明确边界。管道输入不获得交互审批权限。

首版每个工作区只有一个活动任务。Task／Team／worktree、Cron、未结束的异步工作或不确定操作会阻挡 Session→Run 交接。已选定 MCP 工具的 Session 在 Actor 关闭后可以交接，目标会核验相同的服务器声明和工具 manifest。交接保留原始需求、已知结果、归档引用、观察用量、剩余期限和测试授权的剩余次数；旧 Session 保持记录并停止恢复执行，目标重新获得验证证据。

关闭 attached 界面会有界停止其持有的执行。`/detach` 明确请求独立 supervisor；`detached: true` 是对自动规划的 managed 任务的宿主生命周期偏好。重新打开入口会附着已有 Run。启动以持久 ACK 和执行租约确认；若启动不确定，`/status` 展示 token，核实旧执行结束后用 `/supervisor-recover TOKEN` 封锁旧启动，再用 `/resume` 重试。Run 支持选定的 MCP 工具；Cron 继续用于 Session。

## 受管理运行（无人值守）

`homebrew-harness run` 将一个任务保存为 Run，由独立执行进程按有界 Episode 推进。段落之间接续同一会话；进程恢复时核对 checkpoint 与实际执行状态。Run 的总期限包含等待和重启时间。Run 可以使用声明的阶段和固定成员团队。

```sh
cp examples/config/run.example.json ~/.config/homebrew-harness/run.json
homebrew-harness run --cwd /path/to/project start \
  --spec ~/.config/homebrew-harness/run.json
```

### 控制与恢复

启动后输出 `run_id`。可以从另一个终端管理：

```sh
homebrew-harness run --cwd /path/to/project status RUN_ID
homebrew-harness run --cwd /path/to/project steer RUN_ID "补充当前任务要求"
homebrew-harness run --cwd /path/to/project pause RUN_ID
homebrew-harness run --cwd /path/to/project resume RUN_ID
homebrew-harness run --cwd /path/to/project cancel RUN_ID
```

`supervise RUN_ID` 在监督进程重启后接管可恢复任务。`steer --mode queued` 将输入留到下一 Episode；`--mode interrupt` 请求取消当前可取消步骤。独立任务使用新的 Run。

默认工具覆盖文件、上下文和测试。阶段端点仅在 Run 具有相应阶段能力时加入。`allow_tests` 预授权固定测试 recipe。`enabled` 可以启用 Subagent、后台执行或登记的 Workflow，`tools` 选择其工具。通过 `--env-file`、`--models-config`、`--test-config`、`--bash-config` 和 `--mcp-config` 指定工作区外的宿主配置。

状态包含运行结果、停止原因、心跳、成员结果和收尾状态。`recovery RUN_ID` 查看需要检查的操作；核对效果后，使用 `inspect RUN_ID OPERATION_ID REVISION` 并显式恢复。恢复需等待原执行完成收尾。

模型请求超时使用有限恢复策略。Episode 到期保存后接续；Run 或 Session 期限到期停止。迟到响应会被丢弃，尚未结束的执行继续登记并等待收尾。

取消或最终失败后，未完成 Task／Goal 会在执行与未知效果结清后成为 `abandoned`。已验收结果继续保留。`finalized_at` 表示归属结清完成，此后可在同一工作区启动新的 Run。

### 审批与测试许可

工具需要许可时，Run 保存精确请求并进入 `waiting`。需求问题使用独立的 Attention 请求。查询后按当前 revision 答复：

```sh
homebrew-harness run --cwd /path/to/project requests RUN_ID
homebrew-harness run --cwd /path/to/project approve RUN_ID REQUEST_ID REVISION yes
homebrew-harness run --cwd /path/to/project answer RUN_ID QUESTION_ID REVISION "需求决定"
```

批准后模型通过 `execute_approved` 提交原调用，许可在接受操作时消费一次。Attention 答复作为用户修订进入原任务。

成员的固定测试许可到期或耗尽后，会登记 `test_grant` 请求。为原任务续期时，指定有限次数和期限：

```sh
homebrew-harness run --cwd /path/to/project approve RUN_ID REQUEST_ID REVISION yes \
  --runs 1 --seconds 600
```

续期受 recipe 和 Run 上限约束，成员核验新许可后继续。`write_paths` 指定文件可写前缀，固定测试和 Bash 后端由宿主登记。

反复失败、无效写入和文件状态循环会触发局部重规划，持续无进展时阻塞。`progress RUN_ID` 查看记录；执行结束后，`progress-reset RUN_ID REVISION --reason TEXT` 开启新的重规划周期。

### 阶段与输入

RunSpec 可声明 `stages`，每个阶段有 `key`、`subject`、`depends_on` 和 `acceptance`。阶段验收采用 `judge: false` 的结构化条件，最终交付由 `goal` 描述。参见 [分阶段运行示例](examples/config/run-stages.example.json)。

阶段启用 Task 工具。模型认领任务并通过 `complete_task` 提交产物；宿主验收通过才完成 Task、保存交接并解锁依赖。相关文件或需求变化会使旧验收失效。

`depends_on` 表达先后和验收依赖，`inputs` 声明消费者需要的前驱文件，例如 `"inputs": [{"stage": "api", "paths": ["api.py"]}]`。宿主在消费者认领前交付已验收版本，输入只读，其版本关联阶段证明。

工作树阶段在产物、前驱输入、硬条件和测试声明的范围内保存内容快照。`source_paths` 以相对路径前缀补充仓库内源码依赖，不扩大写权限。`snapshot_limits` 配置 `max_files`、`max_bytes` 和 `max_paths`：Run 中用于阶段快照，recipe 中用于测试输入。范围外仍用元数据和 Git 检查跟踪变化。工作树从完整、干净的 HEAD 创建。

`stages RUN_ID` 查看任务状态，`handoffs RUN_ID` 查看原始要求、验收事实和模型总结。模型可用 `get_handoff` 回读交接。测试凭证保存在 SQLite，跨进程恢复时核对当前代码和 recipe。

### 团队、集成与模型交接

`members` 声明固定名称、角色、提示、模型别名与恢复次数。阶段用 `executor` 指定成员；`isolated: true` 使用独立 worktree，`write_paths` 定义产物归属。成员恢复保留身份、任务和上下文。

成员在待处理输入修订应用后继续。工作段结束但任务未完成时，会形成具体 Lead 待办；状态中的 `member_outcomes` 说明结果。未知效果需要宿主检查。

集成阶段用 `integrates` 引用前驱。生产者停止后，`integrate_stage` 将已验收产物应用到集成 worktree。验收通过且声明 `publish: true` 后，可用 `publish_stage` 发布清单中的产物到 base。`transfers` 查看结果；中断后用 `transfers-resolve` 核对实际文件，再结清关联操作。

`fallback_models` 声明备用别名，`fallback_errors` 指定自动交接的故障类别。宿主也可使用 `handoff RUN_ID MODEL_PROFILE REVISION --reason TEXT`。交接创建新的 Lead 会话，承接原始要求、Task 和证据；成员绑定和 Run 总期限继续有效。同模型的 `resume` 将已处理连接故障的确认与 checkpoint 一起保存，并保留用量观察和期限。

### PR 来源与通知

可选 [Run control 配置](examples/config/run-control.example.json) 订阅已有 GitHub PR 并启用最小通知。配置放在工作区外，填写仓库、PR、允许的 actor／app ID 和环境变量，删除不用的 `github` 或 `notification` 项。

```sh
homebrew-harness run --cwd /path/to/project \
  --control-config /path/to/private/run-control.json start --spec /path/to/private/run.json
```

来源将当前 head 的 CI／review 与允许主体的评论作为参考输入，完整事件可回读。来源内容不授予工具许可。`events RUN_ID` 和 `effects RUN_ID` 查看记录。

通知默认关闭。开启后报告决定请求、阻塞和完成状态。不明确的发送结果保留为 `unknown`；核实后使用 `effects-resolve RUN_ID EFFECT_ID REVISION delivered --reason TEXT`。接续 Run 时使用原 control 配置。

### 导出、备份与演示

`export RUN_ID --output FILE` 导出要求与状态。`backup RUN_ID --output DIR` 在监督停止且完成收尾后备份数据库和附件。`verify-backup DIR` 检查清单，`restore-backup DIR --output NEW_DIR` 恢复到新的检查目录。执行前由宿主核对工作区和 Git 身份。`min_free_bytes` 指定运行时磁盘余量，默认 128 MiB。

`search_text`、`git_status` 和 `git_diff` 提供有界内容和变化检查。显式指定 `git_diff` 路径时，目标选择不受状态展示条数限制；重命名会检查两端的可见性。

本地集成演示创建文本规范化项目，执行需求答复、精确审批、监督重启、PR 反馈、阶段验收、模型交接、成员恢复、测试许可续期、请求超时恢复，并在包含 5,001 个无关文件的仓库中完成依赖文件集成。它还检查发布、导出、备份、取消、新 Run 与同模型恢复：

```sh
python examples/managed_run_demo.py --output /tmp/harness-ae-demo
```

输出目录需为空。演示使用本地 HTTP 服务和确定性模型响应，写入 `result.json`，不调用收费模型。

## 执行期间提供输入

交互模式在模型工作时持续读取终端输入：

| 输入 | 行为 |
| --- | --- |
| 普通文字或 `/steer TEXT` | 在安全边界补充当前 Lead 的要求 |
| `/queue TEXT` | 当前工作结束后开始下一项请求 |
| `/interrupt TEXT` | 请求取消可取消工作，再应用新要求 |
| `/steer-to NAME TEXT` | 向指定成员发送用户修订 |
| `/inputs` | 查看待处理输入和结果 |
| `/inputs cancel ID` | 取消待处理输入 |
| `/inputs retry ID` | 重试已确认可重试的输入 |
| `/approve ID yes` / `/approve ID no` | 回答指定工具审批 |

原始任务与修订顺序会保存。已完成工具结果继续作为执行事实；新输入到达后，剩余提议重新核验。

宿主 Bash 默认按调用审批。显式 Session 使用精确审批 ID，自动交互还适配唯一已展示的焦点；单次任务的终端提示接受 `y`／`yes`，回车表示拒绝。

## Goal 与测试

`/goal CONDITION` 设置完成目标，`/goal` 查看状态，`/goal resume` 接续，`/goal clear` 清除。

结构化 Goal 支持 `file_exists`、`file_contains`、`file_sha256` 和 `tests_passed`。例如，在工作区外保存：

```json
{
  "condition": "生成报告并通过项目测试",
  "predicates": [
    {"id": "report", "type": "file_exists", "path": "REPORT.md", "min_bytes": 1},
    {"id": "tests", "type": "tests_passed", "recipe": "unit-tests"}
  ],
  "judge": false
}
```

```sh
homebrew-harness session --allow-tests unit-tests \
  --goal-spec ~/.config/homebrew-harness/goal.json
```

同时提供任务描述和 `--goal-spec` 时，描述仍是任务正文，文件提供完成条件；运行时在模型工作前保存两者。Goal 文件使用有界的 UTF-8 JSON 输入。

工作区包含 `tests/` 时，默认登记 `unit-tests`。自定义测试使用 [TestRunner 模板](examples/config/.test-runner.example.json) 和 `--test-config`。`/tests` 查看 recipe，`/tests approve NAME` 或 `--allow-tests NAME` 授权执行。结果关联被测代码快照，供 Goal 检查。

## Workflow 与 MCP

Workflow 使用宿主登记的 Python 异步脚本组织固定步骤、保存结果，并在恢复时复用。内置演示可以离线运行：

```sh
homebrew-harness workflow list
homebrew-harness workflow --cwd /tmp/harness-example demo
```

主会话用 `--demo-workflows` 加载演示，自定义脚本通过 `WorkflowRegistry` 注册。组件组合参见 [设计文档](docs/DESIGN.zh-CN.md)。

MCP 支持真实 stdio 和 Streamable HTTP 传输。本地演示服务：

```sh
python examples/create_mcp_demo_config.py \
  --output ~/.config/homebrew-harness/mcp.json
homebrew-harness session --enable mcp \
  "连接 docs 服务，搜索 Hooks，并查询服务版本"
```

`/mcp` 查看 Session 连接，`/mcp close CONNECTION_ID` 关闭指定实例。配置和脚本位于 [examples](examples/)。

Run 从宿主配置中选择明确的原始工具名，参见 [MCP Run 示例](examples/config/run-mcp.example.json)：

```sh
homebrew-harness run --mcp-config ~/.config/homebrew-harness/mcp.json \
  start --spec examples/config/run-mcp.example.json
```

`mcp` 映射指定服务器别名及工具列表，同时为 Run 开启 MCP。选中的工具与文件、测试工具进入同一个有效工具池。宿主策略决定权限，`confirm` 调用沿用 Run 的持久请求与一次性批准。worker 重建时重新连接，比较保存的声明和工具 schema；业务状态由外部服务自己的存储保存。

`mcp_delegation` 默认是 `lead_only`。设为 `read_only` 后，成员和 Subagent 可在继承的工具范围内使用已选定且宿主策略为 `allow` + `read_only` 的工具。工作区服务按实际任务准备独立输入。自动交互入口使用同一选择契约，也覆盖 detached 执行及已收尾 Session 的交接。

读取响应丢失会保存为失败观察；写入结果不确定时，先检查恢复记录再继续。Run 状态包含宿主与容器 stdio 的资源记录。若 owner 未能提交关闭事实，宿主核实服务已停止后可用 `run mcp-resource-inspect RUN_ID RESOURCE_ID REVISION --token TOKEN --confirmed-stopped` 保存该检查决定。

直接执行模型 Workflow 可用 `--models-config PATH --model ALIAS` 选择已注册配置；也支持单模型环境配置。选中的配置提供协议、选项与请求限制。Session 内的 Workflow 同样使用完整模型绑定；更换绑定后需创建新的 Workflow 运行。

```sh
homebrew-harness workflow --models-config ~/.config/homebrew-harness/models.json \
  --model planner run review-changes --changes-file changes.txt
homebrew-harness admin benchmark --live \
  --models-config ~/.config/homebrew-harness/models.json --model planner
```

benchmark 默认离线，`--live` 显式选择模型执行，验收范围是一组小型文件任务。Workflow 状态区分前台有界返回、记录确认和实际收尾。暂停的运行在收尾及完成记录确认后可恢复；即使存储繁忙，未结束的执行仍继续被运行时持有。

## 可选容器执行

Bash 使用 `--bash-mode container --bash-config PATH` 选择容器后端，TestRunner 和本地 stdio MCP 在宿主配置中选择容器。示例：

- [Bash 宿主模式](examples/config/bash-host.example.json)
- [Bash 容器模式](examples/config/bash-container.example.json)
- [产物收集规则](examples/config/bash-artifacts.example.json)
- [本地 MCP 容器](examples/config/.mcp-container.example.json)

填写 Docker CLI、镜像摘要和镜像内程序路径。容器接收工作区副本。生成文件需按登记规则收集，再用 `publish_artifacts` 发布。

## 状态与维护

会话状态保存在工作目录的 `.harness/`，Task、Memory 和 worktree 使用各自的本地目录。`/recovery` 查看需要检查的操作，核实后使用提示中的恢复命令。

```sh
homebrew-harness admin storage-status --cwd /tmp/harness-example
homebrew-harness admin checkpoint-list --cwd /tmp/harness-example
homebrew-harness admin gc-preview --cwd /tmp/harness-example
```

`homebrew-harness admin` 管理备份、历史回收和 checkpoint 退休。默认保留最近 10 个会话，较旧会话只有在关闭并完成全部收尾超过 15 天后才进入退休候选。参数参见 `homebrew-harness admin --help`。

## Python 接口

持久 Service 提供工作队列、固定周期调度、原生审批，以及通过认证 HTTP 控制 API 连接的 Text UI。独立程序使用 `RemoteService`，远程操作者可通过外部 `--connection` 文件连接。设置与生命周期操作见 [Services](docs/SERVICES.zh-CN.md)。可选的 [仓库交付](docs/DELIVERY.zh-CN.md) 提供固定候选、质量检查、CI 接续和 PR 交付。默认由维护者合并，可选服务模式在明确批准后执行原生平台合并，并核验实际结果。manual profile 将有界工作交给已有 Run；repository_maintenance 增加平台收件（当前为 GitHub）、范围化工作决定、持久对话与报告。

```python
from pathlib import Path
from homebrew_harness import Harness, HostConfig

with Harness(workdir=Path.cwd(), config=HostConfig.from_files()) as harness:
    session = harness.sessions.open()
    turn = session.submit("解释项目的主要模块", request_id="overview-1")
    report = turn.wait(120)
    if report.snapshot.output_available:
        print(turn.result().text)
```

`Harness` 提供 Session、托管 Run、Workflow、自动入口、审批和维护服务；`AsyncHarness` 提供可等待视图。配置、steering、后台执行、恢复和资源归属用法见 [Python API](docs/PYTHON_API.zh-CN.md)。

[应用示例](examples/python_api.py) 支持离线演示：

```sh
python examples/python_api.py --demo --workspace /tmp/harness-sdk-example
```

`HarnessSession`、`Capabilities`、`ModelRegistry`、`LoopComponents` 和 `agent_loop` 继续用于组件组合。模块职责与执行流程参见 [docs/DESIGN.zh-CN.md](docs/DESIGN.zh-CN.md)。

## 开发与许可

```sh
python -m unittest discover -s tests -t .
```

测试覆盖本地协议服务、文件和数据库、恢复流程与容器集成；Linux Docker 用例按环境启用。

项目采用 [MIT](LICENSE)。部分思路和代码参考 learn-claude-code 教程，上游许可保存在 [licenses](licenses/learn-claude-code-LICENSE)。运行依赖保留各自许可证。

代码平台接入使用明确适配器，当前实现 GitHub；通用执行和维护状态可供后续平台复用。参见 [代码管理平台](docs/PLATFORMS.zh-CN.md)。

[Service 运维](docs/SERVICE_OPERATIONS.zh-CN.md) 提供健康、写入者停靠、工作证据保留、备份验证/恢复、周期仓库审查及显式兼容版本升级。离线 P1～P4 场景可使用 `python examples/service_operations_demo.py --output /tmp/harness-service-operations-demo`。

社区多轮沟通和外部 PR 审查是 Service 的可选能力。配置与控制方法见[社区讨论](docs/COMMUNICATION.zh-CN.md)。

日常贡献使用 PR 流程，见[贡献指南](CONTRIBUTING.zh-CN.md)。
