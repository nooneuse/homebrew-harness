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


def messages(request, adapter):
    result = []
    binding = binding_key(adapter, request.model)
    from homebrew_harness.contracts import tool_result_media

    pending_tool_media = []

    def flush_tool_media():
        # Tool-produced media is re-inspected as native image content only on
        # the documented image-capable path (model declaration AND adapter wire
        # capability). Otherwise the tool result keeps its stable reference and
        # metadata as text; pixels are never faked via filenames or byte text.
        # Deriving this at request time (instead of mutating history) keeps the
        # canonical conversation, recovery replay and compaction unchanged.
        if not pending_tool_media:
            return
        wire = getattr(adapter, "supported_input_modalities", ("text",))
        if adapter.capabilities.supports_input("image") and "image" in wire:
            parts = [
                {
                    "type": "text",
                    "text": (
                        "Native image output produced by the registered tool "
                        "result(s) above. Inspect the image content directly; "
                        "the frozen media references remain retrievable for export."
                    ),
                }
            ]
            seen = set()
            for media in pending_tool_media:
                if media["media_ref"] in seen:
                    continue
                seen.add(media["media_ref"])
                parts.append({"type": "image", "media": dict(media)})
            result.append({"role": "user", "name": "tool_media", "content": parts})
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
        # Ordered frozen content parts are the authoritative multimodal input.
        # The plain `content` string stays a lossless text projection for the
        # text-only runtime; expand parts to native wire content here so images
        # travel as real media, never filenames or byte text.
        parts = message.get("content_parts")
        if parts is not None:
            from homebrew_harness.contracts import (
                content_is_multimodal,
                normalize_content_parts,
            )

            try:
                normalized_parts = normalize_content_parts(parts)
            except ValueError as exc:
                raise ModelProtocolError(str(exc)) from exc
            if content_is_multimodal(normalized_parts):
                row["content"] = [dict(part) for part in normalized_parts]
        content = row.get("content")
        if isinstance(content, list):
            from homebrew_harness.contracts import (
                content_media,
                normalize_content_parts,
            )

            try:
                row["content"] = [
                    dict(part) for part in normalize_content_parts(content)
                ]
            except ValueError as exc:
                raise ModelProtocolError(str(exc)) from exc
            if any(
                part.get("type") == "image" and "media" not in part
                for part in row["content"]
            ):
                raise ModelProtocolError(
                    "Image content must be frozen by the session before model submission."
                )
            if content_media(row["content"]):
                if not adapter.capabilities.supports_input("image"):
                    raise ModelProtocolError(
                        f"Model '{request.model}' via protocol '{adapter.protocol}' "
                        "does not declare image input support. Select an image-capable "
                        "model/profile or explicitly handle the images; history images "
                        "are never silently dropped or converted to text."
                    )
                wire = getattr(adapter, "supported_input_modalities", ("text",))
                if "image" not in wire:
                    raise ModelProtocolError(
                        f"Protocol adapter '{adapter.protocol}' cannot carry native "
                        "image content in this milestone even though model "
                        f"'{request.model}' declares image input. Use the documented "
                        "image-capable adapter (chat_completions) or explicitly "
                        "handle the images; they are never silently dropped."
                    )
        state = row.pop("_protocol_state", None)
        if state:
            if request.state_root is None:
                raise ModelProtocolError(
                    "Provider replay requires its owning state root."
                )
            row["_replay"] = load_replay(
                request.state_root, request.context_key, binding, state
            )
        if row.get("role") == "tool":
            pending_tool_media.extend(tool_result_media(row.get("content")))
        else:
            flush_tool_media()
        # Names are local provenance, not permission-bearing native roles.
        result.append(row)
    flush_tool_media()
    # Per-request image count and total media bytes are a separate limit class
    # from per-image bytes/dimensions (enforced at freeze) and from the encoded
    # request-body ceiling (enforced by the adapter).  Aggregate every frozen
    # image re-sent in this request, including history, so the limit reflects
    # what actually reaches the provider.
    from homebrew_harness.media import MediaError, MediaLimits, request_body_bounds
    from homebrew_harness.runtime.errors import annotate_error

    request_parts = []
    for row in result:
        content = row.get("content")
        if isinstance(content, list):
            request_parts.extend(
                part for part in content if part.get("type") == "image"
            )
    if request_parts:
        try:
            request_body_bounds(request_parts, MediaLimits())
        except MediaError as exc:
            raise annotate_error(
                ModelProtocolError(str(exc)),
                code=getattr(exc, "code", None),
                **(getattr(exc, "details", None) or {}),
            ) from exc
    return result


def logical_payload(value):
    """Copy a provider payload with inline base64 media replaced by a marker.

    Base64 image bytes are media, not ordinary text context characters. The
    logical size keeps context accounting honest without a token budget.
    """
    if isinstance(value, dict):
        return {key: logical_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [logical_payload(item) for item in value]
    if (
        isinstance(value, str)
        and value.startswith("data:image/")
        and ";base64," in value
    ):
        return "media:inline"
    return value


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
