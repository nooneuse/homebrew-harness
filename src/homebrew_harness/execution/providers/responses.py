"""Responses items with explicit local replay and no server-owned tools."""

from homebrew_harness.execution.model_protocol import (
    ModelProtocolError,
    ModelResponse,
    ToolInvocation,
)
from .common import usage


def prepare(request, rows, options, capabilities):
    inputs, instructions = [], []
    for row in rows:
        role, text = row["role"], row.get("content") or ""
        if role in {"system", "developer"}:
            instructions.append("[" + role + "]\n" + text)
        elif role == "tool":
            inputs.append(
                {
                    "type": "function_call_output",
                    "call_id": row["tool_call_id"],
                    "output": text,
                }
            )
        elif role == "assistant" and row.get("_replay"):
            inputs.extend(row["_replay"]["items"])
        else:
            if text:
                inputs.append({"role": role, "content": text})
            for call in row.get("tool_calls") or []:
                fun = call["function"]
                inputs.append(
                    {
                        "type": "function_call",
                        "call_id": call["id"],
                        "name": fun["name"],
                        "arguments": fun["arguments"],
                    }
                )
    body = {
        "model": request.model,
        "instructions": "\n\n".join(instructions),
        "input": inputs,
        "max_output_tokens": request.max_output_tokens,
        "store": False,
        "include": ["reasoning.encrypted_content"],
    }
    if request.tools:
        body["tools"] = [
            {"type": "function", **tool["function"]} for tool in request.tools
        ]
    if request.output_contract:
        contract = request.output_contract
        body["text"] = {
            "format": {
                "type": "json_schema",
                "name": "harness_result",
                "strict": True,
                "schema": contract["schema"],
            }
            if contract.get("schema")
            else {"type": "json_object"}
        }
    if options.get("reasoning") is not None:
        body["reasoning"] = {"effort": options["reasoning"]}
    return body


def decode(value):
    parts, calls = [], []
    for item in value.get("output") or []:
        kind = item.get("type")
        if kind == "message":
            for block in item.get("content") or []:
                if block.get("type") == "output_text":
                    parts.append({"type": "text", "text": block["text"]})
                elif block.get("type") != "refusal":
                    raise ModelProtocolError("Unsupported Responses content block.")
        elif kind == "function_call":
            call = ToolInvocation(item["call_id"], item["name"], item["arguments"])
            calls.append(call)
            parts.append({"type": "tool_call", "call": call.history()})
        elif kind != "reasoning":
            raise ModelProtocolError("Server-owned Responses tools are not enabled.")
    status = value.get("status")
    finish = (
        "incomplete"
        if status == "incomplete"
        else "failed"
        if status in {"failed", "cancelled"}
        else "continuation"
        if status != "completed"
        else "tool_calls"
        if calls
        else "refused"
        if any(
            b.get("type") == "refusal"
            for i in value.get("output") or []
            for b in i.get("content") or []
        )
        else "completed"
    )
    if len({c.id for c in calls}) != len(calls):
        raise ModelProtocolError("Responses function identities collide.")
    return ModelResponse(
        tuple(parts),
        finish,
        usage(value.get("usage"), protocol="openai_responses"),
        {"items": value.get("output") or []},
        value.get("id"),
    )


def streamed(events):
    for event in events:
        kind = event.get("type")
        if kind in {"response.completed", "response.incomplete", "response.failed"}:
            return decode(event["response"])
        if kind == "error":
            raise ModelProtocolError("Responses stream returned an error.")
    raise ModelProtocolError("Responses stream ended without a completion event.")
