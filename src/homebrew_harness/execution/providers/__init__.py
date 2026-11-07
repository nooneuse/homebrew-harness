"""Native protocol adapters behind the shared gateway."""


def adapter_for(client):
    from .chat_completions import ChatCompletionsAdapter
    from .native import NativeAdapter

    if isinstance(client, (ChatCompletionsAdapter, NativeAdapter)):
        return client
    return ChatCompletionsAdapter(client)
