"""Shared bounded payload handling, logical roles and output contracts."""

import copy
import hashlib
import json
import math
from contextlib import contextmanager
from dataclasses import replace

from homebrew_harness.execution.model_protocol import (
    ModelProtocolError,
    ModelOutputError,
    ModelUsage,
    ProviderError,
    load_replay,
)

PROTOCOLS = {
    "chat_completions",
    "openai_responses",
    "dashscope_generation",
    "anthropic_messages",
}
RESERVED = {
    "model",
    "messages",
    "input",
    "tools",
    "stream",
    "timeout",
    "api_key",
    "base_url",
    "headers",
    "extra_headers",
    "extra_query",
    "extra_body",
    "http_client",
    "transport",
    "max_retries",
    "proxy",
    "proxies",
    "previous_response_id",
    "conversation",
    "store",
    "max_tokens",
    "max_output_tokens",
    "max_completion_tokens",
    "response_format",
    "output_config",
    "instructions",
    "system",
    "parameters",
    "text",
    "thinking",
    "enable_thinking",
    "reasoning",
    "temperature",
    "top_p",
    "stream_options",
}


def frozen_options(value):
    if not isinstance(value, dict) or set(value) - {
        "stream",
        "thinking",
        "reasoning",
        "temperature",
        "top_p",
        "incremental_output",
        "extensions",
    }:
        raise ValueError("Unsupported host model options.")
    if "stream" in value and type(value["stream"]) is not bool:
        raise ValueError("Model stream option must be boolean.")
    if "incremental_output" in value and type(value["incremental_output"]) is not bool:
        raise ValueError("Model incremental_output must be boolean.")
    for name, upper in (("temperature", 2), ("top_p", 1)):
        if name in value and (
            type(value[name]) not in {int, float}
            or not math.isfinite(value[name])
            or not 0 <= value[name] <= upper
        ):
            raise ValueError("Invalid model sampling option: " + name)
    if (
        "thinking" in value
        and type(value["thinking"]) is not bool
        and not isinstance(value["thinking"], dict)
    ):
        raise ValueError("Model thinking must be boolean or a protocol configuration.")
    if "reasoning" in value and (
        not isinstance(value["reasoning"], str)
        or value["reasoning"]
        not in {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
    ):
        raise ValueError("Unsupported reasoning effort.")
    extensions = value.get("extensions", {})
    if not isinstance(extensions, dict) or set(extensions) & RESERVED:
        raise ValueError("Model extensions cannot replace runtime request controls.")
    raw = json.dumps(value, sort_keys=True, allow_nan=False)
    if len(raw.encode()) > 8192:
        raise ValueError("Model options exceed 8 KiB.")
    return raw


def binding_key(client, model):
    return hashlib.sha256(
        json.dumps(
            {
                "protocol": client.protocol,
                "revision": client.revision,
                "endpoint": str(client.base_url),
                "model": model,
                "options": client.options_json,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()


def effective_options(adapter, request):
    options = json.loads(adapter.options_json)
    for key, value in request.options.items():
        options[
            {"extra_body": "extensions", "reasoning_effort": "reasoning"}.get(key, key)
        ] = value
    frozen_options(options)
    if options.get("thinking") and not adapter.capabilities.thinking:
        raise ModelProtocolError("Thinking is not supported by this model profile.")
    if (
        options.get("reasoning") not in {None, "none"}
        and not adapter.capabilities.thinking
    ):
        raise ModelProtocolError("Reasoning is not supported by this model profile.")
    if (
        adapter.protocol == "chat_completions"
        and "thinking" in options
        and type(options["thinking"]) is not bool
    ):
        raise ModelProtocolError("Chat enable_thinking must be boolean.")
    if (
        adapter.protocol in {"anthropic_messages", "dashscope_generation"}
        and "reasoning" in options
    ):
        raise ModelProtocolError("This protocol uses thinking, not reasoning_effort.")
    if adapter.protocol == "openai_responses" and "thinking" in options:
        raise ModelProtocolError("Responses uses reasoning_effort, not thinking.")
    if adapter.protocol != "dashscope_generation" and "incremental_output" in options:
        raise ModelProtocolError("incremental_output only applies to Generation.")
    return options


def _adapter_supports_images(adapter):
    return "image" in getattr(adapter, "supported_input_modalities", ("text",))


def require_image_input(adapter, *, model=None):
    """Reject explicit images before reading files or submitting a request."""
    label = f"Model '{model}'" if model else "This model profile"
    if not adapter.capabilities.supports_input("image"):
        raise ModelProtocolError(
            f"{label} does not declare image input support. Select an image-capable "
            "profile; explicit images are never silently dropped."
        )
    if not _adapter_supports_images(adapter):
        raise ModelProtocolError(
            f"Protocol adapter '{adapter.protocol}' cannot carry native image content. "
            "Use the documented image-capable adapter (chat_completions)."
        )


def media_protocol_error(error):
    from homebrew_harness.runtime.errors import annotate_error

    return annotate_error(
        ModelProtocolError(str(error)),
        code=getattr(error, "code", None),
        **(getattr(error, "details", None) or {}),
    )


def image_url_part(media, base64_text=""):
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{media['media_type']};base64,{base64_text}"},
    }


def encoded_json_size(value):
    # Conservative for the supported HTTP/SDK serializers: count ASCII escapes
    # and whitespace as well as all tools, options and message framing.
    return len(json.dumps(value, ensure_ascii=True, allow_nan=False))


def base64_size(media):
    return 4 * ((media["bytes"] + 2) // 3)


def _content_parts(content):
    from homebrew_harness.contracts import normalize_content_parts

    try:
        parts = normalize_content_parts(content)
    except ValueError as exc:
        raise ModelProtocolError(str(exc)) from exc
    if any(part["type"] == "image" and "media" not in part for part in parts):
        raise ModelProtocolError("Image content must be frozen before model submission.")
    return parts


def validate_image_size(media, limits):
    from homebrew_harness.media import MediaError

    size = media["bytes"]
    if size > limits.max_image_bytes:
        raise MediaError(
            f"Image is {size} bytes, above the per-image limit of {limits.max_image_bytes} bytes.",
            "media_bytes_limit",
            limit_name="image_bytes",
            limit_value=limits.max_image_bytes,
            observed_value=size,
            unit="bytes",
        )


def validate_user_media(parts, adapter, *, limits=None):
    """Validate the frozen batch being admitted, independently of history."""
    from homebrew_harness.media import (
        MediaError,
        MediaLimits,
        ensure_request_body_size,
        request_body_bounds,
    )

    normalized = _content_parts(parts)
    images = [part["media"] for part in normalized if part["type"] == "image"]
    if not images:
        return
    require_image_input(adapter)
    limits = limits or MediaLimits()
    try:
        for media in images:
            validate_image_size(media, limits)
        request_body_bounds(normalized, limits)
        # Admission has no history/tool/options payload. The complete encoded
        # request has its own check after provider projection.
        row = {"role": "user", "content": [
            image_url_part(part["media"]) if part["type"] == "image" else part
            for part in normalized
        ]}
        ensure_request_body_size(
            encoded_json_size({"messages": [row]}) + sum(map(base64_size, images)), limits
        )
    except MediaError as exc:
        raise media_protocol_error(exc) from exc


def may_have_media(rows):
    """Conservative cache/sizing gate only; does not authorize media."""
    for row in rows:
        content = row.get("content")
        if row.get("content_parts") is not None or isinstance(content, (list, tuple)):
            return True
        if row.get("role") == "tool" and (
            isinstance(content, dict) and "media" in content
            or isinstance(content, str) and '"media"' in content
        ):
            return True
    return False


def messages(request, adapter):
    result = []
    binding = binding_key(adapter, request.model)
    carry_tool_media = _adapter_supports_images(adapter) and adapter.capabilities.supports_input("image")
    if carry_tool_media:
        from homebrew_harness.contracts import ToolMediaCalls

        calls = ToolMediaCalls()
    pending_tool_media = {}

    def flush_tool_media():
        if not pending_tool_media:
            return
        # Preserve the canonical tool result and its call identity. A native
        # visual follow-up comes only after the entire contiguous tool batch.
        result.append({
            "role": "user",
            "name": "tool_media",
            "_harness_tool_media": True,
            "content": [
                {"type": "text", "text": "Image output from the tool results above."},
                *({"type": "image", "media": media} for media in pending_tool_media.values()),
            ],
        })
        pending_tool_media.clear()

    for message in request.messages:
        row = copy.deepcopy(
            {
                k: v
                for k, v in message.items()
                if k
                in {
                    "role",
                    "content",
                    "name",
                    "tool_calls",
                    "tool_call_id",
                    "_protocol_state",
                }
            }
        )
        if carry_tool_media and row.get("role") == "user" and message.get("_media_turn_start") is True:
            row["_media_turn_start"] = True  # Consumed by the Chat projector, not sent.
        content = row.get("content")
        parts = message.get("content_parts")
        if parts is not None or isinstance(content, (list, tuple)):
            normalized = _content_parts(parts if parts is not None else content)
            if parts is not None and isinstance(content, (list, tuple)) and normalized != _content_parts(content):
                raise ModelProtocolError("Conflicting ordered content representations.")
            if any(part["type"] == "image" for part in normalized):
                if row.get("role") != "user":
                    raise ModelProtocolError("Explicit image attachments require a user message.")
                require_image_input(adapter, model=request.model)
                row["content"] = normalized
            elif parts is None or not isinstance(content, str):
                row["content"] = "\n".join(part["text"] for part in normalized)
        if row.get("role") == "tool" and isinstance(content, dict):
            row["content"] = json.dumps(content, ensure_ascii=False)
        state = row.pop("_protocol_state", None)
        if state:
            if request.state_root is None:
                raise ModelProtocolError(
                    "Provider replay requires its owning state root."
                )
            row["_replay"] = load_replay(
                request.state_root, request.context_key, binding, state
            )
        if row.get("role") != "tool":
            flush_tool_media()
            if carry_tool_media:
                calls.observe(message)
        elif carry_tool_media:
            for media in calls.result_media(message):
                pending_tool_media.setdefault(media["media_ref"], media)
        # Names are local provenance, not permission-bearing native roles.
        result.append(row)
    flush_tool_media()
    return result


def number(value):
    return value if type(value) is int and value >= 0 else None


def usage(data, *, protocol):
    data = data or {}
    inp = number(data.get("input_tokens", data.get("prompt_tokens")))
    out = number(data.get("output_tokens", data.get("completion_tokens")))
    cached = number(
        data.get("cache_read_input_tokens", data.get("prompt_cache_hit_tokens"))
    )
    if cached is None:
        cached = number(
            (
                data.get("input_tokens_details")
                or data.get("prompt_tokens_details")
                or {}
            ).get("cached_tokens")
        )
    created = number(data.get("cache_creation_input_tokens"))
    if protocol == "anthropic_messages" and inp is not None:
        inp += (cached or 0) + (created or 0)
    total = number(data.get("total_tokens"))
    if inp is not None and out is not None:
        total = max(total or 0, inp + out)
    return ModelUsage(
        inp,
        out,
        total,
        number(
            (
                data.get("output_tokens_details")
                or data.get("completion_tokens_details")
                or {}
            ).get("reasoning_tokens")
        ),
        cached,
        number(data.get("prompt_cache_miss_tokens", created)),
    )


def classify_error(status, body):
    status = status if type(status) is int else 400
    if isinstance(body, str):
        body = {"message": body}
    text = json.dumps(body, default=str).lower()
    detail = body.get("error", body) if isinstance(body, dict) else {}
    code = (
        str(detail.get("code", detail.get("type", ""))).lower()
        if isinstance(detail, dict)
        else ""
    )
    kind = (
        "quota"
        if any(v in text for v in ("insufficient_quota", "quota_exhaust", "arrearage"))
        else "context_overflow"
        if any(
            v in text
            for v in (
                "context_length",
                "context window",
                "prompt is too long",
                "prompt_too_long",
            )
        )
        else "authentication"
        if status in {401, 403} or code in {"authentication_error", "permission_error"}
        else "rate_limit"
        if status == 429 or code in {"rate_limit_error", "rate_limit_exceeded"}
        else "server_error"
        if status >= 500
        or code in {"overloaded_error", "api_error", "server_error", "internal_error"}
        else "invalid_request"
    )
    detail = detail if isinstance(detail, dict) else {}
    message = detail.get("message") or (body.get("message") if isinstance(body, dict) else None)
    if not message and isinstance(body, dict) and isinstance(body.get("error"), str):
        message = body["error"]
    return ProviderError(kind, status_code=status, code=detail.get("code") or (body.get("code") if isinstance(body, dict) else None),
        error_type=detail.get("type"), message=message)


class UsageCapture:
    """One request's observed usage, independent of output or storage success."""

    def __init__(self, protocol):
        self.protocol, self.raw, self.final = protocol, {}, False

    def observe(self, data, *, final=False):
        if isinstance(data, dict) and data:
            self.raw.update(data)
            self.final = final

    def value(self):
        return replace(usage(self.raw, protocol=self.protocol), final=self.final)


@contextmanager
def response_errors(capture):
    try:
        yield
    except BaseException as error:
        import httpx

        normalized = error
        if isinstance(error, httpx.TimeoutException):
            normalized = ProviderError("timeout")
        elif isinstance(error, httpx.TransportError):
            normalized = ProviderError("transport")
        elif isinstance(error, (KeyError, TypeError, ValueError)) and not isinstance(
            error, ModelProtocolError
        ):
            normalized = ModelOutputError("Provider returned a malformed response.")
        normalized.model_usage = capture.value()
        if normalized is error:
            raise
        raise normalized from error


def validated_result(response, contract):
    response.validate()
    if (
        contract is None
        or response.finish not in {"completed", "tool_calls"}
        or response.tool_calls
    ):
        return response
    try:
        value = json.loads(response.require_text())
        if contract.get("type") == "json_object" and not isinstance(value, dict):
            raise ValueError()
        if contract.get("schema") is not None:
            from jsonschema import Draft202012Validator

            Draft202012Validator(contract["schema"]).validate(value)
    except Exception:
        from dataclasses import replace

        return replace(response, finish="invalid_output")
    return response
