# Python API

[English](PYTHON_API.md)

应用程序通过 `Harness` 创建执行、查询状态和处理审批。它复用 CLI 使用的配置装配与控制器；`AsyncHarness` 提供对应的异步视图。

## 创建 Session

```python
from pathlib import Path
from homebrew_harness import Harness, HostConfig, SessionOptions

config = HostConfig.from_files()
with Harness(workdir=Path("project"), config=config) as harness:
    session = harness.sessions.open(SessionOptions(enabled=("subagents",)))
    turn = session.submit("解释项目的公开 API", request_id="explain-api-1")
    report = turn.wait(120)
    if report.snapshot.output_available:
        print(turn.result().text)
    else:
        print(report.data())
```

`workdir` 是执行工作目录，状态保存在其中的 `.harness/`。默认模型环境和工具配置与 CLI 使用相同的宿主文件。通过 `env_file`、`models_config`、`mcp_config`、`test_config`、`bash_config` 指定工作区之外的其他配置文件；连接信息放在这些宿主文件中。

`SessionOptions` 选择能力、`model_profile`、MCP 工具、记忆行为和单轮限制。宿主配置定义可用目录，能力启用后仍按原有权限策略执行。

应用也可以传入已有客户端或模型目录：

```python
config = HostConfig.from_components(client=client, model=model_name)
# 也可以使用 HostConfig.from_components(model_registry=registry)
```

借入的客户端和注册表在 Harness 关闭后保持可用；`own_client=True` 将关闭责任交给 Harness。`workflow_registry`、`hooks`、`environment` 映射也是可选宿主组件。传入环境映射不会修改应用的进程环境。

## 服务与句柄

`harness.services` 创建持久服务与串行工作队列。独立程序通过 `RemoteService` 连接认证 HTTP API，无需本地执行工作区或模型配置：

```python
from homebrew_harness import RemoteService

with RemoteService.from_file(connection_file, control=True) as service:
    work = service.work.submit("实现所需修改", request_id="feature-1")
    print(work.wait(120).data())
```

`RemoteService` 退出上下文时关闭自己拥有的连接，Service 继续运行。传入的 `ControlClient` 默认借用，`own_client=True` 将关闭责任交给视图。`ControlConnection` 接收显式凭据，`from_file` 读取外部连接文件。客户端导入不加载 POSIX 执行后端；原生 Windows Service 执行另行适配。

ServiceSpec、调度、detached、连接配置与 Text UI 用法见 [Services](SERVICES.zh-CN.md)，AsyncHarness 提供相同的宿主嵌套 work API。

| 服务 | 主要操作 | 执行单位 |
| --- | --- | --- |
| `sessions` | `open`、`get`、`resume`、`turn`、`list` | 持续 Session 和单次请求 Turn |
| `services` | `start`、`get`、`recover_launch` | 持久服务与串行工作项 |
| `runs` | `start`、`get`、`list` | 跨 Episode 和阶段的托管 Run |
| `workflows` | `start`、`get`、`resume`、`list` | 已注册的可恢复脚本 |
| `interactions` | `open`、`get` | 自动需求路由入口及其任务 |
| `requests` | `list`、`approve`、`deny`、`answer`、`renew_tests` | 针对具体请求的宿主决定 |
| `admin` | `inspect`、`export`、`backup` | 显式恢复检查和 Run 管理 |

Turn、Run、Workflow、自动路由任务均提供以下观察方法：

| 方法 | 含义 |
| --- | --- |
| `snapshot()` | 业务状态、输入版本、待处理请求、持久化和收尾状态 |
| `wait(timeout, until="actionable")` | 等待完成、宿主决定或其他可处理状态 |
| `wait(timeout, until="settled")` | 等待结束或暂停的执行确认持久化并完成收尾 |
| `result()` | 读取本次执行的已确认文本或 JSON 结果 |
| `requests()` | 查询当前宿主请求 |
| `watch(interval=0.1)` | 产生变化后的状态快照，首条事件包含当前状态 |

`ExecutionRef` 包含 `kind`、`id` 和可选 `owner_id`。状态、结果、等待报告、关闭报告均提供 `data()`，便于构造应用响应。每个 Turn 的结果属于原始请求，不会被下一次 Session 请求覆盖。

等待超时只停止观察，执行继续遵循自己的 deadline 或显式控制。`output_available`、`persistence_confirmed`、`execution_pending` 分别表示结果可读、持久化确认和实际执行是否仍在继续。Turn 可以结束而 Session 保留连接；Workflow 显示暂停时仍可能在收尾，恢复前应检查 `execution_pending`。

`watch` 投影已有状态和请求，允许进度合并或重复；持久化记录仍是权威来源。关闭观察者不取消执行。SDK 默认不向终端打印运行时消息。

## 提交、steering 和取消

```python
turn = session.submit("保持现有公开 API", request_id="change-17")
print(turn.submission().data())
turn.steer("同时覆盖失败处理", request_id="change-17-amendment")
# turn.interrupt("使用更小的实现", request_id="change-17-interrupt")
# turn.cancel()
```

`submit` 持久化受理输入后返回，不等待模型结果。同一请求 ID 和相同内容返回同一引用；同一 ID 配不同内容会被拒绝。可能丢失返回时，应保留调用方指定的 ID。Run、Workflow 也提供 `submission()`，区分受理和启动确认；`startup_pending` 表示需要检查已受理执行。

Steering 绑定原始请求，包括仍在队列中的请求。旧模型轮次的提议会在工具执行前被隔离。取消 Turn 后仍可继续使用 Session。SDK 文本始终是任务材料，`/approve`、`/goal` 或 `yes` 不会触发宿主命令。

`session.execute(text, timeout=...)` 将提交和等待合并，返回 `WaitReport`。应用需要立即得到稳定句柄时使用 `submit`。

## 宿主决定与结构化 Goal

```python
pending = turn.requests()
# 应用将请求展示给有权限的用户。
# 用户批准这一具体请求后：
harness.requests.approve(pending[0].ref, decision_id="decision-17")
```

请求引用包含执行目标和版本，决定只应用于这一具体调用。Steering 或过期引用错误后应刷新请求。`deny` 拒绝工具调用；`answer(ref, text=...)` 回答 Run 的 attention 问题；`renew_tests(ref, runs=..., seconds=...)` 续期固定测试授权。它们复用原生控制器的校验与过期规则。

Session 即时审批随活动轮次过期，其返回包含 `delivery`。已受理但交付未知的决定不能证明工具已经执行。Run 的持久请求跨 worker 重启保存。应用通过网络开放这些方法时，由应用提供身份认证和授权用户界面。

通过 `session.submit(..., goal=goal_spec)` 或 `RunSpec(goal=goal_spec)` 传入结构化 Goal，继续使用现有硬条件和验证器。Goal 组合见[设计文档](DESIGN.zh-CN.md)。`session.goal_status()` 查询状态；`session.approve_test(name)`、`session.revoke_test(name)` 显式管理已注册 Session 测试配方。

## 托管 Run 和后台执行

```python
from homebrew_harness.runs import RunSpec

spec = RunSpec("实现需求并验证", episode_model_calls=12, total_seconds=1800)
job = harness.runs.start(spec, request_id="feature-17")
print(job.wait(120).data())
# job.steer("保持当前行为", request_id="feature-17-amendment")
# job.pause()
# job.resume()
# job.cancel()
```

RunSpec 使用现有阶段、成员、Goal、模型别名、fallback、deadline、测试和 MCP 契约。Run 保留一个工作区只能有一个活动 Run 的限制及真实执行租约。费用由模型提供商管理。

`runs.get(id)` 观察已有 Run；`runs.get(id, control=True)` 显式取得发送控制的句柄。`runs.start(..., lifetime="detached")` 使用现有独立 supervisor 启动；`job.detach()` 在原执行收尾后移交附着 Run。关闭观察宿主不会停止 detached Run。

Worker 需要通过宿主配置文件重建执行。进程内客户端、环境映射、Hook 和 Python Workflow callable 用于进程内 Session 或直接 Workflow，不序列化给 worker。

## 已注册 Workflow 与自动路由

```python
from homebrew_harness.orchestration.workflow_runtime import WorkflowRegistry, WorkflowSpec

async def summarize(ctx, args):
    return await ctx.agent(args["text"], label="summary")

schema = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}
registry = WorkflowRegistry([WorkflowSpec("summarize", "摘要", summarize, schema)])
config = HostConfig.from_components(workflow_registry=registry)
with Harness(workdir=Path("project"), config=config) as harness:
    job = harness.workflows.start("summarize", {"text": "需要总结的材料"}, request_id="summary-17")
    print(job.wait(120).data())
```

Workflow 执行已注册脚本，复用原生步骤 journal、模型绑定、锁和收尾。`job.pause()` 请求暂停；`job.resume()` 或 `workflows.resume(id)` 在暂停持久化和收尾确认后恢复，保留原模型别名。参数在受理前验证。`workflows.get(id)` 读取保存结果时不分配模型客户端。

自动需求路由使用：

```python
entrance = harness.interactions.open()
task = entrance.submit("检查项目并实现一个小改进", request_id="auto-17")
print(task.wait(120).data())
```

当路由或交接将原生执行切换为 Run 时，自动入口任务保持自己的引用；快照包含当前路由与目标。它提供 `steer`、`interrupt`、`pause`、`resume`、`cancel`、`upgrade`、`detach`，具体操作由原生执行状态决定。

MCP 使用 `SessionOptions(mcp={"docs": ["search"]})` 或 `RunSpec(mcp={"docs": ["search"]})` 选择工具，宿主文件配置 transport 与权限。Team、Subagent 保留模型别名和可选只读委派契约。Cron 仍是 Session 能力。

## 重新打开与资源收尾

```python
saved = harness.sessions.get(session_id)       # 只观察
old_result = harness.sessions.turn(turn_id)   # 原始请求引用
session = harness.sessions.resume(session_id) # 显式取得执行归属
```

恢复会校验模型、能力、执行策略与租约，不重复已完成请求。中断请求应结合 `session.recovery_status()` 和 Turn 快照检查。`admin.inspect(session, operation_id, revision)` 记录对未知操作的显式检查；`session.continue_recovery()` 在原生条件满足后解除恢复阻挡。中断推理没有确认最终结果时，通过新的显式输入继续。已受理异步后续工作继续关联原始请求 checkpoint。

Session 结果和即时决定随 checkpoint 退休及撤销退休一起归档和恢复。已退休 Session 需先通过现有宿主 checkpoint 管理恢复。

`harness.close(timeout=...)` 请求关闭并返回 `CloseReport`：关闭所属 Session/Workflow，暂停附着 Run。`wait_closed(timeout)` 观察后续收尾。待收尾报告保留执行归属和租约，直到真实工作结束。上下文退出时，若超过窗口则抛出 `ClosePending`，已有应用异常仍保留。借入客户端和外部 detached 执行保持原归属。

`SDKError` 包含稳定的 `code` 和可选执行 `ref`；子类包括 `InvalidInput`、`StaleReference`、`UnsupportedOperation`、`ResultNotReady`、`ClosePending`。普通执行失败和宿主等待状态通过快照与结果表达。

## 异步应用

```python
from homebrew_harness import AsyncHarness

async def execute(config):
    async with AsyncHarness(workdir=Path("project"), config=config) as harness:
        session = await harness.sessions.open()
        turn = await session.submit("解释项目模块", request_id="async-17")
        async for event in turn.watch():
            print(event.data())
        return await turn.result()
```

异步视图共享同一执行状态。服务和控制方法可等待，`id`、`ref` 是直接可读元数据。取消 `wait` 或 `watch` 只停止观察；取消已受理 API 调用不撤销提交，应保留 request ID 查询或重放受理事实。适配层限制并发调用数量，并在关闭时继续管理已接受资源。

[应用示例](../examples/python_api.py) 的 `--demo` 模式可在不接入模型时运行确定性 Session、异步 Session 或注册 Workflow。

维护 profile 在同一 service 视图提供 `converse`、`conversation`、`records`、`record`、`reports`、`report`、`acknowledge_report` 和 `retry_job`。`work.revise` 修订非活动工作的契约，`work.revoke` 撤销授权。服务级工作授权与信息请求通过 `requests()` 展示并使用明确的决定 API。策略与流程见 [Services](SERVICES.zh-CN.md#仓库维护p2)。

### Service 交付控制

Service 视图增加 `delivery`、`prepare_delivery`、`plan_delivery`、`inspect_delivery`、`request_delivery`、`refresh_delivery`、`batch` 和 `decide_batch`。本地与远程视图共用控制协议，AsyncHarness 提供对应可等待方法。固定候选、逐项决定和未知动作查证见 [仓库交付](DELIVERY.zh-CN.md)。

### Service 运维

Service 提供 health、operations、operation、operation_record、quiesce、resume_operations、audit、diagnose、保留预览/应用、备份/验证/恢复及升级准备/授权/确认。AsyncHarness 提供可等待方法；host.admin.service_upgrade_rollback 是明确的宿主本地操作。详见 [Service 运维](SERVICE_OPERATIONS.zh-CN.md)。
