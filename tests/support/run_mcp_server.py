"""Persistent external effects and deliberately missing responses for Run tests."""

import argparse
import asyncio
import json
import os
import socket
from pathlib import Path

import uvicorn
from mcp.server.fastmcp import FastMCP


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    args = parser.parse_args()
    root = Path(os.environ["MCP_FIXTURE_STATE"])
    root.mkdir(parents=True, exist_ok=True)
    server = FastMCP("run-fixture", log_level="WARNING")

    def record(kind, value):
        with (root / "audit.jsonl").open("a") as handle:
            handle.write(json.dumps({"kind": kind, "value": value}) + "\n")

    @server.tool()
    async def read(seconds: float = 0) -> str:
        record("read", seconds)
        await asyncio.sleep(seconds)
        return "external reference"

    @server.tool()
    async def append(text: str, seconds: float = 0) -> str:
        record("write", text)
        await asyncio.sleep(seconds)
        return "stored"

    # Changing this file represents an external schema deployment, not config drift.
    append.__doc__ = (
        (root / "schema.txt").read_text()
        if (root / "schema.txt").exists()
        else "Append external state."
    )
    server._tool_manager.get_tool("append").description = append.__doc__

    if args.transport == "stdio":
        server.run(transport="stdio")
    else:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        from urllib.parse import urlunsplit

        print(
            json.dumps(
                {
                    "url": urlunsplit(
                        (
                            "http",
                            "127.0.0.1:" + str(listener.getsockname()[1]),
                            "/mcp",
                            "",
                            "",
                        )
                    )
                }
            ),
            flush=True,
        )
        uvicorn.Server(
            uvicorn.Config(server.streamable_http_app(), log_level="warning")
        ).run(sockets=[listener])


if __name__ == "__main__":
    main()
