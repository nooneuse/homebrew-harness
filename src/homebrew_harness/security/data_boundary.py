"""Best-effort secret removal at model, archive and result boundaries."""

import re
import threading

_VALUES: set[str] = set()
_LOCK = threading.Lock()
_ASSIGNMENT = re.compile(
    r"(?i)((?:api[_-]?key|access[_-]?token|password|secret)\s*[=:]\s*)(?:\"([^\"\n]{8,})\"|'([^'\n]{8,})'|([A-Za-z0-9+/_=.-]{16,}))"
)
_KEY = re.compile(r"\bsk-[A-Za-z0-9._-]{12,}\b")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----.*?-----END (?:[A-Z ]+ )?PRIVATE KEY-----",
    re.DOTALL,
)


def register_private_values(*values) -> None:
    with _LOCK:
        _VALUES.update(
            value for value in values if isinstance(value, str) and len(value) >= 8
        )


def private_revision():
    """Opaque cache identity; changing redaction policy invalidates cached messages."""
    with _LOCK:
        return hash(frozenset(_VALUES))


def redact(value):
    if isinstance(value, str):
        with _LOCK:
            private = sorted(_VALUES, key=len, reverse=True)
        for item in private:
            value = value.replace(item, "[redacted]")
        value = _PRIVATE_KEY.sub("[redacted private key]", value)
        value = _KEY.sub("[redacted]", value)
        return _ASSIGNMENT.sub(lambda match: match[1] + "[redacted]", value)
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [redact(item) for item in value]
    return value


def model_request(client, **kwargs):
    from homebrew_harness.execution.model_gateway import request

    return request(client, **kwargs)


def sensitive_path(parts) -> bool:
    for part in parts:
        name = part.lower()
        if name in {".env.example", ".env.sample", ".env.template"}:
            continue
        if (
            name == ".env"
            or name.startswith(".env.")
            or name in {".secrets", "secrets", "credentials", ".aws", ".ssh", ".gnupg"}
            or name.startswith((".secrets.", "secrets.", "credentials."))
            or name in {"id_rsa", "id_ed25519", "id_ecdsa"}
            or name.endswith((".key", ".p12", ".pfx"))
        ):
            return True
    return False
