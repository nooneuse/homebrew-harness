"""Independent MCP demo service, usable over stdio or Streamable HTTP."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import tempfile
from pathlib import Path
from typing import Annotated, Any

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field


DOCS = [
    {"title": "Agent Loop", "text": "The model selects tools; the harness executes them and returns paired tool results."},
    {"title": "Hooks", "text": "PreToolUse controls execution. PostToolUse observes results. Stop can continue a turn."},
    {"title": "Task System", "text": "Persist task dependencies and ownership, claim ready work, then verify before completion."},
]


class Payload(BaseModel):
    name: str
    quantity: Annotated[int, Field(ge=1)]


def build_server(state_dir: Path, *, test_tools: bool = False) -> FastMCP:
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    server = FastMCP("demo-docs", log_level="WARNING", stateless_http=False, json_response=False)

    def audit(kind: str, **data):
        fd = os.open(state_dir / "calls.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"kind": kind, "pid": os.getpid(), **data}) + "\n")

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True), structured_output=True)
    def search(query: str, limit: Annotated[int, Field(ge=1, le=5)] = 3) -> dict[str, Any]:
        """Search the demo harness documentation by topic or keyword."""
        audit("tool", name="search")
        matches = [doc for doc in DOCS if query.lower() in (doc["title"] + " " + doc["text"]).lower()]
        return {"query": query, "results": matches[:limit]}

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True), structured_output=True)
    def get_version() -> dict[str, Any]:
        """Return service version and process identity for transport verification."""
        audit("tool", name="get_version")
        return {"service": "demo-docs", "version": "1.0", "pid": os.getpid(),
                "private_model_env_present": any(key in os.environ for key in (
                    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "DASHSCOPE_API_KEY"))}

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False), structured_output=True)
    def record_note(text: str) -> dict[str, Any]:
        """Append a note to this external service's private state directory."""
        audit("tool", name="record_note")
        fd = os.open(state_dir / "notes.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"text": text}) + "\n")
        return {"stored": True, "text": text}

    if test_tools:
        @server.tool(name="dotted.echo", annotations=ToolAnnotations(readOnlyHint=True), structured_output=True)
        def dotted_echo(value: str) -> dict[str, Any]:
            """Test routing from normalized model names back to raw MCP names."""
            audit("tool", name="dotted.echo")
            return {"value": value}

        @server.tool(annotations=ToolAnnotations(readOnlyHint=True), structured_output=True)
        def echo_scope(workdir: str, name: str, payload: Payload | None = None) -> dict[str, Any]:
            """Echo ordinary arguments named workdir/name and an optional nested payload."""
            audit("tool", name="echo_scope")
            return {"workdir": workdir, "name": name, "payload": payload.model_dump() if payload else None}

        @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
        def fail() -> dict:
            """Return a real MCP isError result for failure-path tests."""
            audit("tool", name="fail")
            raise ValueError("Demo tool failure")

        @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
        async def delay(seconds: float) -> dict:
            """Exercise request deadlines without automatic retries."""
            audit("tool", name="delay")
            await asyncio.sleep(seconds)
            return {"finished": True}

        @server.tool(annotations=ToolAnnotations(readOnlyHint=False))
        def crash() -> dict:
            """Test abrupt transport closure; only exposed with --test-tools."""
            audit("tool", name="crash")
            os._exit(7)

    server.audit = audit
    return server


class HTTPAudit:
    def __init__(self, app, audit):
        self.app, self.audit = app, audit

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            token = os.getenv("MCP_DEMO_TOKEN")
            if token and dict(scope["headers"]).get(b"authorization") != ("Bearer " + token).encode():
                await send({"type": "http.response.start", "status": 401, "headers": []})
                await send({"type": "http.response.body", "body": b"Unauthorized"})
                return
            if scope["method"] == "POST":
                parts, body = [], b""
                while True:
                    message = await receive()
                    parts.append(message)
                    body += message.get("body", b"")
                    if not message.get("more_body", False):
                        break
                try:
                    payload = json.loads(body)
                    self.audit("rpc", method=payload.get("method"),
                               has_session=bool(dict(scope["headers"]).get(b"mcp-session-id")))
                except (ValueError, AttributeError):
                    pass

                original_receive = receive

                async def replay():
                    return parts.pop(0) if parts else await original_receive()

                receive = replay
        await self.app(scope, receive, send)


def main():
    parser = argparse.ArgumentParser(description="Run a small independent MCP service.")
    parser.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    parser.add_argument("--port", type=int, default=0, help="HTTP port; zero reserves an available loopback port.")
    parser.add_argument("--test-tools", action="store_true")
    args = parser.parse_args()
    state = os.getenv("MCP_DEMO_STATE_DIR")
    state_dir = Path(state).expanduser().resolve() if state else Path(tempfile.mkdtemp(prefix="mcp-demo-"))
    server = build_server(state_dir, test_tools=args.test_tools)
    if args.transport == "stdio":
        server.run(transport="stdio")  # stdout is reserved for JSON-RPC frames.
    else:
        listener = socket.socket()
        listener.bind(("127.0.0.1", args.port))
        listener.listen(128)
        port = listener.getsockname()[1]
        app = HTTPAudit(server.streamable_http_app(), server.audit)
        print(json.dumps({"url": f"http://127.0.0.1:{port}/mcp", "pid": os.getpid()}), flush=True)
        uvicorn.Server(uvicorn.Config(app, log_level="warning")).run(sockets=[listener])


if __name__ == "__main__":
    main()
