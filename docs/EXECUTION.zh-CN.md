# 控制耗时操作

[English](EXECUTION.md)

耗时工具可以在执行仍由运行时管理时，把控制权交回模型。模型可以观察输出、等待、发送支持的输入或停止操作。Session、Run、Subagent、团队成员和 Service 模型任务，在原工具已获授权时，会装配同一套执行控制入口。

## 执行模式

Bash、注册测试、子任务 `task` 和 `workflow` 接受 `execution` 选项：

```json
{
  "command": "make test",
  "execution": {"mode": "interactive", "wait_seconds": 10}
}
```

默认 `interactive`：短暂等待后返回最终结果或 `execution_id`。等待结束不会停止进程。选择 `one_shot` 可等待最终结果：

```json
{
  "command": "make test",
  "execution": {"mode": "one_shot", "timeout_seconds": 900}
}
```

`timeout_seconds` 是可选的执行上限；省略或 `null` 不增加单次墙钟期限。父 Run／任务期限及宿主明确配置的限制继续生效。`wait_seconds` 为 0–60 秒，只控制交互模式的一次观察等待，不能传给一次性模式。

模式不改变权限、目录或容器政策。短调用通常会在首次等待内完成。Bash 原有 `run_in_background` 仍属于后台模块；统一观察、输入和控制采用新的执行选项。

其它注册工作区工具可以通过统一包装入口选择模式，避免每个工具重复携带选项 Schema：

```json
{"tool": "read_file", "arguments": {"path": "report.txt"}, "execution": {"mode": "interactive", "wait_seconds": 0}}
```

将该对象交给 `invoke_tool`。包装入口只能调用当前上下文原本可见的工具。

## 观察和控制

| 工具 | 功能 |
| --- | --- |
| `list_executions` | 查找当前上下文拥有的操作 |
| `get_execution` | 查看状态、可用控制和结果信息 |
| `read_execution_output` | 按字符游标读取增量输出 |
| `wait_execution` | 等待收尾，等待到期不停止操作 |
| `send_execution_input` | 在支持时发送有 request ID 的输入 |
| `stop_execution` | 请求取消，再观察实际收尾 |

控制使用 `execution_id`。输出接受 `cursor` 和 `limit`，每次最多 16,000 字符；`stream="result"` 可分页读取较大的最终结果，默认读取进程输出。完成事件持久送给拥有者模型，原始日志不会逐行触发模型调用。

等待结束、没有新输出、执行仍在运行都不是验证通过。取消回执表示请求已受理；资源可能仍在停止，应查看 `execution_pending` 和 `cleanup` 后再重启有冲突的工作。

输入取决于操作能力。Bash pipe 支持 stdin，子任务支持影响后续模型步骤的 steering，测试一般不接收交互输入。修改命令或测试范围时，先停止或完成旧尝试，再以新参数启动。输入 request ID 防止重复应用，状态分别表示受理、应用和拒绝。

## 局部测试

配方可以提供测试选择适配器。宿主保留启动器、环境、测试根目录、角色和资源政策，模型选择范围内的用例：

```json
{
  "name": "unit-tests",
  "selection": {"targets": ["tests.test_parser.ParserTests.test_empty_input"]},
  "execution": {"mode": "interactive", "wait_seconds": 0}
}
```

省略 `selection` 执行完整配方。unittest 支持模块、类和方法名；pytest 支持文件、用例 ID 及可选 `filter`，注册执行环境需要已安装 pytest。没有适配器的配方会拒绝选择参数。

仓库外注册配置示例：

```json
{
  "version": 1,
  "limits": {"max_runs": 20, "total_seconds": null},
  "recipes": [{
    "name": "unit-tests",
    "builtin": "unittest",
    "test_roots": ["tests"],
    "roles": ["lead", "subagent", "teammate"],
    "timeout": null,
    "cpu_seconds": null,
    "authorization": "confirm"
  }]
}
```

累计测试时间、单次墙钟时间和每个进程的 CPU 秒数默认不设上限，测试墙钟耗时继续记录。显式有限上限、执行次数、父期限及容器 CPU 分配、内存和 PID 边界继续生效。已有 Run 保留其捕获的政策，新的授权执行才采用新政策。

回执记录选择范围、`test_scope`（`full`／`selected`）、源码快照、退出码和收尾。局部通过不能满足完整 `tests_passed` 条件或 Service 完整质量门；零用例、取消、超时和源码变化不算通过。

宿主定义的 `tests_passed` 条件可以明确包含同样的 `selection` 对象，要求局部验证；不指定时要求完整配方。较新的局部失败会使较旧的完整通过证据失效。

## MCP 与 Workflow

MCP 保留原服务 Schema。显式选择模式使用 `invoke_mcp_tool`：

```json
{
  "tool": "mcp__docs__search",
  "arguments": {"query": "execution lifecycle"},
  "execution": {"mode": "interactive", "wait_seconds": 0}
}
```

包装入口沿用原参数校验和权限。每个连接默认允许四项并发请求，宿主可将 `concurrency` 配置为 1–32。取消一项请求不会关闭仍服务其它请求的共享连接，服务端提供的进度进入执行输出。本地取消不能证明远端效果已撤销，不确定结果保留原恢复合同。

注册 `workflow` 工具接受同样的模式选项；步骤和 checkpoint 仍由原 Workflow 管理，资源收尾后通过现有 `resume_from_run_id` 合同继续。

## Python 与 Service

Session 或 Run 提供按拥有者限定的执行视图：

```python
for snapshot in session.executions.list():
    operation = session.executions.get(snapshot.ref.id)
    print(operation.output(cursor=0, limit=4000))
    operation.stop(request_id="operator-stop")
```

支持的输入使用 `operation.input(text, request_id=...)`，观察使用 `operation.wait(...)`，最终结果使用 `operation.result()`。Snapshot 的 `details.inputs` 显示输入的受理、应用或拒绝状态，不包含输入正文；同一输入 request ID 在执行结束后仍可幂等查询。模型 stdin 按进程原来的工作目录审批。只读 Run 视图不能提交控制。Service Work 的 `executions()`、`execution(id)`、`execution_output(id)`、`stop_execution(id, ...)`、`send_execution_input(id, text, ...)` 通过 HTTP 控制 API 工作。

重启保留尝试和结果。无法重新附着的执行显示中断及收尾／效果不确定性，不会自动重放；活动和未收尾记录会阻止 checkpoint 退休。

## 耗时宿主 Hook

短回调可保持 inline。可导入的模块级回调可选择独立进程：

```python
hooks.register_hook("PreToolUse", assess_request, execution="process")
```

进程 Hook 保留原合同，能够随拥有者操作取消，执行前校验源码身份。闭包和需要宿主实时对象的回调应使用合作式运行设施；不会把这类 Python 线程描述成可直接强杀的进程。待收尾状态持续可见，直到执行真实结束。
