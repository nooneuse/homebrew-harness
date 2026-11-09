"""Deterministic model responses and request capture for tests."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock
from openai.types.chat import ChatCompletion


def reply(content=None, calls=None, finish_reason="stop", reasoning=None):
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return ChatCompletion.model_validate(
        {
            "id": "test-completion",
            "object": "chat.completion",
            "created": 0,
            "model": "test-model",
            "choices": [
                {"index": 0, "finish_reason": finish_reason, "message": message}
            ],
        }
    )


def call(call_id, command=None, *, name="bash", arguments=None):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": arguments
            if arguments is not None
            else json.dumps({"command": command}),
        },
    }


def fake_client(responses, *, identity="test-client"):
    snapshots = []
    responses = iter(responses)

    def create(**kwargs):
        snapshots.append(copy.deepcopy(kwargs["messages"]))
        return next(responses)

    method = Mock(side_effect=create)
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=method))
    )
    if identity is not None:
        # All default fixtures represent one stable endpoint across restarts.
        client._harness_model_identity = identity
    return client, snapshots


def conversation(messages):
    """Protocol assertions exclude separately tested host intent attachments."""
    return [
        message
        for message in messages
        if message.get("name") not in {"task_intent", "inherited_task_constraints"}
    ]


def model_binding(client, model):
    from homebrew_harness.execution.model_registry import ModelRegistry

    return ModelRegistry().select(client=client, model=model)
