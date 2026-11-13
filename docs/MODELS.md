# Model connections and backends

**English** | [简体中文](MODELS.zh-CN.md) · [README](../README.md)

A model connection declares an API protocol and a transport policy. A profile selects a model and its capabilities. Model names and brands do not determine the protocol: GPT, DeepSeek and Qwen can all use a Chat Completions connection when their endpoint supports it.

The base installation uses HTTP for all supported protocols. Optional vendor SDKs provide another executor for the same request and response conversion.

| Protocol | HTTP | Optional SDK extra |
| --- | --- | --- |
| `chat_completions` | Supported | `openai` |
| `openai_responses` | Supported | `openai` |
| `anthropic_messages` | Supported | `anthropic` |
| `dashscope_generation` | Supported | `dashscope` |

From a project checkout, install an extra with `python -m pip install -e ".[openai]"`, replacing the extra name as needed. Multiple extras can be installed together with `".[openai,anthropic,dashscope]"`. The installed package supports the same extras.

## Connection policy

The model catalog's `connections` entries accept `transport`:

| Value | New connection behavior |
| --- | --- |
| `auto` (default) | Use an installed, supported SDK; otherwise use HTTP and record the selection reason |
| `http` | Use HTTP |
| `sdk` | Require the matching SDK; fail before sending if it is unavailable or unsupported |

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

Store this configuration outside the workspace and supply its referenced environment variables. Every backend uses the explicitly configured endpoint and credential. The single-model environment mode uses Chat Completions and accepts `HARNESS_MODEL_TRANSPORT`, with the same three values.

SDK eligibility checks the package version and executor contract. A broken installation or client initialization error is reported. SDKs with implicit custom headers from their environment are ineligible; use HTTP or clear those SDK-specific settings. The runtime uses independent clients with TLS verification, no environment proxy inheritance, no redirects, and no SDK automatic retries.

## Shared execution and recovery

Session worker requests and model-backed Workflow steps make up to three transport attempts within the caller's remaining deadline. Each attempt counts against the caller's execution limits; cancellation and replaced input stop the old request. Completed tool calls and script steps are retained. Managed Runs retain their durable episode retry policy rather than multiplying retries across layers.

Model connection errors remain observations. They do not permanently block later requests. Each execution controller owns its bounded retries and deadline; an unfinished request still occupies its actual concurrency resources.

The protocol adapter prepares one native request, executes it through the selected backend, and converts its result to `ModelResponse`. HTTP and SDK share tool IDs, structured output, reasoning replay, stream completion checks and usage observations. The SDK executes model requests; harness tools continue through the existing permission and dispatch layer.

`auto` selects during connection preparation. A sent request stays with that executor, including errors and interrupted streams. Gateway deadlines, cancellation and failure handling apply to both backends. Usage remains observational; spending limits belong to the provider.

Bindings save the resolved backend and its revision, along with SDK name and version where applicable. A saved HTTP execution continues over HTTP after an SDK is installed. A saved SDK execution requires its saved SDK version; removing or upgrading it requires restoring the environment or explicitly starting a new execution. Connection settings and profile capabilities retain their existing identity checks.

Session, Run, Workflow, Service, benchmark, teams and subagents use the shared model selection path. Each connection can use its own backend. Programmatic injected clients retain their host-defined lifecycle and binding.

Optional [Approval review](APPROVALS.md) uses the same model bindings and gateway. Select the current Lead, actual requester or a dedicated profile; every review starts an independent conversation.

For direct inspection, `ProtocolAdapter.transport_description` reports the selected backend, declared policy, selection reason, and SDK version. `ModelBinding.identity()` exposes the persistent identity without connection secrets.
