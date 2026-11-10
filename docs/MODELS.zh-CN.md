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

协议适配器准备一份原生请求，通过所选后端执行，再转换为 `ModelResponse`。HTTP 与 SDK 共用工具 ID、结构化输出、思考状态回传、流式结束检查和用量观察。SDK 只执行模型请求，harness 工具继续使用原有权限和分发层。

`auto` 在连接准备阶段选择后端。请求发送后，错误和断流都不会改变该执行器。网关的 deadline、取消和故障处理适用于两种后端。用量用于观察，费用上限由模型提供商管理。

绑定保存实际后端及其修订，并在使用 SDK 时保存名称和版本。原 HTTP 执行不会因为安装 SDK 而改变路径。原 SDK 执行要求其保存的版本；卸载或升级后，需要恢复对应环境，或显式创建新执行。连接配置和模型能力继续使用原有身份检查。

Session、Run、Workflow、Service、benchmark、团队和 Subagent 共用模型选择入口。不同连接可以使用不同后端，程序注入的客户端保留由宿主定义的生命周期和绑定。

直接检查时，`ProtocolAdapter.transport_description` 提供实际后端、声明策略、选择理由和 SDK 版本。`ModelBinding.identity()` 提供持久身份，内容不包含连接秘密。
