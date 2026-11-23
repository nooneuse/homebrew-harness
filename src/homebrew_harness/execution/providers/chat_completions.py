from homebrew_harness.runtime.limits import current_limits
"""Chat Completions SDK projection and buffered streaming."""

from homebrew_harness.runtime.errors import boundary_error

import base64
import copy
import json

from homebrew_harness.execution.model_protocol import (
    ModelCapabilities,
    ModelProtocolError,
    ModelResponse,
    ToolInvocation,
)
from .common import (
    base64_size,
    encoded_json_size,
    image_url_part,
    media_protocol_error,
    messages,
    usage,
    validate_image_size,
    validated_result,
    effective_options,
    UsageCapture,
    response_errors,
    classify_error,
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
        diagnostics={"provider_finish_reason": reason},
    )


def usage_from_chat(value):
    if value is None:
        return usage({}, protocol="chat_completions")
    return usage(as_data(value).get("usage"), protocol="chat_completions")


class ChatCompletionsAdapter:
    protocol = "chat_completions"
    revision = "chat-v1"
    supported_input_modalities = ("text", "image")

    def __init__(
        self, client, *, options_json="{}", capabilities=None, connection_id=None
    ):
        self.client = client
        self.options_json = options_json
        self.capabilities = capabilities or ModelCapabilities()
        self.connection_id = connection_id

    @property
    def transport_identity(self):
        return {
            "backend": "custom",
            "revision": "injected-client-v1",
            "sdk": None,
            "sdk_version": None,
        }

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
        return self._prepare(request)

    def prepare_logical(self, request):
        return self._prepare(request, logical=True)

    def _prepare(self, request, *, logical=False):
        return prepare(
            request,
            messages(request, self),
            effective_options(self, request),
            self.capabilities,
            logical=logical,
        )

    def execute(self, request, payload, timeout, check):
        capture = UsageCapture(self.protocol)
        from .backends import injected_sdk_errors

        with response_errors(capture), injected_sdk_errors():
            return self._execute(request, payload, timeout, check, capture)

    def _execute(self, request, payload, timeout, check, capture):
        payload = dict(payload, timeout=timeout)
        from .backends import sdk_parameters

        raw = self.client.chat.completions.create(
            **sdk_parameters(self.client.chat.completions.create, payload)
        )
        if not payload.get("stream"):
            data = as_data(raw)
            if data.get("error"):
                raise classify_error(400, data)
            capture.observe(data.get("usage"), final=True)
            return validated_result(decode(raw), request.output_contract)
        try:

            def events():
                finished = False
                for chunk in raw:
                    check()
                    data = as_data(chunk)
                    if data.get("error"):
                        raise classify_error(400, data)
                    finished = finished or any(
                        c.get("finish_reason") for c in data.get("choices") or []
                    )
                    if data.get("usage") is not None:
                        capture.observe(
                            data["usage"],
                            final=bool(finished),
                        )
                    yield data

            return validated_result(streamed(events()), request.output_contract)
        finally:
            if hasattr(raw, "close"):
                raw.close()


def _project_media(request, payload, *, logical):
    """Select a bounded visual working set without changing canonical history."""
    groups, required = [], []
    for row in payload["messages"]:
        tool_media = row.pop("_harness_tool_media", False)
        if row.pop("_media_turn_start", False) or (row.get("role") == "assistant" and not row.get("tool_calls")):
            # Images from answered or abandoned turns are historical; their
            # loss must not block a fresh turn. Steering has no start marker.
            required = []
        parts = row.get("content")
        if not isinstance(parts, list):
            continue
        slots = []
        for part in parts:
            if part["type"] != "image":
                continue
            media = part.pop("media")
            part.update(
                type="text",
                text=f"[Image not sent in this request; frozen reference: {media['media_ref']}]",
            )
            # Replace only this JSON part. Escaping, framing and base64 are
            # accounted for without allocating/re-serializing a large payload
            # once per candidate. Text and every other request field still count.
            delta = (
                encoded_json_size(image_url_part(media)) + base64_size(media)
                - encoded_json_size(part)
            )
            slots.append((part, media, delta))
        if slots:
            groups.append(slots)
            if not tool_media:
                required = slots
    if not groups:
        return

    from homebrew_harness.media import (
        MediaError,
        MediaLimits,
        MediaStore,
        ensure_request_body_size,
        request_body_bounds,
        validate_image,
    )

    limits = MediaLimits()
    store = (
        MediaStore(request.state_root)
        if not logical and request.state_root is not None else None
    )
    wire_bytes = encoded_json_size(payload)

    def include(slot):
        part, media, _ = slot
        if logical:
            native = {"type": "image_url", "image_url": {"url": "media:inline"}}
        else:
            if store is None:
                raise MediaError(
                    "Image input requires its owning state root.", "media_reference_invalid"
                )
            try:
                data = store.load(media["media_ref"])
            except MediaError:
                raise
            except (OSError, ValueError) as exc:
                raise MediaError(
                    "Frozen media could not be safely read.", "media_unreadable"
                ) from exc
            actual = validate_image(data, limits)
            if len(data) != media["bytes"] or any(actual[key] != media[key] for key in actual):
                raise MediaError(
                    "Frozen media metadata does not match the stored content.",
                    "media_identity_conflict",
                )
            native = image_url_part(media, base64.b64encode(data).decode("ascii"))
        part.clear()
        part.update(native)

    try:
        # The newest explicit attachment batch is all-or-error. Neither older
        # user images nor recent tool output can displace any part of it.
        for _, media, _ in required:
            validate_image_size(media, limits)
        bounds = request_body_bounds(
            ({"type": "image", "media": media} for _, media, _ in required), limits
        )
        count, raw_bytes = bounds["images"], bounds["image_bytes"]
        wire_bytes += sum(delta for _, _, delta in required)
        if count and not logical:
            ensure_request_body_size(wire_bytes, limits)
        for slot in required:
            include(slot)
        for slots in reversed(groups):
            if slots is required:
                continue
            for slot in slots:
                _, media, delta = slot
                if (
                    count >= limits.max_images_per_request
                    or media["bytes"] > limits.max_image_bytes
                    or raw_bytes + media["bytes"] > limits.max_request_image_bytes
                    or wire_bytes + delta > limits.max_request_body_bytes
                ):
                    continue
                try:
                    include(slot)
                except MediaError:
                    # Unavailable history cannot wedge an unrelated request.
                    # Its stable reference remains visible and recoverable.
                    continue
                count += 1
                raw_bytes += media["bytes"]
                wire_bytes += delta
        if count and not logical:
            ensure_request_body_size(wire_bytes, limits)
    except MediaError as exc:
        raise media_protocol_error(exc) from exc


def prepare(request, rows, options, capabilities, *, logical=False):
    if request.tools and not capabilities.tools:
        raise ModelProtocolError(
            "This model does not support the required client tools."
        )
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
            if not capabilities.json_schema:
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
        payload.update(extensions)
    if options.get("stream"):
        payload.update(stream=True, stream_options={"include_usage": True})
    _project_media(request, payload, logical=logical)
    return payload


def streamed(events):
    collected = {"choices": [{"message": {"content": "", "tool_calls": []}}]}
    calls, size, finished = {}, 0, False
    for chunk in events:
        data = as_data(chunk)
        size += len(json.dumps(data, default=str))
        if size > current_limits().model_response_bytes:
            raise boundary_error(ModelProtocolError, "model_stream_buffer_limit", "Model stream exceeded its bounded buffer.", limit_name="stream_buffer_characters", limit_value=8_000_000, observed_value=size, unit="serialized_characters")
        if data.get("usage") is not None:
            collected["usage"] = data["usage"]
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
                collected["choices"][0]["finish_reason"] = choice["finish_reason"]
                finished = True
    if not finished:
        raise boundary_error(ModelProtocolError, "model_stream_incomplete", "Model stream ended without a completion proof; no finish reason was received.")
    collected["choices"][0]["message"]["tool_calls"] = [calls[k] for k in sorted(calls)]
    return decode(collected)
