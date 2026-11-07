"""Write a ready demo configuration outside Git, keeping paths machine-local."""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Create a private stdio demo MCP configuration.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path)
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    state = args.state_dir.expanduser().resolve() if args.state_dir else Path(tempfile.mkdtemp(prefix="harness-mcp-demo-"))
    payload = {"servers": {"docs": {
        "transport": "stdio", "execution": {"mode": "host"}, "command": sys.executable,
        "args": [str(Path(__file__).with_name("mcp_demo_server.py").resolve())],
        "cwd": str(state), "env": {"MCP_DEMO_STATE_DIR": str(state)},
        "tools": {"search": {"action": "allow", "read_only": True},
                  "get_version": {"action": "allow", "read_only": True},
                  "record_note": {"action": "confirm", "read_only": False}},
    }}}
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    print("Created private MCP config: " + str(output))


if __name__ == "__main__":
    main()
