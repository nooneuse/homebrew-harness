"""Host-selected model capability identity; raw connection details stay private."""

import hashlib
import json
import os
import re
import uuid


def profile_fingerprint(client, model, *, profile=None, settings=None):
    from homebrew_harness.execution.model_gateway import ModelGateway

    raw = client._client if isinstance(client, ModelGateway) else client
    selected = (
        profile
        if profile is not None
        else os.getenv("HARNESS_MODEL_PROFILE", "").strip()
    )
    if selected and not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", selected):
        raise ValueError(
            "Model profile must be a host-selected short identifier, without connection details."
        )
    endpoint = getattr(raw, "base_url", None)
    # An unidentified custom client is not safely interchangeable with another.
    if not selected and not endpoint:
        attributes = getattr(raw, "__dict__", {})
        selected = attributes.get("_harness_model_identity")
        if selected is None:
            selected = "local-" + uuid.uuid4().hex
            try:
                setattr(raw, "_harness_model_identity", selected)
            except (AttributeError, TypeError):
                pass
    payload = {
        "format": 2,
        "model": model,
        "profile": selected or "",
        "endpoint_digest": hashlib.sha256(str(endpoint or "").encode()).hexdigest(),
        "settings": settings or {},
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
