"""Isolated network executors; protocol conversion and tool execution stay elsewhere."""

import copy
import importlib
import importlib.metadata
import importlib.util
import inspect
import json
import os
import re
import sys
from contextvars import ContextVar
from contextlib import contextmanager

import httpx

from homebrew_harness.execution.model_protocol import ModelProtocolError, ProviderError
from .common import classify_error

PATHS = {
    "chat_completions": "chat/completions",
    "openai_responses": "responses",
    "anthropic_messages": "messages",
    "dashscope_generation": "services/aigc/text-generation/generation",
}
SDKS = {
    "chat_completions": "openai",
    "openai_responses": "openai",
    "anthropic_messages": "anthropic",
    "dashscope_generation": "dashscope",
}

MAX_RESPONSE_BYTES = 8_000_000

_PINS = ContextVar("model_backend_pins", default={})
_CHECK = ContextVar("model_response_check", default=lambda: None)


def backend_pins():
    return copy.deepcopy(_PINS.get())


def bounded_status(response):
    try:
        if response.status_code >= 400:
            raise classify_error(
                response.status_code, json_body(response, _CHECK.get(), error_body=True)
            )
        if 300 <= response.status_code < 400:
            raise ProviderError("invalid_request", status_code=response.status_code)
    except BaseException as error:
        error._harness_response_boundary = True
        raise


@contextmanager
def backend_scope(*values):
    pins = dict(_PINS.get())

    def collect(value):
        if isinstance(value, dict):
            if isinstance(value.get("transport"), dict) and "fingerprint" in value:
                transport = value["transport"]
                key = (
                    transport.get("connection_id")
                    or value.get("profile")
                    or "@inherited"
                )
                if key in pins and any(
                    pins[key].get(k) != transport.get(k)
                    for k in ("backend", "revision", "sdk", "sdk_version")
                ):
                    raise ValueError(
                        "Saved model bindings disagree on one connection backend."
                    )
                pins[key] = copy.deepcopy(transport)
            for child in value.values():
                collect(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                collect(child)

    for value in values:
        collect(value)
    token = _PINS.set(pins)
    try:
        yield
    finally:
        _PINS.reset(token)


def network_error(error):
    # Optional SDKs can use a different httpx generation than the base runtime.
    for name in ("httpx", "httpx2"):
        module = sys.modules.get(name)
        if module is not None:
            if isinstance(error, module.TimeoutException):
                return ProviderError("timeout")
            if isinstance(error, module.TransportError):
                return ProviderError("transport")
    return None


@contextmanager
def injected_sdk_errors():
    try:
        yield
    except BaseException as error:
        normalized = network_error(error)
        if normalized is not None:
            raise normalized from error
        module = sys.modules.get(type(error).__module__.split(".")[0])
        if module is not None:
            if isinstance(error, getattr(module, "APITimeoutError", ())):
                raise ProviderError("timeout") from error
            if isinstance(error, getattr(module, "APIConnectionError", ())):
                raise ProviderError("transport") from error
            if isinstance(error, getattr(module, "APIStatusError", ())):
                raise classify_error(error.status_code, error.body or {}) from error
        raise


def sdk_parameters(method, body):
    params = inspect.signature(method).parameters
    if any(p.kind == p.VAR_KEYWORD for p in params.values()):
        return copy.deepcopy(body)
    direct = {k: copy.deepcopy(v) for k, v in body.items() if k in params}
    extra = {k: copy.deepcopy(v) for k, v in body.items() if k not in params}
    if extra:
        if "extra_body" not in params:
            raise ModelProtocolError("SDK does not accept this protocol's extensions.")
        direct["extra_body"] = extra
    return direct


def json_body(response, check, *, error_body=False):
    chunks = []
    size = 0
    for chunk in response.iter_bytes():
        check()
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise ModelProtocolError("Model response exceeds its bounded size.")
        chunks.append(chunk)
    try:
        value = json.loads(b"".join(chunks))
    except ValueError as error:
        if error_body:
            return {}
        raise ModelProtocolError("Provider returned invalid JSON.") from error
    if not isinstance(value, dict):
        if error_body:
            return {}
        raise ModelProtocolError("Provider response must be an object.")
    return value


def sse_events(response, check):
    def lines():
        pending = bytearray()
        size = 0
        for chunk in response.iter_bytes():
            check()
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise ModelProtocolError("Model stream exceeds its bounded size.")
            pending.extend(chunk)
            while b"\n" in pending:
                line, _, rest = pending.partition(b"\n")
                pending[:] = rest
                try:
                    yield line.rstrip(b"\r").decode("utf-8")
                except UnicodeError as error:
                    raise ModelProtocolError(
                        "Provider stream returned invalid text."
                    ) from error
        if pending:
            try:
                yield pending.rstrip(b"\r").decode("utf-8")
            except UnicodeError as error:
                raise ModelProtocolError(
                    "Provider stream returned invalid text."
                ) from error

    data = []
    count = 0
    for line in lines():
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif not line and data:
            raw = "\n".join(data)
            data.clear()
            if raw == "[DONE]":
                return
            count += 1
            if count > 10000:
                raise ModelProtocolError("Model stream exceeds its event limit.")
            try:
                value = json.loads(raw)
            except ValueError as error:
                raise ModelProtocolError(
                    "Provider stream returned invalid JSON."
                ) from error
            if not isinstance(value, dict):
                raise ModelProtocolError("Provider stream event must be an object.")
            yield value
    if data:
        raise ModelProtocolError("Model stream ended inside an event.")


class HTTPBackend:
    def __init__(self, protocol, api_key, base_url, api_version):
        self.protocol = protocol
        headers = (
            {"x-api-key": api_key, "anthropic-version": api_version}
            if protocol == "anthropic_messages"
            else {"Authorization": "Bearer " + api_key}
        )
        self.client = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            headers=headers,
            timeout=60,
            trust_env=False,
            follow_redirects=False,
        )
        self.identity = {
            "backend": "http",
            "revision": "http-v1",
            "sdk": None,
            "sdk_version": None,
        }

    def prepare(self, body):
        return body

    @contextmanager
    def response(self, body, streaming, timeout, check):
        headers = (
            {"X-DashScope-SSE": "enable"}
            if streaming and self.protocol == "dashscope_generation"
            else {}
        )
        with self.client.stream(
            "POST", PATHS[self.protocol], json=body, headers=headers, timeout=timeout
        ) as response:
            if response.status_code >= 400:
                raise classify_error(
                    response.status_code, json_body(response, check, error_body=True)
                )
            if 300 <= response.status_code < 400:
                raise ProviderError("invalid_request", status_code=response.status_code)
            yield (
                sse_events(response, check) if streaming else json_body(response, check)
            )

    def close(self):
        self.client.close()


class SDKBackend:
    def __init__(self, protocol, api_key, base_url, api_version, module, version):
        self.protocol, self.module = protocol, module
        network = module.DefaultHttpxClient(
            timeout=60,
            trust_env=False,
            follow_redirects=False,
            event_hooks={"response": [bounded_status]},
        )
        options = {
            "api_key": api_key,
            "base_url": base_url,
            "max_retries": 0,
            "timeout": 60,
            "http_client": network,
        }
        if protocol == "anthropic_messages":
            options["default_headers"] = {"anthropic-version": api_version}
        else:
            options.update(organization="", project="", admin_api_key="")
        try:
            self.client = (
                module.Anthropic if protocol == "anthropic_messages" else module.OpenAI
            )(**options)
        except BaseException:
            network.close()
            raise
        try:
            resource = (
                self.client.chat.completions
                if protocol == "chat_completions"
                else self.client.responses
                if protocol == "openai_responses"
                else self.client.messages
            )
            self.method = resource.with_streaming_response.create
        except BaseException:
            self.client.close()
            raise
        self.identity = {
            "backend": "sdk",
            "revision": "sdk-raw-v1",
            "sdk": SDKS[protocol],
            "sdk_version": version,
        }

    def prepare(self, body):
        sdk_parameters(self.method, body)
        return body

    @contextmanager
    def response(self, body, streaming, timeout, check):
        token = _CHECK.set(check)
        try:
            with self.method(
                **sdk_parameters(self.method, body), timeout=timeout
            ) as response:
                check()
                if response.status_code >= 400:
                    raise classify_error(
                        response.status_code, json_body(response, check)
                    )
                yield (
                    sse_events(response, check)
                    if streaming
                    else json_body(response, check)
                )
        except BaseException as error:
            cause = error.__cause__
            if cause is not None and getattr(
                cause, "_harness_response_boundary", False
            ):
                raise cause from None
            normalized = network_error(error)
            if normalized is not None:
                raise normalized from error
            if isinstance(error, self.module.APITimeoutError):
                raise ProviderError("timeout") from error
            if isinstance(error, self.module.APIConnectionError):
                raise ProviderError("transport") from error
            if isinstance(error, self.module.APIStatusError):
                raise classify_error(error.status_code, error.body or {}) from error
            raise
        finally:
            _CHECK.reset(token)

    def close(self):
        self.client.close()


class GenerationSDKBackend:
    def __init__(self, api_key, base_url, module, version):
        from dashscope.transport import SyncTransport

        self.network = httpx.Client(
            timeout=60,
            trust_env=False,
            follow_redirects=False,
            event_hooks={"response": [bounded_status]},
        )
        try:
            self.client = SyncTransport(
                api_key=api_key,
                base_url=base_url,
                max_retries=0,
                http_client=self.network,
                timeout=60,
            )
        except BaseException:
            self.network.close()
            raise
        self.identity = {
            "backend": "sdk",
            "revision": "dashscope-transport-v1",
            "sdk": "dashscope",
            "sdk_version": version,
        }

    def prepare(self, body):
        return body

    @contextmanager
    def response(self, body, streaming, timeout, check):
        from dashscope import errors

        response = None
        token = _CHECK.set(check)
        try:
            response = self.client.open_stream(
                "POST",
                PATHS["dashscope_generation"],
                json=body,
                headers={"X-DashScope-SSE": "enable"} if streaming else {},
                timeout=timeout,
            )
            check()
            yield (
                sse_events(response, check) if streaming else json_body(response, check)
            )
        except BaseException as error:
            if isinstance(error, errors.APITimeoutError):
                raise ProviderError("timeout") from error
            if isinstance(error, errors.APIConnectionError):
                raise ProviderError("transport") from error
            if isinstance(error, errors.DashScopeError):
                raise classify_error(getattr(error, "status_code", 400), {}) from error
            raise
        finally:
            _CHECK.reset(token)
            if response is not None:
                response.close()

    def close(self):
        self.client.close()
        self.network.close()


def sdk_available(protocol):
    name = SDKS[protocol]
    if importlib.util.find_spec(name) is None:
        return None, "sdk_not_installed"
    try:
        version = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None, "sdk_version_unavailable"
    matched = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    minimum = {"openai": (2, 54, 0), "anthropic": (2, 7, 0), "dashscope": (2, 0, 4)}[
        name
    ]
    release = tuple(map(int, matched.groups())) if matched else ()
    if not release or release[0] != 2 or release < minimum:
        return None, "sdk_version_unsupported"
    if os.environ.get(
        {
            "openai": "OPENAI_CUSTOM_HEADERS",
            "anthropic": "ANTHROPIC_CUSTOM_HEADERS",
            "dashscope": "DASHSCOPE_CUSTOM_HEADERS",
        }[name]
    ):
        return None, "sdk_environment_headers_not_isolated"
    module = importlib.import_module(name)
    if name == "dashscope":
        from dashscope.transport import SyncTransport

        if not hasattr(SyncTransport, "open_stream"):
            return None, "sdk_streaming_unsupported"
    elif not hasattr(module, "DefaultHttpxClient"):
        return None, "sdk_client_isolation_unsupported"
    return (module, version), "sdk_available"


def open_backend(
    protocol, api_key, base_url, api_version, policy="auto", expected=None
):
    if policy not in {"auto", "http", "sdk"}:
        raise ValueError("Model transport must be auto, http or sdk.")
    selected = expected.get("backend") if expected else None
    if selected == "custom":
        raise ValueError("A saved custom backend requires its injected owner.")
    effective = selected or policy
    available, reason = (
        (None, "explicit_http") if effective == "http" else sdk_available(protocol)
    )
    if effective == "sdk" and available is None:
        raise ValueError(
            "Requested SDK backend unavailable: "
            + reason
            + "; install the matching optional dependency or use HTTP for a new binding."
        )
    backend = (
        (
            GenerationSDKBackend(api_key, base_url, *available)
            if protocol == "dashscope_generation"
            else SDKBackend(protocol, api_key, base_url, api_version, *available)
        )
        if available
        else HTTPBackend(protocol, api_key, base_url, api_version)
    )
    backend.identity = {
        **backend.identity,
        "declared_transport": policy,
        "selection_reason": reason,
    }
    if expected and any(
        backend.identity.get(k) != expected.get(k)
        for k in ("backend", "revision", "sdk", "sdk_version")
    ):
        backend.close()
        raise ValueError("Restore requires the saved model backend and SDK version.")
    return backend


def session_backend_bindings(workdir, requested):
    if not requested:
        return None
    from homebrew_harness.storage.durable_runtime import DurableStore

    store = DurableStore(workdir)
    sid = store.get_meta("latest_session") if requested == "latest" else requested
    return [
        store.get("checkpoints", "lead:" + str(sid)),
        store.items("checkpoints", prefix="member:" + str(sid) + ":"),
    ]
