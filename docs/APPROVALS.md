# Approval review

**English** | [简体中文](APPROVALS.zh-CN.md) · [README](../README.md)

Tool permissions first resolve to `allow`, `deny`, or `ask`. The default approval mode is `manual`. Optional `auto_review` assesses each `ask` request against the original task, accepted amendments, and host policy in an independent model conversation. Existing allow and deny rules need no review call.

The reviewer approves a sufficiently supported action, rejects an action clearly outside authorization, and leaves an uncertain request for the operator. It can read bounded project evidence. Decisions retain the original parameters and use the existing approval publisher and execution checks.

## Configuration

Put this in an external host configuration for automatic interaction or Service:

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

Run specifications accept the same `approval_review` object. A non-null Run setting takes precedence over the host setting.

| Setting | Default | Meaning |
| --- | --- | --- |
| `mode` | `manual` | `manual` or `auto_review` |
| `model_source` | `lead` | Current Lead, actual `requester`, or host-selected `profile` |
| `model_profile` | Unset | Model catalog alias, required for `profile` |
| `requester_fallback` | Unset | Explicit `lead` fallback if requester provenance is unavailable |
| `kinds` | `["tool", "test_grant"]` | Approval kinds delegated to the reviewer |
| `timeout_seconds` | `120` | Review deadline, from `0.01` to `500` seconds |
| `max_turns` | `6` | Maximum review model calls, from `1` to `8` |

Every model source uses an independent context and its complete model binding through ModelGateway, including protocol, options, output allowance and context window. Calls and usage enter the existing observations. `requester` captures the actual subagent or teammate binding where available. Missing provenance requires human review unless the host explicitly sets `requester_fallback: "lead"`. A model connection failure also falls back to human review.

For a dedicated reviewer, set `model_source: "profile"` and `model_profile: "safety"`. Configure `safety` in the external model catalog; connection secrets remain in the host environment. See [Models](MODELS.md).

The reviewer reads UTF-8 files through `read_review_file(path, offset=0, limit=10000)`. Offsets count Unicode characters. Pages describe the file version, viewed range and next offset. Missing paths and ordinary query errors return structured tool feedback; the model decides which evidence matters and whether another page is needed. Changed evidence returns to the conversation for a current decision. Reads remain limited to 16 files, 16 MiB of distinct content and 64 pages, with an 8 MiB limit per file.

The final result contains `decision` and `reason`; `risk` is optional. There is no separate `authorization` decision. Invalid output is explained to the model so it can correct its answer. Model-service errors allow up to three consecutive attempts within the same deadline and model-call limit. Historical failures do not disable future model requests.

An interrupted review with no active owner and a still-pending request is explained to the reviewer for reassessment. Completed decisions and unknown submission confirmation are preserved. A failed attempt does not restart on each service poll. To explicitly retry a pending Service request without approving it:

```python
service.retry_approval_review(request, request_id="review-retry-1")
```

The CLI and Text UI expose the same retry:

```sh
homebrew-harness service review-retry SERVICE_ID REQUEST_ID REVISION --retry-id retry-1
```


Requests retain their exact parameters, policy, ownership and execution bindings. File coverage is information for the reviewer, rather than a requirement to read every file in full.

`find_review_files(pattern="**/*", cursor=0, limit=50)` locates visible files; `search_review_text(text, pattern="**/*", cursor=0, limit=50)` returns the first matching line per file. Both use bounded navigation and return a continuation cursor or a reason to narrow the query. Navigation visits at most 10,000 entries per call; literal search shares an 8 MiB content allowance with a 2 MiB per-file allowance. Navigation and file reads share the existing page limit. Search hits and stored path hints help locate evidence; read relevant files before selecting them as decision evidence.

Service review contexts reuse a small directory of previously observed code paths and versions. Communication review uses its fixed PR snapshot when present; delivery review uses the candidate workspace. A previously recorded version is a navigation hint, while the reviewer decides relevance and the runtime checks its selected current evidence.

The reviewer receives the remaining model turns and observed paths. It can continue reading or return a decision. Review records retain bounded `observations` describing successful and failed queries, plus `observed_evidence` even when review stops. These observations contain paths, ranges, versions and result codes; they are separate from the final verdict's `evidence` and do not authorize execution.

The optional `evidence_paths` result names which observed paths support the decision; an empty list means no file evidence is needed. Immediate tool execution rechecks that selected evidence and the actual request bindings, without treating unrelated workspace changes as an approval failure.

## Runtime behavior

Session reviews include original requirements, input version, execution role, workspace and tool-specific host details. Uncertainty returns to the approval broker. Without a broker, one-shot execution retains the terminal prompt; a noninteractive call returns `approval_required`. Session broker requests belong to their active turn and deadline.

Run review begins after a durable request is accepted. Approved tool requests still need `execute_approved`; the original call is consumed once when accepted. Member test renewals retain task scope and registered run/time limits. Attention questions and the managed member/Lead boundary keep their existing behavior.

Service reviews business requests asynchronously; its native Runs review tool requests. Explicitly include `work_authority`, `delivery_authority`, or `communication_authority` in `kinds` to delegate the corresponding business decisions, together with the configured project policy. The reviewer receives the fixed proposal, publication plan or message, and current policy. Platform PR approval and merge privileges continue to depend on the configured account and project policy. Service business requests currently have no captured model requester: `requester` requires the explicit Lead fallback or human review for them.

Human fallback uses the existing terminal, HTTP/Text UI and Python API. Durable Run and Service requests remain pending across worker restarts.

## Python API and records

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

Answer remaining requests through `harness.requests`, using their exact reference and revision. See [Python API](PYTHON_API.md).

The runtime saves reviewer binding, request/context versions, verdict, short reason and evidence references in `approval_reviews`. A changed request or earlier human decision supersedes the review. Interrupted reviews and unknown publication confirmation do not trigger repeated model calls or publication; the native decision record remains authoritative. Session review records follow checkpoint retirement and restoration.

Boundary failures in an approval attempt retain `error_info`, including the selected deadline or file/page/content limit. The reason identifies the execution boundary; ordinary file-query errors continue to return to the reviewer as tool feedback.
