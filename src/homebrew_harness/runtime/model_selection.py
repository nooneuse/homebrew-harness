"""Interactive choices over the existing model registry, without provider discovery."""

import copy
import hashlib
import json

from homebrew_harness.execution.model_gateway import ModelGateway
from homebrew_harness.execution.model_registry import ModelBinding
from homebrew_harness.execution.providers import adapter_for
from homebrew_harness.execution.providers.common import frozen_options
from homebrew_harness.security.data_boundary import redact

EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "on", "off"}


def validate_choice(value):
    if not isinstance(value, dict) or set(value) - {"profile", "model", "reasoning"}:
        raise ValueError("Model choice accepts only profile, model and reasoning.")
    for name in ("profile", "model"):
        item = value.get(name)
        if item is not None and (not isinstance(item, str) or not item.strip() or len(item) > 200 or any(ord(c) < 32 for c in item)):
            raise ValueError("Model choices require short nonempty identifiers.")
    if value.get("profile") is not None and value.get("model") is not None:
        raise ValueError("Choose a registered profile or an inherited model name.")
    if value.get("reasoning") is not None and (not isinstance(value["reasoning"], str) or value["reasoning"] not in EFFORTS):
        raise ValueError("Unsupported reasoning setting.")
    if redact(value) != value:
        raise ValueError("Model choices cannot contain private connection values.")
    return {name: value.get(name) for name in ("profile", "model", "reasoning")}


def choose_binding(registry, client, model, choice, *, context_chars=50000, output_tokens=4096):
    choice = validate_choice(choice)
    base = registry.select(choice["profile"], client=client, model=choice["model"] or model,
        context_chars=context_chars, max_output_tokens=output_tokens)
    if choice["reasoning"] is None:
        return base
    adapter = base.client._adapter if isinstance(base.client, ModelGateway) else adapter_for(base.client)
    if not adapter.capabilities.thinking:
        raise ValueError("This profile declares no reasoning/thinking support.")
    value = choice["reasoning"]
    options = json.loads(adapter.options_json)
    options.pop("thinking", None)
    options.pop("reasoning", None)
    if value in {"on", "off"}:
        if adapter.protocol == "openai_responses":
            raise ValueError("Responses uses an effort level; select default, none or a supported effort.")
        if value == "on" and adapter.protocol == "anthropic_messages" and base.max_output_tokens <= 1024:
            raise ValueError("Anthropic thinking needs an output limit above its default thinking budget.")
        options["thinking"] = value == "on"
    else:
        if adapter.protocol not in {"chat_completions", "openai_responses"}:
            raise ValueError("This protocol uses thinking on/off; effort levels are not mapped.")
        options["reasoning"] = value
    variant = copy.copy(adapter)
    variant.options_json = frozen_options(options)
    fingerprint = hashlib.sha256(json.dumps({"binding": base.fingerprint, "options": variant.options_json}, sort_keys=True).encode()).hexdigest()
    return ModelBinding(variant, base.model, base.profile, fingerprint, base.context_chars,
        base.max_output_tokens, base.transport)


def model_status(registry, binding):
    adapter = binding.client._adapter if isinstance(binding.client, ModelGateway) else adapter_for(binding.client)
    options = json.loads(adapter.options_json)
    return redact({"profile": binding.profile, "model": binding.model, "protocol": adapter.protocol,
        "reasoning": {name: options[name] for name in ("reasoning", "thinking") if name in options},
        "models": registry.catalog(), "selection": "profile_alias" if registry.specs else "same_connection_model_name"})
