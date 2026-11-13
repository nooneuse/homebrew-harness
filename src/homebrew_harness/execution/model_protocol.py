"""Serializable model contract. SDK objects belong to provider adapters only."""

import copy
import hashlib
import json
import uuid
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass, field

from homebrew_harness.runtime.errors import AgentError


class ModelProtocolError(AgentError):
    pass


class ModelOutputError(ModelProtocolError):
    """The provider response violates the output contract; a new answer may repair it."""


class ProviderError(ModelProtocolError):
    def __init__(self, kind, *, status_code=None, code=None, error_type=None, message=None):
        from homebrew_harness.security.data_boundary import redact
        import re

        def safe(value, limit):
            if not isinstance(value, (str, int, float)):
                return None
            return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", redact(str(value)))[:limit]

        self.kind, self.status_code = kind, status_code
        self.code, self.error_type = safe(code, 256), safe(error_type, 256)
        self.message = safe(message, 4096)
        self.transient = kind in {"transport", "timeout", "rate_limit", "server_error"}
        label = kind + (f"; HTTP {status_code}" if status_code is not None else "")
        if self.code or self.error_type:
            label += "; " + (self.code or self.error_type)
        super().__init__("Model provider failed (" + label + ")." + (" " + self.message if self.message else ""))
        self.body = {"error": {
            "code": self.code or ("insufficient_quota" if kind == "quota" else "context_length_exceeded" if kind == "context_overflow" else kind),
            **({"type": self.error_type} if self.error_type else {}),
            **({"message": self.message} if self.message else {}),
        }}


@dataclass(frozen=True)
class ToolInvocation:
    id: str
    name: str
    arguments: str
    kind: str = "function"

    def history(self):
        return {
            "id": self.id,
            "type": self.kind,
            "function": {"name": self.name, "arguments": self.arguments},
        }

    @classmethod
    def from_history(cls, value):
        function = value.get("function", value)
        arguments = function.get("arguments", "{}")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False)
        identity, name = value.get("id"), function.get("name")
        if (
            not isinstance(identity, str)
            or not identity
            or not isinstance(name, str)
            or not name
        ):
            raise ModelOutputError("Provider returned an invalid tool identity.")
        return cls(identity, name, arguments, value.get("type", "function"))


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    reasoning_tokens: int | None = None
    cache_hit_tokens: int | None = None
    cache_miss_tokens: int | None = None
    final: bool = True

    def data(self):
        value = {
            k: getattr(self, k)
            for k in (
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "reasoning_tokens",
                "cache_hit_tokens",
                "cache_miss_tokens",
            )
        }
        if not self.final:
            value["total_tokens"] = None
        return value


@dataclass(frozen=True)
class ModelCapabilities:
    tools: bool = True
    json_schema: bool = True
    thinking: bool = True


@dataclass(frozen=True)
class ModelRequest:
    model: str
    messages: tuple
    tools: tuple = ()
    max_output_tokens: int = 4096
    output_contract: dict | None = None
    options: dict = field(default_factory=dict)
    context_key: str = ""
    state_root: object = None


@dataclass(frozen=True)
class ModelResponse:
    parts: tuple = ()
    finish: str = "completed"
    usage: ModelUsage = field(default_factory=ModelUsage)
    replay: dict | None = None
    provider_id: str | None = None

    def validate(self):
        if self.finish not in {
            "completed",
            "tool_calls",
            "incomplete",
            "refused",
            "continuation",
            "failed",
            "invalid_output",
        }:
            raise ModelOutputError("Unknown model completion state.")
        if any(p.get("type") not in {"text", "tool_call"} for p in self.parts):
            raise ModelOutputError("Unsupported model output part.")
        if any(
            not isinstance(p.get("text"), str)
            for p in self.parts
            if p["type"] == "text"
        ):
            raise ModelOutputError("Model text must be a string.")
        calls = self.tool_calls
        if (
            len(calls) > 128
            or len({c.id for c in calls}) != len(calls)
            or any(c.kind != "function" for c in calls)
        ):
            raise ModelOutputError("Invalid or duplicate model tool identities.")
        if (
            self.finish == "tool_calls"
            and not calls
            or self.finish == "completed"
            and calls
        ):
            raise ModelOutputError(
                "Model completion state disagrees with its tool output."
            )
        return self

    @property
    def text(self):
        return "".join(p["text"] for p in self.parts if p["type"] == "text")

    @property
    def tool_calls(self):
        return tuple(
            ToolInvocation.from_history(p["call"])
            for p in self.parts
            if p["type"] == "tool_call"
        )

    @classmethod
    def create(
        cls,
        text=None,
        calls=(),
        *,
        finish="completed",
        usage=None,
        replay=None,
        provider_id=None,
    ):
        parts = []
        if text is not None:
            if not isinstance(text, str):
                raise ModelProtocolError(
                    "Provider text has an unsupported representation."
                )
            parts.append({"type": "text", "text": text})
        parts.extend({"type": "tool_call", "call": c.history()} for c in calls)
        ids = [c.id for c in calls]
        if len(ids) != len(set(ids)) or len(ids) > 128:
            raise ModelProtocolError(
                "Provider tool identities collide or exceed the limit."
            )
        return cls(tuple(parts), finish, usage or ModelUsage(), replay, provider_id)

    def require_text(self, *, nonempty=False):
        self.validate()
        if self.finish != "completed" or self.tool_calls:
            raise ModelProtocolError(
                "Model did not return a complete tool-free result."
            )
        if nonempty and not self.text.strip():
            raise ModelProtocolError("Model returned empty text.")
        return self.text


def save_replay(root, owner, binding, payload):
    from pathlib import Path
    from homebrew_harness.security.safe_files import SafeFile
    from homebrew_harness.storage.artifact_inventory import register_artifact

    raw = json.dumps(
        {"owner": owner, "binding": binding, "payload": payload}, ensure_ascii=False
    ).encode()
    if len(raw) > 2_000_000:
        raise ModelProtocolError(
            "Provider replay state exceeds its private size limit."
        )
    reference = ".harness/protocol-state/tool-results/" + uuid.uuid4().hex + ".txt"
    with SafeFile(Path(root), reference, create_parents=True) as target:
        target.publish(raw, None, 0o600)
    register_artifact(root, reference, raw, "protocol_state")
    return {
        "reference": reference,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "owner": owner,
        "binding": binding,
    }


def load_replay(root, owner, binding, reference):
    from pathlib import Path
    from homebrew_harness.security.safe_files import SafeFile

    if reference.get("owner") != owner or reference.get("binding") != binding:
        raise ModelProtocolError(
            "Provider replay state belongs to a different context or model."
        )
    path = reference.get("reference", "")
    if not isinstance(path, str) or not path.startswith(
        ".harness/protocol-state/tool-results/"
    ):
        raise ModelProtocolError("Invalid provider replay reference.")
    key = (str(Path(root).resolve()), owner, binding, path, reference.get("sha256"))
    with SafeFile(Path(root), path) as target, target.open_read() as stream:
        info = os.fstat(stream.fileno())
        stamp = (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )
        with _REPLAY_LOCK:
            cached = _REPLAY_CACHE.get(key)
            if cached and cached[0] == stamp:
                _REPLAY_CACHE.move_to_end(key)
                return copy.deepcopy(cached[1])
        raw = stream.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ModelProtocolError("Provider replay state exceeds its size limit.")
        target.verify_binding()
        after = os.fstat(stream.fileno())
        if stamp != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ModelProtocolError("Provider replay changed while being read.")
    if raw is None or hashlib.sha256(raw).hexdigest() != reference.get("sha256"):
        raise ModelProtocolError("Provider replay state is missing or changed.")
    value = json.loads(raw)
    if value.get("owner") != owner or value.get("binding") != binding:
        raise ModelProtocolError("Provider replay ownership changed.")
    with _REPLAY_LOCK:
        _REPLAY_CACHE[key] = (stamp, value["payload"], len(raw))
        while (
            len(_REPLAY_CACHE) > 32
            or sum(v[2] for v in _REPLAY_CACHE.values()) > 8_000_000
        ):
            _REPLAY_CACHE.popitem(last=False)
    return copy.deepcopy(value["payload"])


_REPLAY_LOCK = threading.Lock()
_REPLAY_CACHE = OrderedDict()
