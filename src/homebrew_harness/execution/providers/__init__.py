"""Native protocol adapters behind the shared gateway."""


def adapter_for(client):
    from .chat_completions import ChatCompletionsAdapter
    from .adapter import ProtocolAdapter

    if isinstance(client, (ChatCompletionsAdapter, ProtocolAdapter)):
        return client
    return ChatCompletionsAdapter(client)
