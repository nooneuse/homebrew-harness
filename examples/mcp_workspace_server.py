"""A small stdio MCP service suitable for a prebuilt restricted image."""

import os
from pathlib import Path
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

server = FastMCP("workspace-docs", log_level="WARNING")


@server.tool(annotations=ToolAnnotations(readOnlyHint=True), structured_output=True)
def get_version() -> dict[str, Any]:
    """Return service identity; repeated calls share one connection."""
    return {"service": "workspace-docs", "pid": os.getpid()}


@server.tool(annotations=ToolAnnotations(readOnlyHint=True), structured_output=True)
def read_text(
    path: Annotated[str, Field(min_length=1, max_length=1024)],
) -> dict[str, Any]:
    """Preview a UTF-8 file relative to the service workspace, up to 8,000 bytes."""
    root = Path.cwd().resolve()
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Use a relative workspace file.")
    target = root / relative
    if target.is_symlink() or not target.resolve().is_relative_to(root):
        raise ValueError("File must stay in the service workspace.")
    with target.open("rb") as stream:
        data = stream.read(8001)
    return {
        "path": path,
        "text": data[:8000].decode("utf-8"),
        "truncated": len(data) > 8000,
    }


if __name__ == "__main__":
    server.run(transport="stdio")
