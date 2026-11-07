"""Messages content blocks, exact thinking signatures and local function tools."""

import json
from homebrew_harness.execution.model_protocol import (
    ModelProtocolError,
    ModelResponse,
    ToolInvocation,
)
from .common import usage


def prepare(request, rows, options, capabilities):
    if options.get("temperature", 0) > 1:
        raise ModelProtocolError("Anthropic temperature must be between zero and one.")
    system, result = [], []
    for row in rows:
        role, text = row["role"], row.get("content") or ""
        if role in {"system", "developer"}:
            system.append("[" + role + "]\n" + text)
            continue
        if role == "tool":
            role = "user"
            blocks = [
                {
                    "type": "tool_result",
                    "tool_use_id": row["tool_call_id"],
                    "content": text,
                }
            ]
        elif row.get("_replay"):
            blocks = row["_replay"]["blocks"]
        else:
            blocks = [{"type": "text", "text": text}] if text else []
            blocks.extend(
                {
                    "type": "tool_use",
                    "id": call["id"],
                    "name": call["function"]["name"],
                    "input": json.loads(call["function"]["arguments"]),
                }
                for call in row.get("tool_calls") or []
            )
        if result and result[-1]["role"] == role:
            result[-1]["content"].extend(blocks)
        else:
            result.append({"role": role, "content": blocks})
    body = {
        "model": request.model,
        "system": "\n\n".join(system),
        "messages": result,
        "max_tokens": request.max_output_tokens,
    }
    if request.tools:
        body["tools"] = [
            {
                "name": t["function"]["name"],
                "description": t["function"]["description"],
                "input_schema": t["function"]["parameters"],
            }
            for t in request.tools
        ]
    contract = request.output_contract
    if contract:
        if contract.get("schema"):
            body["output_config"] = {
                "format": {"type": "json_schema", "schema": contract["schema"]}
            }
        else:
            body["system"] += (
                "\nReturn exactly one JSON object. The host validates the complete result."
            )
    thinking = options.get("thinking")
    if thinking is True or isinstance(thinking, dict):
        if not capabilities.thinking:
            raise ModelProtocolError("Thinking is not supported by this model profile.")
        body["thinking"] = (
            thinking
            if isinstance(thinking, dict)
            else {
                "type": "enabled",
                "budget_tokens": 1024,
            }
        )
        config = body["thinking"]
        if config.get("type") == "enabled":
            budget = config.get("budget_tokens")
            if (
                type(budget) is not int
                or not 1024 <= budget < request.max_output_tokens
            ):
                raise ModelProtocolError(
                    "Anthropic thinking requires 1024 <= budget_tokens < max_output_tokens; configure a compatible output limit."
                )
        elif config.get("type") not in {"disabled", "adaptive"}:
            raise ModelProtocolError("Unsupported Anthropic thinking configuration.")
    elif thinking is False:
        body["thinking"] = {"type": "disabled"}
    return body


def decode(value):
    parts, ids = [], set()
    blocks = value.get("content") or []
    for block in blocks:
        kind = block.get("type")
        if kind == "text":
            parts.append({"type": "text", "text": block["text"]})
        elif kind == "tool_use":
            call = ToolInvocation(
                block["id"],
                block["name"],
                json.dumps(block["input"], ensure_ascii=False),
            )
            if call.id in ids:
                raise ModelProtocolError("Messages tool identities collide.")
            ids.add(call.id)
            parts.append({"type": "tool_call", "call": call.history()})
        elif kind not in {"thinking", "redacted_thinking"}:
            raise ModelProtocolError("Server-owned Messages tools are not enabled.")
    reason = value.get("stop_reason")
    finish = (
        "tool_calls"
        if reason == "tool_use" and ids
        else "completed"
        if reason in {"end_turn", "stop_sequence"}
        else "incomplete"
        if reason == "max_tokens"
        else "refused"
        if reason == "refusal"
        else "continuation"
        if reason == "pause_turn"
        else "failed"
    )
    return ModelResponse(
        tuple(parts),
        finish,
        usage(value.get("usage"), protocol="anthropic_messages"),
        {"blocks": blocks},
        value.get("id"),
    )


def streamed(events):
    value, blocks, fragments, ended = {}, {}, {}, set()
    for event in events:
        kind = event.get("type")
        if kind == "message_start":
            value = event["message"]
        elif kind == "content_block_start":
            index = event["index"]
            if index in blocks:
                raise ModelProtocolError("Duplicate Messages stream block.")
            blocks[index] = event["content_block"]
        elif kind == "content_block_delta":
            index, delta = event["index"], event["delta"]
            if index not in blocks or index in ended:
                raise ModelProtocolError("Messages stream block ordering is invalid.")
            block = blocks[index]
            field = {
                "text_delta": "text",
                "thinking_delta": "thinking",
                "signature_delta": "signature",
            }.get(delta.get("type"))
            if field:
                block[field] = block.get(field, "") + delta.get(field, "")
            elif delta.get("type") == "input_json_delta":
                fragments[index] = fragments.get(index, "") + delta.get(
                    "partial_json", ""
                )
            else:
                raise ModelProtocolError("Unsupported Messages stream delta.")
        elif kind == "content_block_stop":
            index = event["index"]
            if index not in blocks:
                raise ModelProtocolError("Unknown Messages stream block completion.")
            if index in fragments:
                try:
                    blocks[index]["input"] = json.loads(fragments[index])
                except ValueError as error:
                    raise ModelProtocolError(
                        "Messages tool JSON is incomplete."
                    ) from error
            ended.add(index)
        elif kind == "message_delta":
            value.update(event.get("delta") or {})
            value["usage"] = {**value.get("usage", {}), **event.get("usage", {})}
        elif kind == "message_stop":
            if set(blocks) != ended:
                raise ModelProtocolError("Messages stream has unfinished blocks.")
            value["content"] = [blocks[i] for i in sorted(blocks)]
            return decode(value)
        elif kind == "error":
            raise ModelProtocolError("Messages stream returned an error.")
    raise ModelProtocolError("Messages stream ended without message_stop.")
