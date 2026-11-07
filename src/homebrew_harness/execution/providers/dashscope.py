"""Native Generation wire format and delta/cumulative response aggregation."""

import copy
from homebrew_harness.execution.model_protocol import ModelProtocolError
from .chat_completions import decode as chat_decode


def prepare(request, rows, options, capabilities):
    for row in rows:
        row.pop("name", None)
        replay = row.pop("_replay", None)
        if replay:
            row.update(replay)
    parameters = {"result_format": "message", "max_tokens": request.max_output_tokens}
    for name in ("temperature", "top_p"):
        if name in options:
            if name == "temperature" and options[name] >= 2:
                raise ModelProtocolError("Generation temperature must be below 2.")
            parameters[name] = options[name]
    if request.tools:
        parameters["tools"] = list(request.tools)
    if request.output_contract:
        parameters["response_format"] = {"type": "json_object"}
    if options.get("thinking") is not None:
        if type(options["thinking"]) is not bool:
            raise ModelProtocolError("Generation thinking must be boolean.")
        parameters["enable_thinking"] = options["thinking"]
    if options.get("stream"):
        parameters["incremental_output"] = options.get("incremental_output", True)
    parameters.update(options.get("extensions", {}))
    return {
        "model": request.model,
        "input": {"messages": rows},
        "parameters": parameters,
    }


def decode(value):
    output = value.get("output") or {}
    return chat_decode(
        {
            "id": value.get("request_id"),
            "choices": output.get("choices") or value.get("choices") or [],
            "usage": value.get("usage") or {},
        }
    )


def streamed(events, *, incremental):
    final, calls, finished = None, {}, False
    for event in events:
        if not incremental:
            final = copy.deepcopy(event)
        elif final is None:
            final = copy.deepcopy(event)
            message = final.get("output", {}).get("choices", [{}])[0].get("message", {})
            for call in message.get("tool_calls") or []:
                calls[call.get("index", len(calls))] = copy.deepcopy(call)
        else:
            choices = (event.get("output") or {}).get("choices") or []
            if choices:
                target = final["output"]["choices"][0]
                message = target["message"]
                delta = choices[0].get("message") or {}
                for name in ("content", "reasoning_content"):
                    if delta.get(name):
                        message[name] = message.get(name, "") + delta[name]
                for call in delta.get("tool_calls") or []:
                    index = call.get("index", len(calls))
                    current = calls.setdefault(
                        index,
                        {
                            "id": call.get("id", ""),
                            "function": {"name": "", "arguments": ""},
                        },
                    )
                    if call.get("id"):
                        current["id"] = call["id"]
                    for name in ("name", "arguments"):
                        current["function"][name] += (call.get("function") or {}).get(
                            name
                        ) or ""
                if choices[0].get("finish_reason"):
                    target["finish_reason"] = choices[0]["finish_reason"]
            if event.get("usage"):
                final["usage"] = event["usage"]
        choices = (event.get("output") or {}).get("choices") or []
        finished |= bool(
            choices and choices[0].get("finish_reason") not in {None, "null"}
        )
    if final is None or not finished:
        raise ModelProtocolError("Generation stream lacks a final finish reason.")
    if incremental and calls:
        final["output"]["choices"][0]["message"]["tool_calls"] = [
            calls[i] for i in sorted(calls)
        ]
    return decode(final)
