"""A real stdio service with explicit destructive/failure probes for Docker tests."""

import asyncio
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

server = FastMCP("sandbox-probe", log_level="WARNING")


@server.tool(structured_output=True)
def identity() -> dict[str, Any]:
    return {"pid": os.getpid()}


@server.tool(structured_output=True)
def read_text(path: str) -> dict[str, Any]:
    return {"text": (Path.cwd() / path).read_text()}


@server.tool(structured_output=True)
def probe(paths: list[str], port: int) -> dict[str, Any]:
    status = dict(
        line.split(":", 1)
        for line in Path("/proc/self/status").read_text().splitlines()
        if ":" in line
    )
    blocked = []
    for path in paths:
        try:
            Path(path).read_bytes()
            blocked.append(False)
        except OSError:
            blocked.append(True)
    s = socket.socket()
    s.settimeout(0.2)
    try:
        s.connect(("127.0.0.1", port))
        network = False
    except OSError:
        network = True
    finally:
        s.close()
    return {
        "blocked": blocked,
        "network_blocked": network,
        "nonroot": os.getuid() != 0,
        "no_caps": int(status["CapEff"], 16) == 0,
        "no_new_privileges": status["NoNewPrivs"].strip() == "1",
        "host_environment_absent": "HARNESS_ENV_CANARY" not in os.environ,
    }


@server.tool(structured_output=True)
def write_workspace(path: str) -> dict[str, Any]:
    (Path.cwd() / path).write_text("modified")
    return {"written": True}


@server.tool(structured_output=True)
async def delay(seconds: float) -> dict[str, Any]:
    Path("/tmp/effect").write_text("accepted")
    await asyncio.sleep(seconds)
    return {"finished": True}


@server.tool()
def huge_output() -> str:
    return "x" * (5 * 1024 * 1024)


@server.tool(structured_output=True)
def spawn_detached() -> dict[str, Any]:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(90)"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {"child_pid": process.pid}


@server.tool(structured_output=True)
def run_script() -> dict[str, Any]:
    path = Path("/workspace/run.sh")
    process = subprocess.run([str(path)], capture_output=True, text=True, timeout=2)
    return {
        "exit_code": process.returncode,
        "stdout": process.stdout,
        "mode": path.stat().st_mode & 0o7777,
    }


if __name__ == "__main__":
    server.run(transport="stdio")
