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

审核器通过 `read_review_file(path, offset=0, limit=10000)` 分块读取 UTF-8 项目文件，偏移和长度以 Unicode 字符计数。返回结果包含 `next_offset`、文件哈希与 `read_complete`，可以在当前上下文中继续读取。单次审核最多读取 16 个文件、16 MiB 不同文件内容和 64 页，单文件上限为 8 MiB。自动批准前，每个请求读取的文件必须完整覆盖，且分块读取和最终决定时的文件版本一致。必要证据缺失、变更或未读完时转人工审核。

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
