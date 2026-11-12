"""Shared protocol semantics over explicit HTTP or optional SDK executors."""

from homebrew_harness.execution.model_protocol import (
    ModelCapabilities,
    ModelProtocolError,
)
from . import anthropic, chat_completions, dashscope, responses
from .common import (
    messages,
    effective_options,
    UsageCapture,
    response_errors,
    validated_result,
    classify_error,
)
from .backends import open_backend

CODECS = {
    "chat_completions": chat_completions,
    "openai_responses": responses,
    "anthropic_messages": anthropic,
    "dashscope_generation": dashscope,
}


class ProtocolAdapter:
    revision = "native-v1"

    def __init__(
        self,
        protocol,
        *,
        api_key,
        base_url,
        api_version="2023-06-01",
        options_json="{}",
        capabilities=None,
        connection_id=None,
        transport="auto",
        expected_transport=None,
    ):
        if protocol not in CODECS:
            raise ValueError("Unsupported model protocol.")
        self.protocol, self.api_key, self.base_url = protocol, api_key, base_url
        self.api_version, self.options_json = api_version, options_json
        self.capabilities = capabilities or ModelCapabilities()
        self.connection_id = connection_id
        self.revision = "chat-v1" if protocol == "chat_completions" else "native-v1"
        self.backend = open_backend(
            protocol, api_key, base_url, api_version, transport, expected_transport
        )
        self.timeout = 60

    @property
    def client(self):
        return self.backend.client

    @property
    def transport_identity(self):
        return {
            k: self.backend.identity[k]
            for k in ("backend", "revision", "sdk", "sdk_version")
        }

    @property
    def transport_description(self):
        return dict(self.backend.identity)

    @property
    def default_headers(self):
        return getattr(
            self.client, "default_headers", getattr(self.client, "headers", {})
        )

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def close(self):
        self.backend.close()

    def with_options(self, **options):
        if options.get("max_retries", 0) != 0:
            raise ValueError("SDK retries are controlled by the ModelGateway.")
        return self

    def prepare(self, request):
        if request.tools and not self.capabilities.tools:
            raise ModelProtocolError("This profile does not support client tools.")
        if (
            request.output_contract
            and request.output_contract.get("schema")
            and not self.capabilities.json_schema
        ):
            raise ModelProtocolError(
                "This profile does not support schema-constrained output."
            )
        options = effective_options(self, request)
        body = CODECS[self.protocol].prepare(
            request, messages(request, self), options, self.capabilities
        )
        if (
            self.protocol != "dashscope_generation"
            and self.protocol != "chat_completions"
        ):
            for name in ("temperature", "top_p"):
                if name in options:
                    body[name] = options[name]
            body.update(options.get("extensions", {}))
            if options.get("stream"):
                body["stream"] = True
        return self.backend.prepare(body)

    def execute(self, request, payload, timeout, check):
        capture = UsageCapture(self.protocol)
        with response_errors(capture):
            streaming = effective_options(self, request).get("stream", False)
            with self.backend.response(payload, streaming, timeout, check) as raw:
                if not streaming:
                    if raw.get("code") or raw.get("error"):
                        raise classify_error(400, raw)
                    capture.observe(raw.get("usage"), final=True)
                    return validated_result(
                        CODECS[self.protocol].decode(raw), request.output_contract
                    )

                def events():
                    finished = False
                    for value in raw:
                        check()
                        if (
                            value.get("code")
                            or value.get("error")
                            or value.get("type") == "error"
                        ):
                            raise classify_error(400, value)
                        if self.protocol == "anthropic_messages":
                            kind = value.get("type")
                            if kind == "message_start":
                                capture.observe(value.get("message", {}).get("usage"))
                            elif kind == "message_delta":
                                capture.observe(
                                    value.get("usage"),
                                    final=bool(
                                        value.get("delta", {}).get("stop_reason")
                                    ),
                                )
                            elif kind == "message_stop":
                                capture.final = True
                        elif self.protocol == "openai_responses":
                            native = value.get("response") or {}
                            if native:
                                capture.observe(
                                    native.get("usage"),
                                    final=native.get("status")
                                    in {"completed", "incomplete", "failed"},
                                )
                        else:
                            choices = (
                                (value.get("output") or {}).get("choices")
                                or value.get("choices")
                                or []
                            )
                            finished = finished or bool(
                                choices
                                and choices[0].get("finish_reason")
                                not in {None, "null"}
                            )
                            capture.observe(value.get("usage"), final=finished)
                        if value.get("type") == "response.failed" and (value.get("response") or {}).get("error"):
                            raise classify_error(400, value["response"])
                        yield value

                response = (
                    dashscope.streamed(
                        events(),
                        incremental=effective_options(self, request).get(
                            "incremental_output", True
                        ),
                    )
                    if self.protocol == "dashscope_generation"
                    else CODECS[self.protocol].streamed(events())
                )
                return validated_result(response, request.output_contract)
