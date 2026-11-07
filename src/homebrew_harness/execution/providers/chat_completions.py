"""Chat Completions SDK projection and buffered streaming."""

import copy
import json

from homebrew_harness.execution.model_protocol import (
    ModelCapabilities,
    ModelProtocolError,
    ModelResponse,
    ToolInvocation,
)
from .common import (
    messages,
    usage,
    validated_result,
    effective_options,
    UsageCapture,
    response_errors,
)


def as_data(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {k: as_data(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [as_data(v) for v in value]
    if hasattr(value, "model_dump"):
        return as_data(value.model_dump(exclude_none=True))
    return as_data(vars(value))


def decode(value):
    data = as_data(value)
    choices = data.get("choices") or []
    if not choices:
        raise ModelProtocolError("The model returned no choices.")
    choice = choices[0]
    message = choice.get("message") or {}
    calls = [ToolInvocation.from_history(c) for c in message.get("tool_calls") or []]
    reason = choice.get("finish_reason")
    if reason not in {"stop", "tool_calls", "length", "content_filter"}:
        raise ModelProtocolError("Chat response lacks a supported completion proof.")
    finish = (
        "incomplete"
        if reason == "length"
        else "refused"
        if reason == "content_filter" or message.get("refusal")
        else "tool_calls"
        if calls
        else "completed"
        if reason in {"stop", "tool_calls"}
        else "failed"
    )
    replay = (
        {"reasoning_content": message["reasoning_content"]}
        if message.get("reasoning_content") is not None
        else None
    )
    return ModelResponse.create(
        message.get("content"),
        calls,
        finish=finish,
        usage=usage(data.get("usage"), protocol="chat_completions"),
        replay=replay,
        provider_id=data.get("id"),
    )


def usage_from_chat(value):
    if value is None:
        return usage({}, protocol="chat_completions")
    return usage(as_data(value).get("usage"), protocol="chat_completions")


class ChatCompletionsAdapter:
    protocol = "chat_completions"
    revision = "chat-v1"

    def __init__(
        self, client, *, options_json="{}", capabilities=None, connection_id=None
    ):
        self.client = client
        self.options_json = options_json
        self.capabilities = capabilities or ModelCapabilities()
        self.connection_id = connection_id

    @property
    def api_key(self):
        return getattr(self.client, "api_key", None)

    @property
    def base_url(self):
        return getattr(self.client, "base_url", None)

    @property
    def timeout(self):
        return getattr(self.client, "timeout", None)

    @property
    def default_headers(self):
        return getattr(self.client, "default_headers", None)

    def with_options(self, **options):
        raw = (
            self.client.with_options(**options)
            if hasattr(self.client, "with_options")
            else self.client
        )
        return type(self)(
            raw,
            options_json=self.options_json,
            capabilities=self.capabilities,
            connection_id=self.connection_id,
        )

    def close(self):
        self.client.close()

    def prepare(self, request):
        if request.tools and not self.capabilities.tools:
            raise ModelProtocolError(
                "This model does not support the required client tools."
            )
        options = effective_options(self, request)
        rows = messages(request, self)
        for row in rows:
            replay = row.pop("_replay", None)
            if replay:
                row.update(replay)
        payload = {
            "model": request.model,
            "messages": rows,
            "max_tokens": request.max_output_tokens,
        }
        if request.tools:
            payload["tools"] = copy.deepcopy(list(request.tools))
        contract = request.output_contract
        if contract:
            if contract.get("type") == "json_schema":
                if not self.capabilities.json_schema:
                    raise ModelProtocolError(
                        "This model does not support schema-constrained output."
                    )
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "harness_result",
                        "strict": True,
                        "schema": contract["schema"],
                    },
                }
            else:
                payload["response_format"] = {"type": "json_object"}
        if "temperature" in options:
            payload["temperature"] = options["temperature"]
        if "top_p" in options:
            payload["top_p"] = options["top_p"]
        if options.get("reasoning") is not None:
            payload["reasoning_effort"] = options["reasoning"]
        extensions = dict(options.get("extensions", {}))
        if options.get("thinking") is not None:
            extensions["enable_thinking"] = options["thinking"]
        if extensions:
            payload["extra_body"] = extensions
        if options.get("stream"):
            payload.update(stream=True, stream_options={"include_usage": True})
        return payload

    def execute(self, request, payload, timeout, check):
        capture = UsageCapture(self.protocol)
        with response_errors(capture):
            return self._execute(request, payload, timeout, check, capture)

    def _execute(self, request, payload, timeout, check, capture):
        payload = dict(payload, timeout=timeout)
        raw = self.client.chat.completions.create(**payload)
        if not payload.get("stream"):
            capture.observe(as_data(raw).get("usage"), final=True)
            return validated_result(decode(raw), request.output_contract)
        collected = {"choices": [{"message": {"content": "", "tool_calls": []}}]}
        calls, size, finished = {}, 0, False
        try:
            for chunk in raw:
                check()
                data = as_data(chunk)
                size += len(json.dumps(data, default=str))
                if size > 8_000_000:
                    raise ModelProtocolError(
                        "Model stream exceeded its bounded buffer."
                    )
                if data.get("usage") is not None:
                    collected["usage"] = data["usage"]
                    capture.observe(data["usage"], final=finished)
                for choice in data.get("choices") or []:
                    delta = choice.get("delta") or {}
                    message = collected["choices"][0]["message"]
                    for name in ("content", "reasoning_content"):
                        if delta.get(name):
                            message[name] = message.get(name, "") + delta[name]
                    for call in delta.get("tool_calls") or []:
                        index = call.get("index", 0)
                        target = calls.setdefault(
                            index, {"id": "", "function": {"name": "", "arguments": ""}}
                        )
                        if call.get("id"):
                            target["id"] = call["id"]
                        function = call.get("function") or {}
                        for name in ("name", "arguments"):
                            target["function"][name] += function.get(name) or ""
                    if choice.get("finish_reason"):
                        collected["choices"][0]["finish_reason"] = choice[
                            "finish_reason"
                        ]
                        finished = True
            if not finished:
                raise ModelProtocolError(
                    "Model stream ended without a completion proof."
                )
            collected["choices"][0]["message"]["tool_calls"] = [
                calls[k] for k in sorted(calls)
            ]
            return validated_result(decode(collected), request.output_contract)
        finally:
            raw.close()
