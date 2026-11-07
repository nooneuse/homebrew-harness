"""Isolated native HTTP connections with bounded SSE and no implicit retries."""

import json

from homebrew_harness.execution.model_protocol import (
    ModelCapabilities,
    ModelProtocolError,
)
from .common import (
    classify_error,
    messages,
    validated_result,
    effective_options,
    UsageCapture,
    response_errors,
)
from . import responses, anthropic, dashscope

MODULES = {
    "openai_responses": responses,
    "anthropic_messages": anthropic,
    "dashscope_generation": dashscope,
}
PATHS = {
    "openai_responses": "responses",
    "anthropic_messages": "messages",
    "dashscope_generation": "services/aigc/text-generation/generation",
}


class NativeAdapter:
    revision = "native-v1"

    def __init__(
        self,
        protocol,
        *,
        api_key,
        base_url,
        options_json="{}",
        capabilities=None,
        api_version="2023-06-01",
        connection_id=None,
    ):
        if protocol not in MODULES:
            raise ValueError("Unsupported native model protocol.")
        import httpx

        self.protocol, self.api_key, self.base_url = protocol, api_key, base_url
        self.options_json, self.capabilities = (
            options_json,
            capabilities or ModelCapabilities(),
        )
        self.connection_id = connection_id
        headers = (
            {"x-api-key": api_key, "anthropic-version": api_version}
            if protocol == "anthropic_messages"
            else {"Authorization": "Bearer " + api_key}
        )
        self.client = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            headers=headers,
            timeout=60,
            follow_redirects=False,
        )
        self.timeout = 60

    @property
    def default_headers(self):
        return self.client.headers

    def close(self):
        self.client.close()

    def with_options(self, **options):
        # The gateway owns deadlines; connections do not create retrying clones.
        return self

    def prepare(self, request):
        if request.tools and not self.capabilities.tools:
            raise ModelProtocolError("This model does not support the required tools.")
        if (
            request.output_contract
            and request.output_contract.get("schema")
            and not self.capabilities.json_schema
        ):
            raise ModelProtocolError(
                "This model does not support schema-constrained output."
            )
        options = effective_options(self, request)
        body = MODULES[self.protocol].prepare(
            request, messages(request, self), options, self.capabilities
        )
        if self.protocol != "dashscope_generation":
            for name in ("temperature", "top_p"):
                if name in options:
                    body[name] = options[name]
            body.update(options.get("extensions", {}))
            if options.get("stream"):
                body["stream"] = True
        return body

    def execute(self, request, payload, timeout, check):
        capture = UsageCapture(self.protocol)
        with response_errors(capture):
            return self._execute(request, payload, timeout, check, capture)

    def _execute(self, request, payload, timeout, check, capture):
        options = effective_options(self, request)
        streaming = options.get("stream", False)
        headers = (
            {"X-DashScope-SSE": "enable"}
            if self.protocol == "dashscope_generation" and streaming
            else {}
        )
        with self.client.stream(
            "POST", PATHS[self.protocol], json=payload, headers=headers, timeout=timeout
        ) as response:
            if not streaming:
                size, chunks = 0, []
                for chunk in response.iter_bytes():
                    check()
                    size += len(chunk)
                    if size > 8_000_000:
                        raise ModelProtocolError(
                            "Model response exceeds its bounded size."
                        )
                    chunks.append(chunk)
                try:
                    value = json.loads(b"".join(chunks))
                except ValueError as error:
                    raise ModelProtocolError(
                        "Provider returned invalid JSON."
                    ) from error
                if response.status_code >= 400 or value.get("code"):
                    raise classify_error(response.status_code, value)
                capture.observe(value.get("usage"), final=True)
                return validated_result(
                    MODULES[self.protocol].decode(value), request.output_contract
                )
            if response.status_code >= 400:
                raise classify_error(response.status_code, {})

            def events():
                lines, size, count = [], 0, 0
                for line in response.iter_lines():
                    check()
                    size += len(line.encode())
                    if size > 8_000_000:
                        raise ModelProtocolError(
                            "Model SSE buffer exceeds its size limit."
                        )
                    if line.startswith("data:"):
                        lines.append(line[5:].lstrip())
                    elif not line and lines:
                        raw = "\n".join(lines)
                        lines.clear()
                        if raw == "[DONE]":
                            break
                        count += 1
                        if count > 10000:
                            raise ModelProtocolError(
                                "Model SSE exceeds its event limit."
                            )
                        value = json.loads(raw)
                        if value.get("code") or value.get("type") == "error":
                            raise classify_error(400, value)
                        native_response = value.get("response") or {}
                        if native_response:
                            capture.observe(
                                native_response.get("usage"),
                                final=native_response.get("status")
                                in {"completed", "incomplete", "failed"},
                            )
                        elif self.protocol == "anthropic_messages":
                            if value.get("type") == "message_start":
                                capture.observe(value.get("message", {}).get("usage"))
                            elif value.get("type") == "message_delta":
                                capture.observe(
                                    value.get("usage"),
                                    final=bool(
                                        value.get("delta", {}).get("stop_reason")
                                    ),
                                )
                            elif value.get("type") == "message_stop":
                                capture.final = True
                        else:
                            choices = value.get("output", {}).get("choices") or []
                            capture.observe(
                                value.get("usage"),
                                final=bool(
                                    choices
                                    and choices[0].get("finish_reason")
                                    not in {None, "null"}
                                ),
                            )
                        yield value
                if lines:
                    raise ModelProtocolError("Model SSE ended inside an event.")

            value = (
                dashscope.streamed(
                    events(), incremental=options.get("incremental_output", True)
                )
                if self.protocol == "dashscope_generation"
                else MODULES[self.protocol].streamed(events())
            )
            return validated_result(value, request.output_contract)
