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

安装后提供四个命令：

```sh
homebrew-harness --help
harness-run --help
harness-workflow --help
harness-admin --help
```

也可以通过 `python -m homebrew_harness` 启动主程序。

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
homebrew-harness --models-config ~/.config/homebrew-harness/models.json
```

填写模板引用的环境变量，并按实际模型配置工具、结构化输出和思考能力。Lead 可用 `--lead-model` 选择别名；成员和 Subagent 从宿主登记的配置中选择。凭据与模型接入地址由宿主环境提供。

## 快速开始

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

工作目录默认为启动命令时所在的目录。`--cwd` 选择其他工作区，会话状态保存在该工作区中。

默认 `core` 配置包含基础工具。按需启用可选模块：

```sh
homebrew-harness --enable subagents --enable tasks
homebrew-harness --enable teams --enable worktrees
homebrew-harness --enable background --enable cron
homebrew-harness --memory-mode recall
```

`--profile full` 启用全部可选模块，记忆默认为 `recall`。`--memory-mode auto` 允许正常用户回合结束后的记忆维护；`off` 关闭记忆调用。

项目附带 [Python 代码审查](skills/code-review/SKILL.zh-CN.md) 和 [文本摘要](skills/text-summary/SKILL.zh-CN.md) 两个技能示例。

## 受管理运行（无人值守）

`harness-run` 将一个任务保存为 Run，由独立执行进程按有界 Episode 推进。段落之间接续同一会话；进程恢复时核对 checkpoint 与实际执行状态。Run 的总期限包含等待和重启时间。Run 可以使用声明的阶段和固定成员团队。

```sh
cp examples/config/run.example.json ~/.config/homebrew-harness/run.json
harness-run --cwd /path/to/project start \
  --spec ~/.config/homebrew-harness/run.json
```

### 控制与恢复

启动后输出 `run_id`。可以从另一个终端管理：

```sh
harness-run --cwd /path/to/project status RUN_ID
harness-run --cwd /path/to/project steer RUN_ID "补充当前任务要求"
harness-run --cwd /path/to/project pause RUN_ID
harness-run --cwd /path/to/project resume RUN_ID
harness-run --cwd /path/to/project cancel RUN_ID
```

`supervise RUN_ID` 在监督进程重启后接管可恢复任务。`steer --mode queued` 将输入留到下一 Episode；`--mode interrupt` 请求取消当前可取消步骤。独立任务使用新的 Run。

默认工具覆盖文件、上下文和测试。`allow_tests` 预授权固定测试 recipe。`enabled` 可以启用 Subagent、后台执行或登记的 Workflow，`tools` 选择其工具。通过 `--env-file`、`--models-config`、`--test-config` 和 `--bash-config` 指定工作区外的宿主配置。

状态包含运行结果、停止原因、心跳、成员结果和收尾状态。`recovery RUN_ID` 查看需要检查的操作；核对效果后，使用 `inspect RUN_ID OPERATION_ID REVISION` 并显式恢复。恢复需等待原执行完成收尾。

模型请求超时使用有限恢复策略。Episode 到期保存后接续；Run 或 Session 期限到期停止。迟到响应会被丢弃，尚未结束的执行继续登记并等待收尾。

取消或最终失败后，未完成 Task／Goal 会在执行与未知效果结清后成为 `abandoned`。已验收结果继续保留。`finalized_at` 表示归属结清完成，此后可在同一工作区启动新的 Run。

### 审批与测试许可

工具需要许可时，Run 保存精确请求并进入 `waiting`。需求问题使用独立的 Attention 请求。查询后按当前 revision 答复：

```sh
harness-run --cwd /path/to/project requests RUN_ID
harness-run --cwd /path/to/project approve RUN_ID REQUEST_ID REVISION yes
harness-run --cwd /path/to/project answer RUN_ID QUESTION_ID REVISION "需求决定"
```

批准后模型通过 `execute_approved` 提交原调用，许可在接受操作时消费一次。Attention 答复作为用户修订进入原任务。

成员的固定测试许可到期或耗尽后，会登记 `test_grant` 请求。为原任务续期时，指定有限次数和期限：

```sh
harness-run --cwd /path/to/project approve RUN_ID REQUEST_ID REVISION yes \
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
harness-run --cwd /path/to/project \
  --control-config /path/to/private/run-control.json start --spec /path/to/private/run.json
```

来源将当前 head 的 CI／review 与允许主体的评论作为参考输入，完整事件可回读。来源内容不授予工具许可。`events RUN_ID` 和 `effects RUN_ID` 查看记录。

通知默认关闭。开启后报告决定请求、阻塞和完成状态。不明确的发送结果保留为 `unknown`；核实后使用 `effects-resolve RUN_ID EFFECT_ID REVISION delivered --reason TEXT`。接续 Run 时使用原 control 配置。

### 导出、备份与演示

`export RUN_ID --output FILE` 导出要求与状态。`backup RUN_ID --output DIR` 在监督停止且完成收尾后备份数据库和附件。`verify-backup DIR` 检查清单，`restore-backup DIR --output NEW_DIR` 恢复到新的检查目录。执行前由宿主核对工作区和 Git 身份。`min_free_bytes` 指定运行时磁盘余量，默认 128 MiB。

`search_text`、`git_status` 和 `git_diff` 提供有界内容和变化检查。

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

宿主 Bash 默认按调用审批。交互模式使用精确审批 ID；单次任务的终端提示接受 `y`／`yes`，回车表示拒绝。

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

工作区包含 `tests/` 时，默认登记 `unit-tests`。自定义测试使用 [TestRunner 模板](examples/config/.test-runner.example.json) 和 `--test-config`。`/tests` 查看 recipe，`/tests approve NAME` 或 `--allow-tests NAME` 授权执行。结果关联被测代码快照，供 Goal 检查。

## Workflow 与 MCP

Workflow 使用宿主登记的 Python 异步脚本组织固定步骤、保存结果，并在恢复时复用。内置演示可以离线运行：

```sh
harness-workflow list
harness-workflow --cwd /tmp/harness-example demo
```

主会话用 `--demo-workflows` 加载演示，自定义脚本通过 `WorkflowRegistry` 注册。组件组合参见 [设计文档](docs/DESIGN.zh-CN.md)。

MCP 支持真实 stdio 和 Streamable HTTP 传输。本地演示服务：

```sh
python examples/create_mcp_demo_config.py \
  --output ~/.config/homebrew-harness/mcp.json
homebrew-harness --enable mcp \
  "连接 docs 服务，搜索 Hooks，并查询服务版本"
```

`/mcp` 查看连接，`/mcp close CONNECTION_ID` 关闭指定实例。配置和脚本位于 [examples](examples/)。

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
harness-admin storage-status --cwd /tmp/harness-example
harness-admin checkpoint-list --cwd /tmp/harness-example
harness-admin gc-preview --cwd /tmp/harness-example
```

`harness-admin` 管理备份、历史回收和 checkpoint 退休。默认保留最近 10 个会话，较旧会话只有在关闭并完成全部收尾超过 15 天后才进入退休候选。参数参见 `harness-admin --help`。

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

`HarnessSession` 是常用入口，`Capabilities`、`ModelRegistry`、`LoopComponents` 和 `agent_loop` 用于宿主组合。模块职责与执行流程参见 [docs/DESIGN.zh-CN.md](docs/DESIGN.zh-CN.md)。

## 开发与许可

```sh
python -m unittest discover -s tests -t .
```

测试覆盖本地协议服务、文件和数据库、恢复流程与容器集成；Linux Docker 用例按环境启用。

项目采用 [MIT](LICENSE)。部分思路和代码参考 learn-claude-code 教程，上游许可保存在 [licenses](licenses/learn-claude-code-LICENSE)。运行依赖保留各自许可证。
