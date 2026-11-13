# 自动审批审核

[English](APPROVALS.md) | **简体中文** · [README](../README.zh-CN.md)

工具权限先判断为 `allow`、`deny` 或 `ask`。默认审批模式为 `manual`。可选的 `auto_review` 使用独立模型上下文，将每个 `ask` 请求与原始任务、已接受修订及宿主政策比对。规则直接允许或拒绝的调用不会额外发起审核。

依据充分的动作自动批准，明确超出授权范围的动作自动拒绝，拿不准的请求交给部署者。审核器可读取有限的项目证据。决定保留原始参数，继续经过已有审批提交和执行校验。

## 配置

在默认交互入口或 Service 使用的外部宿主配置中加入：

```json
{
  "approval_review": {"mode": "auto_review", "model_source": "lead"}
}
```

```sh
homebrew-harness --host-config ~/.config/homebrew-harness/host.json
homebrew-harness session --approval-review '{"mode":"auto_review"}'
homebrew-harness run --approval-review '{"mode":"auto_review"}' start --spec run.json
```

Run 规格接受同一个 `approval_review` 对象；非空 Run 配置优先于宿主配置。

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `mode` | `manual` | `manual` 或 `auto_review` |
| `model_source` | `lead` | 当前 Lead、实际 `requester` 或宿主指定 `profile` |
| `model_profile` | 未设置 | `profile` 来源必须提供的模型目录别名 |
| `requester_fallback` | 未设置 | 缺少可信请求者绑定时显式回退 `lead` |
| `kinds` | `["tool", "test_grant"]` | 委托审核器处理的审批种类 |
| `timeout_seconds` | `15` | 审核 deadline，允许 `0.01`～`120` 秒 |
| `max_turns` | `3` | 最多调用审核模型的次数，允许 `1`～`8` |

所有来源都使用独立上下文，并通过 ModelGateway 使用完整模型绑定，包括协议、参数、输出额度和上下文窗口。调用次数和用量进入已有观察记录。`requester` 在可用时捕获实际 subagent／teammate 的绑定。缺少可信来源时默认转人工，宿主可明确设置 `requester_fallback: "lead"`。模型连接失败同样转人工。

专用审核模型配置为 `model_source: "profile"`、`model_profile: "safety"`。`safety` 需在外部模型目录中定义，连接凭据仍由宿主环境提供，参见[模型接入](MODELS.zh-CN.md)。

审核器通过 `read_review_file(path, offset=0, limit=10000)` 读取 UTF-8 文件，偏移以 Unicode 字符计数。返回结果描述文件版本、已读范围与下一页偏移。路径缺失和普通查询错误作为结构化工具反馈，由模型决定证据是否相关、是否继续读取；证据变更回到当前对话重新判断。仍保留单次审核 16 个文件、16 MiB 不同文件内容、64 页及单文件 8 MiB 的读取上限。

最终结果包含 `decision` 和 `reason`，`risk` 可选，不再单独返回 `authorization` 决定。无效输出会反馈给模型纠正。模型服务错误在同一截止和调用轮次内最多连续尝试三次，历史失败不会禁用后续模型请求。

上次审核中断、原请求仍待处理且没有活跃审核者时，会向模型说明中断并重新审核。已提交决定和提交确认未知的记录继续保留。失败尝试不会在每次 Service 轮询时无限重启。显式重试一个待处理的 Service 请求而不批准它：

```python
service.retry_approval_review(request, request_id="review-retry-1")
```

CLI 和 Text UI 提供相同重试入口：

```sh
homebrew-harness service review-retry SERVICE_ID REQUEST_ID REVISION --retry-id retry-1
```


原请求的参数、政策、执行归属及动作绑定继续有效。文件是否读完是供模型判断的信息，不要求所有查阅文件都完整读取。

最终结果可用 `evidence_paths` 选择决定所依赖的已观察路径，空列表表示不需要文件证据。即时工具执行前复核这些证据与实际请求绑定，不把无关工作区变化当成审批失败。

## 运行方式

Session 审核包含原始约束、输入版本、执行角色、工作区和工具的宿主信息。不确定时回到审批 broker。无 broker 的一次性调用保留终端提示；非交互调用返回 `approval_required`。Session broker 请求受当前轮次和 deadline 约束。

Run 在持久请求受理后审核。工具请求批准后仍需 `execute_approved`，原操作受理时只消费一次。成员测试续期保留任务范围和注册次数／时间上限。Attention 问题及成员与 Lead 的边界保留原有语义。

Service 异步审核业务请求，其原生 Run 自行审核工具请求。需要在 `kinds` 中明确加入 `work_authority`、`delivery_authority` 或 `communication_authority`，并配置相应项目政策，才能委托对应业务决定。审核器读取固定提案、发布计划或消息及当前政策。平台 PR approve、merge 权限仍取决于所配置账户和项目政策。Service 业务请求目前没有捕获模型请求者绑定，使用 `requester` 时需要显式 Lead 回退或人工审核。

人工兜底使用原有终端、HTTP/Text UI 和 Python API。Run、Service 的持久请求可跨 worker 重启继续等待。

## Python API 与记录

```python
from homebrew_harness import Harness, HostConfig

config = HostConfig.from_files(
    approval_review={"mode": "auto_review", "model_source": "lead"},
)
with Harness(workdir="project", config=config) as harness:
    session = harness.sessions.open()
    turn = session.submit("Run the registered checks and report the result")
    report = turn.wait(60)
    pending = turn.requests()
```

调用方可通过 `harness.requests` 回答剩余请求，决定绑定精确引用和版本。参见[Python API](PYTHON_API.zh-CN.md)。

运行时将审核模型绑定、请求与上下文版本、结论、简短理由和证据引用保存在 `approval_reviews`。请求改变或人工先决定时，自动审核作废。被中断的审核及提交确认未知的记录不会自动重复调用或发布，原审批记录仍是执行依据。Session 审核记录随 checkpoint 一起退休和恢复。
