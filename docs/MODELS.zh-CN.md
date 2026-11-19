# 模型连接与执行后端

[English](MODELS.md) | **简体中文** · [README](../README.zh-CN.md)

模型连接声明 API 协议与后端策略，profile 选择模型及其能力。模型名称和品牌不决定协议：只要接入地址支持，GPT、DeepSeek 和 Qwen 都可以使用 Chat Completions 连接。

基础安装通过 HTTP 支持全部协议。供应商 SDK 为可选依赖，与 HTTP 执行器共用请求和响应转换。

| 协议 | HTTP | 可选 SDK extra |
| --- | --- | --- |
| `chat_completions` | 支持 | `openai` |
| `openai_responses` | 支持 | `openai` |
| `anthropic_messages` | 支持 | `anthropic` |
| `dashscope_generation` | 支持 | `dashscope` |

在项目目录中使用 `python -m pip install -e ".[openai]"` 安装 extra，按需替换名称。也可以通过 `".[openai,anthropic,dashscope]"` 一起安装多个 SDK。安装包提供同名 extras。

Session、Run、Service、Workflow、团队与 subagent 的常规模型生成，默认上限统一为**每次请求 20,000 个输出 token**。根据提供商的计数方式，这可能包括思考 token。它是允许生成的上限，不要求模型用满。模型 profile 可通过 `max_output_tokens` 覆盖；宿主配置与 Run 使用 `output_tokens`，直接 Workflow 使用 `--max-output-tokens`。Session 也支持 `--worker-output-tokens` 或 `HARNESS_WORKER_OUTPUT_TOKENS`。已有配置及执行绑定中保存的显式上限继续生效。

默认上下文阈值为序列化请求的 100,000 个字符，并非提供商的 token 上下文窗口。可根据模型能力和任务设置 `context_chars`。Memory 筛选、Goal 检查与 benchmark 场景保留各自的独立限制。上下文压缩按完整装配请求检查该阈值。


模型请求默认上限为 500 秒。Session turn、Run episode 和 Service maintenance job 默认上限为 1500 秒；Run 和自动交互执行的总时长默认为 10800 秒。Maintenance job 最多调用模型 24 轮。Workflow 默认上限为 1500 秒，接受最多 64000 个输入字符。嵌套请求以自身截止时间与调用者剩余时间中更早到期者为准。显式宿主设置仍按其配置执行。

Goal 模型检查默认允许 8000 个输出 token、120 秒。上下文摘要首次允许 6000 个输出 token，生成不完整时仅重试一次，放宽到 12000；摘要长度目标是偏好，完整请求仍受配置的上下文容量约束。容量或无效输出补救在每次压缩中最多重写两次，历史覆盖边界保持原值。自动审批使用独立的[配置](APPROVALS.zh-CN.md)。

## 上下文窗口与工作记录

每个模型 profile 可独立设置 `context_chars`。profile 的值优先于宿主设置；省略时继承宿主值，宿主默认值为 100,000。profile 允许 10,000～2,000,000 个序列化请求字符。例如，同一个已配置连接可给两个模型分配不同窗口：

```json
{
  "profiles": {
    "lead": {"connection": "compatible", "model_env": "LEAD_MODEL", "context_chars": 240000},
    "coder": {"connection": "compatible", "model_env": "CODER_MODEL", "context_chars": 140000}
  }
}
```

应根据提供商实际 token 窗口和输出余量选择字符阈值。harness 不根据模型名称推测容量，也不将字符等同于 token。较大的窗口降低压缩频率，同时增加每次请求携带的历史量。`/model` 显示实际上下文阈值和输出上限；切换 profile 会采用对应配置。保存的 Run 保持原模型绑定，修改目录不会隐式改变已有 Run。

历史达到压力阈值时，harness 先生成语义工作摘要，再用归档引用替换较早消息。摘要保留有证据的事实、决定、假设和下一步动作，包括相关接口与文件版本。摘要属于参考材料；原始约束、权限及验证回执各自保持其权威性。摘要随普通对话 checkpoint 保存，并参与后续摘要。分块摘要共享生成目标，按较早摘要的实际长度规划后续范围。摘要超过建议长度时，只要完整序列化请求仍可容纳即可接受。需要时，压缩器在原任务期限内归档已消费结果、调整保留历史并重写工作摘要。提前压缩失败时继续使用仍可发送的历史；真实容量或补救失败保留原始档案与诊断引用。

模型可向 `compact` 工具提供可选的 `working_state` 文本，保留当前决定和紧接着的动作，最多 8,000 字符。文件读取返回不透明的文件系统 `version`，后续页面可将其作为 `expected_version` 检测修改；`offset` 按 Unicode 字符计，`limit` 按行计。版本变化时返回重新读取的位置，不返回另一快照的页面。

## 连接策略

模型目录的 `connections` 条目支持 `transport`：

| 值 | 新连接的行为 |
| --- | --- |
| `auto`（默认） | 优先使用已安装且受支持的 SDK，否则使用 HTTP 并记录选择理由 |
| `http` | 使用 HTTP |
| `sdk` | 要求对应 SDK；不可用或不受支持时在发送前失败 |

```json
{
  "version": 2,
  "connections": {
    "compatible": {
      "protocol": "chat_completions",
      "transport": "auto",
      "api_key_env": "MODEL_API_KEY",
      "base_url_env": "MODEL_ENDPOINT"
    }
  },
  "profiles": {
    "lead": {
      "connection": "compatible",
      "model_env": "LEAD_MODEL"
    }
  },
  "defaults": {"lead": "lead"}
}
```

将配置保存在工作区外，提供其引用的环境变量。所有后端都使用显式配置的接入地址与凭据。单模型环境变量模式使用 Chat Completions，通过 `HARNESS_MODEL_TRANSPORT` 选择后端，同样支持上述三个值。

SDK 适用性检查包版本和执行器合同。损坏的安装或客户端初始化错误会明确报错。若 SDK 从环境继承了额外连接头，则不参与自动选择；可以使用 HTTP 或清除这些 SDK 专用设置。运行时使用独立客户端，保留 TLS 验证、禁止继承环境代理和自动重定向，并关闭 SDK 内建重试。

## 共享执行与恢复

Session 的工作模型请求和使用模型的 Workflow 步骤在调用方剩余截止时间内最多尝试三次传输，每次尝试计入调用方的执行限额；取消或输入替换会停止旧请求。已经完成的工具调用和脚本步骤予以保留。托管 Run 保留原有持久 episode 重试策略，不在多个层次叠加重试。

模型连接错误仅作为观察记录，不会永久阻止后续请求。各执行控制器使用自己的有界重试和截止时间；未结束的请求继续占用实际并发资源。

协议适配器准备一份原生请求，通过所选后端执行，再转换为 `ModelResponse`。HTTP 与 SDK 共用工具 ID、结构化输出、思考状态回传、流式结束检查和用量观察。SDK 只执行模型请求，harness 工具继续使用原有权限和分发层。

`auto` 在连接准备阶段选择后端。请求发送后，错误和断流都不会改变该执行器。网关的 deadline、取消和故障处理适用于两种后端。用量用于观察，费用上限由模型提供商管理。

绑定保存实际后端及其修订，并在使用 SDK 时保存名称和版本。原 HTTP 执行不会因为安装 SDK 而改变路径。原 SDK 执行要求其保存的版本；卸载或升级后，需要恢复对应环境，或显式创建新执行。连接配置和模型能力继续使用原有身份检查。

Session、Run、Workflow、Service、benchmark、团队和 Subagent 共用模型选择入口。不同连接可以使用不同后端，程序注入的客户端保留由宿主定义的生命周期和绑定。

可选的[自动审批审核](APPROVALS.zh-CN.md)复用同一模型绑定与网关。可选择当前 Lead、实际请求者或专用 profile，每次审核使用独立上下文。

直接检查时，`ProtocolAdapter.transport_description` 提供实际后端、声明策略、选择理由和 SDK 版本。`ModelBinding.identity()` 提供持久身份，内容不包含连接秘密。

## 边界诊断

模型配置错误标明相关字段；JSON 语法错误提供行列位置。重试最终失败时说明实际尝试次数、配置上限及停止重试的原因。重试成功继续使用原有用量观察。

执行失败通过 `error_info` 返回 `type`、`code`、`message` 和 `details`。可用时，限额信息包含配置名称、配置值、观测值和单位。模型输出错误保留提供商结束原因、请求输出上限和已报告的输出／思考用量；流缺少结束标记与提供商明确报告达到输出上限分别描述。

超时诊断区分准备、排队和传输阶段，并展示 Gateway 上限、可能更短的客户端上限和实际生效的截止时间。`sent`、`settled` 表示请求是否已提交、执行是否已结束。Session turn 快照、Run outcome details、Workflow 错误结果和 Service job 保留同一份诊断。Service 的 `job`、`records jobs` 及工具错误结果均可查看。诊断不包含原始请求正文或思考文本。
