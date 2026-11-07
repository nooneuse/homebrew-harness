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
