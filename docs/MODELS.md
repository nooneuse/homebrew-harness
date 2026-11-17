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

General model generation defaults to a maximum of **20,000 output tokens per request** across Session, Run, Service, Workflow, teams and subagents. The allowance may include reasoning tokens, depending on the provider. It is a ceiling, not a target. A model profile can override it with `max_output_tokens`; host settings and Run specifications use `output_tokens`, and direct Workflow execution uses `--max-output-tokens`. Session also accepts `--worker-output-tokens` or `HARNESS_WORKER_OUTPUT_TOKENS`. Explicit limits saved in existing configurations and execution bindings remain in effect.

The default context threshold is 100,000 serialized request characters, not the provider's token context window. Configure `context_chars` for the selected model and workload. Compaction, Memory selection, Goal checking and benchmark scenarios retain their separate limits.


The default model request deadline is 500 seconds. Session turns, Run episodes and Service maintenance jobs have a 1,500-second deadline; Run and automatic interaction executions have a 10,800-second total deadline. Maintenance jobs allow up to 24 model calls. Workflow defaults to 1,500 seconds and accepts up to 64,000 input characters. Every nested request uses the earlier of its own deadline and the caller's remaining deadline. Explicit host settings retain their configured limits.

Goal model checks default to 8,000 output tokens and 120 seconds. Context summaries allow 6,000 output tokens, with one 12,000-token retry for incomplete generation; their retained text and history coverage limits stay the same. Approval review has its own [configuration](APPROVALS.md).

## Context windows and working state

Set `context_chars` separately on each model profile. A profile value overrides the host setting; an omitted value inherits the host setting, whose default is 100,000. Valid profile values range from 10,000 to 2,000,000 serialized request characters. For example, these profiles can use the same configured connection with different windows:

```json
{
  "profiles": {
    "lead": {"connection": "compatible", "model_env": "LEAD_MODEL", "context_chars": 240000},
    "coder": {"connection": "compatible", "model_env": "CODER_MODEL", "context_chars": 140000}
  }
}
```

Choose a character threshold appropriate to the provider's actual token window and output allowance. The harness does not infer capacity from model names or treat characters as tokens. Larger windows reduce compaction frequency and increase the history sent on each request. `/model` shows the effective context threshold and output limit; switching profiles adopts their settings. Saved Runs keep their model binding, so editing a catalog does not silently change an existing Run.

Under history pressure, the harness creates a semantic working summary before replacing older messages with an archive reference. Summaries retain confirmed facts, decisions, hypotheses and pending actions, including relevant interfaces and file versions. They remain reference material; original constraints, permissions and verification receipts retain their own authority. The summary is saved in the ordinary conversation checkpoint and feeds later summaries.

A model can pass optional `working_state` text to the `compact` tool to preserve its current decisions and next action; the limit is 8,000 characters. File reads return an opaque filesystem `version`. Subsequent pages can supply it as `expected_version` to detect edits; `offset` counts Unicode characters and `limit` counts lines. A changed version reports a restart offset instead of returning a page from a different snapshot.

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

## Boundary diagnostics

Model configuration errors identify the affected field, with line and column for invalid JSON. Final retry failures include the actual attempt count, configured attempt limit and the reason retrying stopped. Successful retries use the existing usage observations.

Execution failures expose `error_info` with `type`, `code`, `message` and `details`. Limit details name the configuration, configured value, observed value and unit where available. Model output failures retain the provider finish reason, requested output allowance and reported output/reasoning usage. A missing stream terminator is reported separately from a provider-declared output limit.

Timeout diagnostics distinguish preparation, queue waiting and transport, and show the gateway limit, any tighter client limit and the effective deadline. `sent` and `settled` describe whether execution was submitted and has finished. Session turn snapshots, Run outcome details, Workflow error results and Service jobs retain the same diagnostics. Service `job` and `records jobs` display them; tool error results include them as well. Raw request bodies and reasoning text are excluded.
