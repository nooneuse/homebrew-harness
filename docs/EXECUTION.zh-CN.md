# 控制耗时操作

当前预算默认值与迁移见[运行预算](LIMITS.zh-CN.md)。

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

`timeout_seconds` 是可选的执行上限；省略或 `null` 不增加单次墙钟期限。父 Run／任务期限及宿主明确配置的限制继续生效。`wait_seconds` 为有限非负秒数，只控制交互模式的一次观察等待，不能传给一次性模式。

交互执行可以跨越正常的 Run Episode 边界。Checkpoint 保留执行 ID，下一段可以继续观察或控制同一次执行。仍有活动拥有者的 `started` 操作保持进行中；失去拥有者或效果未知的操作需要恢复检查。显式停止和父任务期限仍会结束执行。

模式不改变权限、目录或容器政策。短调用通常会在首次等待内完成。Bash 原有 `run_in_background` 仍属于后台模块；统一观察、输入和控制采用新的执行选项。

`delete_file` 在与 `write_file`、`edit_file` 相同的写入授权范围内删除一个普通文件，可传入 `read_file` 返回的 `expected_version`。已不存在的文件单独报告；目录和 symlink 路径会被拒绝。删除不要求 Bash 权限；删除后的持久化失败会保留不确定结果供恢复检查。

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

控制使用 `execution_id`。输出接受 `cursor` 和 `limit`，默认每次窗口为 16,000 字符，可配置；`stream="result"` 可分页读取较大的最终结果，默认读取进程输出。完成事件持久送给拥有者模型，原始日志不会逐行触发模型调用。

等待结束、没有新输出、执行仍在运行都不是验证通过。取消回执表示请求已受理；资源可能仍在停止，应查看 `execution_pending` 和 `cleanup` 后再重启有冲突的工作。

输入取决于操作能力。Bash pipe 支持 stdin，子任务支持影响后续模型步骤的 steering，测试一般不接收交互输入。修改命令或测试范围时，先停止或完成旧尝试，再以新参数启动。输入 request ID 防止重复应用，状态分别表示受理、应用和拒绝。

## 保存的文件输出

`read_file` 识别当前上下文或明确继承的上下文保存的文件结果，直接返回记录的内容，并在 `source` 中单独标注历史来源路径、版本和位置，避免再把整份 JSON 响应作为文件正文嵌套。原归档保持不变；可用 `raw=true` 查看原始内容。视图的 offset 是记录正文内的 Unicode 字符位置，升级后打开旧归档应从 0 开始；分页使用 `next_offset` 和返回的归档 `version`。历史来源版本不能证明当前工作文件未变化。完整 JSON 返回值连同转义与元数据一起受文件输出预算约束。

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

内置 unittest 配方还可声明宿主拥有的 `default_targets`（非空、位于声明 `test_roots` 之下的点分 unittest 目标列表）和 `min_tests`（正整数）。没有显式选择时只运行配置的默认目标；未配置时保持原有发现行为，`min_tests` 只作用于该完整运行，零用例永不通过。显式选择完全取代默认目标，回执标记 `test_scope: "selected"`，且不适用完整最低数量。这两个可选字段会选用独立的扩展内置启动器，并绑定进配方与启动器指纹，缓存证据不能跨配置复用。未声明字段的配方保持原有启动器字节、行为和证据哈希；容器注册必须精确固定匹配的启动器，命令之间没有回退。

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

回执记录选择范围、`test_scope`（`full`／`selected`）、源码快照、退出码和收尾。启用可选字段的完整运行还会记录配置的 `default_targets` 和 `min_tests`。局部通过不能满足完整 `tests_passed` 条件或 Service 完整质量门；零用例、取消、超时和源码变化不算通过。

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

包装入口沿用原参数校验和权限。每个连接默认允许四项并发请求，宿主可将 `concurrency` 配置为正整数。取消一项请求不会关闭仍服务其它请求的共享连接，服务端提供的进度进入执行输出。本地取消不能证明远端效果已撤销，不确定结果保留原恢复合同。

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

## 持久化与关闭窗口

状态持久化使用独立的 180 秒总窗口。数据库连接准备默认 15 秒、等待写锁默认 30 秒；取得写锁后，事务使用 60 秒窗口。checkpoint 与工具结果写入仅在尚未开始写入或确认回滚后重试，最多 3 次，并共用原持久化期限。重试保留原结果与执行身份，提交确认不明仍需查证。

资源关闭默认允许 60 秒，已有显式配置继续生效。持久化可以在模型或 Episode 期限结束后保存已完成事实，同时受外层资源关闭窗口约束。存储超时会说明操作、阶段、上限、已耗时和提交／回滚状态；调用者的一次观察等待与这些窗口分别处理。

模型单次请求默认使用 500 秒窗口；自动审查的整体任务时间默认无硬限。Git 查询窗口默认为 120 秒，可通过 runtime_limits 配置，显式父期限继续生效。

MCP 建连和容器管理默认允许 90 秒，容器检查允许 15 秒。备份、审计等 Service 运维操作整体时间默认无硬限；状态查询的一次观察等待单独处理。

## 工作状态压缩

完整任务、已接受补充和计划独立保留，工作摘要优先保存未完成事项、相关接口和证据；transcript 与操作索引可检索。默认启用自动压缩；模型仍可调用 `compact`，供应商确认溢出时另走恢复。已保存的 `auto_compact: false` 在恢复时仍生效。新受理的托管 Run 持久保存有效 `runtime_limits`，更换 worker 或宿主默认配置不会改写它。摘要建议为 8,000 字符，10,000 字符是名义窗口，实际序列化上下文能容纳时可保留更长摘要。读取覆盖、分块、重写与名义窗口可配置；可选压缩失败保留原始历史并返回观察。

原生 Run 自动审查会在持久请求公开前登记活动。正常模型或 Episode 边界在关闭 Session 前等待这项关键活动完成；显式暂停、取消、请求失效和绝对期限继续生效，人工决定优先于迟到的模型输出。新 Run 的审批请求默认无额外计时有效期，仍绑定版本和权限；明确的有效期及 Run 父期限继续生效。
