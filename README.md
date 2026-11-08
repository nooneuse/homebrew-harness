# homebrew-harness

一个可组合的 Python Agent 运行时。它将模型调用、工具执行、上下文管理和持久状态连接起来，支持交互开发、局部委派和可恢复的任务执行。

模型提出下一步动作，运行时调用对应工具，将结果放回上下文，直到产生最终回答。Task、Goal、Workflow 和 Agent Team 在这一循环上提供不同的工作组织方式。

## 功能

| 范围 | 能力 |
| --- | --- |
| 模型接入 | Chat Completions、Responses、DashScope Generation、Anthropic Messages；按角色选择模型 |
| 基础工具 | 文件读取/写入/编辑、文件匹配、Bash、计划更新、技能加载 |
| 上下文 | 按需技能目录、历史压缩、完整结果归档、可选跨会话记忆 |
| 编排 | Subagent、持久任务依赖图、Agent Teams、后台命令、Cron、Workflow |
| 完成检查 | Goal 与文件/测试硬条件 |
| 交互 | 工作期间追加 steering、排队请求、interrupt 和独立工具审批 |
| 执行环境 | 宿主 Bash、可选 Bash/TestRunner/本地 MCP 容器后端、容器产物收集与发布 |
| 状态管理 | SQLite checkpoint、持久 inbox、恢复查询、备份与历史维护 |

## 安装

需要 Python 3.10 或以上。核心运行不依赖 Docker；容器后端需要可用的 Docker 环境和配置好的镜像。

在本项目目录执行：

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

安装后提供三个入口：

```sh
homebrew-harness --help
harness-workflow --help
harness-admin --help
```

也可以通过 `python -m homebrew_harness` 启动主程序。

## 配置模型

配置放在工作区外。创建默认配置文件：

```sh
mkdir -p ~/.config/homebrew-harness
cp examples/config/.env.example ~/.config/homebrew-harness/.env
chmod 600 ~/.config/homebrew-harness/.env
```

在该文件填写 `OPENAI_API_KEY`、`OPENAI_BASE_URL` 和 `OPENAI_MODEL`。单模型入口使用 Chat Completions，已导出的环境变量优先于文件内容。

多模型和其他协议使用 [模型目录模板](examples/config/.models.example.json)。`connections` 声明协议与环境变量名，`profiles` 声明模型和用途，`defaults` 指定 Lead 与成员角色的默认配置。

```sh
cp examples/config/.models.example.json ~/.config/homebrew-harness/models.json
homebrew-harness --models-config ~/.config/homebrew-harness/models.json
```

填写模板引用的环境变量，并按实际模型配置工具、结构化输出和思考能力。Lead 可用 `--lead-model` 选择配置别名；成员和 Subagent 从宿主登记的配置中选择。模板中的连接值保持为空或通过环境变量引用，仓库不保存实际凭据和模型接入地址。

## 开始使用

```sh
# 交互会话
homebrew-harness

# 单次任务
homebrew-harness "阅读项目文件，说明核心模块之间的关系"

# 指定工作目录
mkdir -p /tmp/harness-example
homebrew-harness --cwd /tmp/harness-example \
  "创建 hello.py，打印 Hello, world!，然后读回确认"

# 恢复最近保存的会话
homebrew-harness --resume-session
```

默认 `core` 配置包含基础工具。按需启用额外模块：

```sh
homebrew-harness --enable subagents --enable tasks
homebrew-harness --enable teams --enable worktrees
homebrew-harness --enable background --enable cron
homebrew-harness --memory-mode recall
```

`--profile full` 启用全部可选模块，记忆默认为 recall。`--memory-mode auto` 允许正常用户回合结束后的记忆维护；`off` 关闭记忆调用。

## 受管理的单 Agent 运行

`harness-run` 将一个任务保存为 Run，由独立执行进程按 Episode 推进。正常段落结束后接续同一会话，进程恢复时核对 checkpoint 与实际执行状态。Run 的总期限包含等待和重启时间。

```sh
cp examples/config/run.example.json ~/.config/homebrew-harness/run.json
harness-run --cwd /path/to/project start \
  --spec ~/.config/homebrew-harness/run.json
```

启动后输出 `run_id`。可以从另一个终端管理：

```sh
harness-run --cwd /path/to/project status RUN_ID
harness-run --cwd /path/to/project steer RUN_ID "补充当前任务要求"
harness-run --cwd /path/to/project pause RUN_ID
harness-run --cwd /path/to/project resume RUN_ID
harness-run --cwd /path/to/project cancel RUN_ID
```

`supervise RUN_ID` 在管理进程重启后接管可恢复任务。`steer --mode queued` 将补充信息留到下一 Episode，`--mode interrupt` 请求取消当前可取消步骤；独立新任务使用新的 Run。

默认提供文件、上下文与测试工具。`allow_tests` 指定预授权的固定 recipe；测试定义、有效期和次数在恢复后继续核对。`enabled` 可开启 `subagents`、`background` 或固定 `workflow`，`tools` 选择其实际工具。模型、测试和 Bash 配置继续使用工作区外文件，可通过 `--env-file`、`--models-config`、`--test-config` 和 `--bash-config` 指定。

状态包含运行结果、停止原因、心跳和收尾状态。权限不足或需要检查的效果会使 Run 阻塞；`recovery RUN_ID` 查看记录，手工确认后使用 `inspect RUN_ID OPERATION_ID REVISION`，再显式恢复。恢复需等待原执行完成收尾。该入口用于固定授权的单 Agent 运行。

需要动态工具许可时，Run 保存精确请求并进入 `waiting`。需求问题使用独立的 Attention 请求；可以查询后按当前 revision 答复：

```sh
harness-run --cwd /path/to/project requests RUN_ID
harness-run --cwd /path/to/project approve RUN_ID REQUEST_ID REVISION yes
harness-run --cwd /path/to/project answer RUN_ID QUESTION_ID REVISION "需求决定"
```

批准后模型通过 `execute_approved` 提交原调用，授权在接受操作时消费一次。答复作为用户修订进入原任务。`write_paths` 指定 Run 的文件可写前缀；固定测试和 Bash 后端仍由宿主登记。反复失败、无效写入和文件状态循环会触发局部重规划，持续无效时阻塞；`progress RUN_ID` 查询，`progress-reset RUN_ID REVISION --reason TEXT` 在执行结束后开启新的重规划周期。

可选 [Run control 配置](examples/config/run-control.example.json) 订阅已有 GitHub PR，并配置最小通知。将配置放在工作区外，填写仓库、PR、允许的 actor/app ID 和环境变量；删除不使用的 `github` 或 `notification` 项。

```sh
harness-run --cwd /path/to/project \
  --control-config /path/to/private/run-control.json start --spec /path/to/private/run.json
```

来源将当前 head 的 CI/review 与允许主体的评论交给模型作为参考，完整事件可回读；源内容不形成工具许可。`events RUN_ID`、`effects RUN_ID` 查询记录。通知默认不开启，启用后发送决定请求、阻塞和完成状态；不明确的发送结果保留为 `unknown`，用 `effects-resolve RUN_ID EFFECT_ID REVISION delivered --reason TEXT` 在核实后结清。接续同一 Run 时沿用原 control 配置。

RunSpec 可配置 `stages`，每个阶段有 `key`、`subject`、`depends_on` 和 `acceptance`。阶段验收采用 `judge: false` 的结构化硬条件；最终交付由 `goal` 描述。参见 [分阶段运行示例](examples/config/run-stages.example.json)。

代码依赖使用 `inputs` 指定已验收前驱的文件，例如 `"inputs": [{"stage": "api", "paths": ["api.py"]}]`。宿主在消费者认领前交付固定版本；输入只读，其版本与阶段证明关联。`depends_on` 表达先后和验收关系，输入及产物的文件范围分别声明。

阶段自动启用 Task 工具。模型认领任务后，`complete_task` 提交当前产物；宿主验收通过才完成 Task、保存交接并解锁依赖。相关文件或需求变化会使旧验收失效。`stages RUN_ID` 查看任务状态，`handoffs RUN_ID` 查看原始要求、验收事实和模型总结。模型使用 `get_handoff` 回读交接。测试凭证保存在 SQLite，跨进程核对当前代码与 recipe 后继续使用。

`members` 声明固定成员的名称、角色、提示、模型别名与恢复次数。对应阶段通过 `executor` 指定成员，`isolated: true` 使用独立 worktree，`write_paths` 表达文件归属。成员恢复保留原身份、任务和上下文；未知效果需要宿主检查。输入修订未应用时，成员在原任务中等待，应用后继续；工作段结束而任务未完成时，运行时将具体待办交给 Lead。`status` 中的 `member_outcomes` 可查看原因和结果。

集成阶段使用 `integrates` 引用前驱，`integrate_stage` 在生产者停止后将已验收产物应用到集成 worktree。验收通过且声明 `publish: true` 时，`publish_stage` 将清单中的产物发布到 base。`transfers` 查看结果；中断后用 `transfers-resolve` 核对实际文件，再处理关联操作。

`fallback_models` 声明备用模型别名，`fallback_errors` 指定自动切换的故障类别。宿主也可使用 `handoff RUN_ID MODEL_PROFILE REVISION --reason TEXT`。切换使用新的 Lead 会话，承接原始要求、Task 和交接；成员模型保持原绑定，Run 总期限不变。同模型的 `resume` 在检查通过后确认已处理的连接故障，保存恢复确认与 checkpoint，继续原用量和期限。

取消或最终失败后的未完成 Task/Goal 会在实际执行与未知效果结清后成为 `abandoned`。已验收结果继续保留。`finalized_at` 表示归属结清完成，此后可在同一工作区启动新的 Run。

成员的固定测试许可到期或耗尽后，会登记 `test_grant` 请求并等待宿主决定。`harness-run requests RUN_ID` 查看请求，`harness-run approve RUN_ID REQUEST_ID REVISION yes --runs 1 --seconds 600` 为原任务续期；次数和期限受登记的 recipe 与 Run 上限约束。成员收到决定后重新核验许可并继续。

模型请求超时使用有限恢复策略；Episode 到期保存后接续，Run 或 Session 总期限到期停止。迟到响应被丢弃，尚未结束的执行保留收尾状态。

工作树阶段按产物、前驱输入、硬条件和测试输入范围保存内容快照。可用 `source_paths` 补充仓库内源码依赖，它是相对路径前缀，不扩大写权限。`snapshot_limits` 可配置 `max_files`、`max_bytes` 和 `max_paths`；Run 中控制阶段快照，测试 recipe 中控制其输入快照。范围外仍保留工作区与 Git 变更检查；工作树从完整的干净 HEAD 创建。


`export RUN_ID --output FILE` 导出运行要求与状态，`backup RUN_ID --output DIR` 在停止监督和完成收尾后备份数据库与附件。`verify-backup DIR` 校验清单；`restore-backup DIR --output NEW_DIR` 恢复到新的检查目录。工作区/Git 身份由宿主核对后再安排执行。`min_free_bytes` 指定运行时磁盘余量，默认 128 MiB。

文件工具还提供 `search_text`、`git_status` 和 `git_diff`，用于有限范围的内容与代码变化检查。

本地 A～E 场景会创建一个文本规范化项目，运行需求答复、精确测试授权、两次监督进程重启、PR 反馈、阶段验收、模型切换、双成员恢复与集成，并核对导出和备份，以及取消后新任务和同模型恢复：

```sh
python examples/managed_run_demo.py --output /tmp/harness-ae-demo
```

输出目录需为空。场景使用本地 HTTP 服务和确定性模型响应，结果写入 `result.json`，不调用收费模型。

## 在执行中提供新输入

交互模式会持续读取终端输入：

| 输入 | 行为 |
| --- | --- |
| 普通文字或 `/steer TEXT` | 在安全边界补充当前 Lead 的要求 |
| `/queue TEXT` | 当前工作结束后开始下一项请求 |
| `/interrupt TEXT` | 请求取消当前可取消工作，再应用新要求 |
| `/steer-to NAME TEXT` | 向指定队友发送用户修订 |
| `/inputs` | 查看待处理输入和处理结果 |
| `/inputs cancel ID` | 取消待处理输入 |
| `/inputs retry ID` | 重试已确认可重试的失败输入 |
| `/approve ID yes` / `/approve ID no` | 回答指定工具审批 |

原始任务与修订顺序会保存。已完成的工具结果继续作为执行事实，剩余旧提议在新修订到达后重新判断。

宿主 Bash 默认按调用审批。交互模式使用精确审批 ID；单次任务的终端提示接受 `y`/`yes`，回车表示拒绝。

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
homebrew-harness --allow-tests unit-tests \
  --goal-spec ~/.config/homebrew-harness/goal.json
```

工作目录包含 `tests/` 时，默认登记 `unit-tests`。自定义测试使用 [TestRunner 模板](examples/config/.test-runner.example.json) 和 `--test-config`。`/tests` 查看 recipe，`/tests approve NAME` 或 `--allow-tests NAME` 授权执行。测试结果关联对应代码快照，供 Goal 检查。

## Workflow 与 MCP

Workflow 使用宿主登记的 Python 异步脚本组织固定步骤，保存每步结果并在恢复时复用。内置示例可离线运行：

```sh
harness-workflow list
harness-workflow --cwd /tmp/harness-example demo
```

主会话可用 `--demo-workflows` 加载示例。自定义脚本通过 `WorkflowRegistry` 注册，具体组合方式见 [设计文档](docs/DESIGN.md)。

MCP 支持真实 stdio 和 Streamable HTTP 传输。本地演示服务：

```sh
python examples/create_mcp_demo_config.py \
  --output ~/.config/homebrew-harness/mcp.json
homebrew-harness --enable mcp \
  "连接 docs 服务，搜索 Hooks，并查询服务版本"
```

`/mcp` 查看当前连接，`/mcp close CONNECTION_ID` 关闭指定实例。配置和演示脚本位于 [examples](examples/)。

## 可选容器执行

Bash 使用 `--bash-mode container --bash-config PATH` 选择容器后端。TestRunner 和本地 stdio MCP 在各自宿主配置中选择容器。配置示例：

- [Bash 宿主模式](examples/config/bash-host.example.json)
- [Bash 容器模式](examples/config/bash-container.example.json)
- [容器产物规则](examples/config/bash-artifacts.example.json)
- [本地 MCP 容器](examples/config/.mcp-container.example.json)

填写 Docker CLI、镜像摘要和镜像内程序位置。容器接收工作区副本；需要保存生成文件时，先按登记规则收集产物，再通过 `publish_artifacts` 发布。

## 状态与维护

会话数据保存在工作目录的 `.harness/`，任务、记忆与 worktree 使用各自的本地目录。`/recovery` 查看需要检查的操作；检查完成后使用提示中的恢复命令接续。

```sh
harness-admin storage-status --cwd /tmp/harness-example
harness-admin checkpoint-list --cwd /tmp/harness-example
harness-admin gc-preview --cwd /tmp/harness-example
```

备份、历史回收和 checkpoint 退休通过 `harness-admin` 管理。默认退休策略保留最近 10 个会话，只选择关闭并完成收尾超过 15 天的较旧会话。具体参数见 `harness-admin --help`。

## Python 接口

```python
import os
from pathlib import Path
from openai import OpenAI
from homebrew_harness import HarnessSession

client = OpenAI(
    api_key=os.environ["OPENAI_API_KEY"],
    base_url=os.environ["OPENAI_BASE_URL"],
    max_retries=0,
)

with client:
    with HarnessSession(client, os.environ["OPENAI_MODEL"], Path.cwd()) as session:
        answer = session.run_user("阅读项目并解释主要模块")
        print(answer)
```

`HarnessSession` 是常用入口；`Capabilities`、`ModelRegistry`、`LoopComponents` 和 `agent_loop` 用于宿主组合。模块职责与运行流程见 [docs/DESIGN.md](docs/DESIGN.md)。

## 开发与许可

```sh
python -m unittest discover -s tests -t .
```

测试包含本地协议服务、文件与数据库、恢复流程和容器集成；Linux Docker 用例按环境启用。

项目采用 [MIT](LICENSE)。部分思路和代码参考 learn-claude-code 教程，其上游许可保存在 [licenses](licenses/learn-claude-code-LICENSE)。运行依赖保留各自许可证。
