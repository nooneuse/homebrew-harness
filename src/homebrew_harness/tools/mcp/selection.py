"""Host-selected MCP names and stable declarations, independent of live sockets."""

import hashlib
import json
import os
import shutil
from dataclasses import asdict
from pathlib import Path

from homebrew_harness.execution.test_runner import executable_identity
from homebrew_harness.security.data_boundary import register_private_values


def host_executable(spec, supplied):
    """Resolve the command using the service's actual PATH and cwd."""
    from mcp.client.stdio import get_default_environment
    from homebrew_harness.configuration import public_environment

    env = {**public_environment(get_default_environment()), **supplied}
    cwd = spec.cwd or Path.cwd()
    command = Path(spec.command)
    if command.is_absolute():
        executable = str(command)
    elif os.sep in spec.command:
        executable = os.path.abspath(cwd / command)
    else:
        path = os.pathsep.join(
            os.path.abspath(cwd / part) if not Path(part).is_absolute() else part
            for part in env.get("PATH", os.defpath).split(os.pathsep)
        )
        executable = shutil.which(spec.command, path=path)
    if not executable:
        raise ValueError("Selected MCP executable is unavailable.")
    return executable


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def alias(server, tool):
    from .runtime import normalize_mcp_name

    value = "mcp__" + normalize_mcp_name(server) + "__" + normalize_mcp_name(tool)
    if len(value) > 64:
        raise ValueError("Selected MCP alias exceeds the tool-name limit.")
    return value


def validate_selection(value):
    if not isinstance(value, dict) or len(value) > 16:
        raise ValueError("MCP selection must map up to 16 server names to tool lists.")
    result, names = {}, set()
    for server, tools in value.items():
        if (
            not isinstance(server, str)
            or not server
            or server == "*"
            or len(server) > 100
            or not isinstance(tools, (list, tuple))
            or not 1 <= len(tools) <= 64
            or any(not isinstance(t, str) or not t or len(t) > 100 for t in tools)
            or len(set(tools)) != len(tools)
            or "*" in tools
        ):
            raise ValueError(
                "MCP selections require explicit unique server/tool names."
            )
        for tool in tools:
            name = alias(server, tool)
            if name in names:
                raise ValueError("Selected MCP names collide after normalization.")
            names.add(name)
        result[server] = sorted(tools)
    if len(names) > 128:
        raise ValueError("MCP selection exceeds the effective tool-pool limit.")
    return dict(sorted(result.items()))


def selected_names(selection):
    return frozenset(
        alias(server, tool) for server, tools in selection.items() for tool in tools
    )


def declaration(specs, selection, environment=None):
    from .runtime import _resolve_values

    selected = validate_selection(selection)
    records = {}
    for name, tools in selected.items():
        if name not in specs:
            raise ValueError("Selected MCP server is not configured: " + name)
        spec = specs[name]
        if any(
            tool not in spec.tools or spec.tools[tool].action == "deny"
            for tool in tools
        ):
            raise ValueError(
                "MCP selection needs explicit permitted host tool policies."
            )
        env, headers = (
            _resolve_values(spec.env, environment),
            _resolve_values(spec.headers, environment),
        )
        register_private_values(
            spec.url,
            *(
                v
                for k, v in {**env, **headers}.items()
                if any(
                    word in k.casefold()
                    for word in ("token", "key", "secret", "authorization", "password")
                )
            ),
        )
        data = asdict(spec)
        data.update(
            env=env,
            headers=headers,
            tools={tool: asdict(spec.tools[tool]) for tool in tools},
        )
        if spec.transport == "stdio" and spec.execution.mode == "host":
            data["executable"] = executable_identity(host_executable(spec, env))
        records[name] = {"fingerprint": digest(data)}
    return {"selection": selected, "servers": records}


def prepare_runtime(runtime, *, workdir=None, task_id=None):
    for server in runtime.selection or {}:
        runtime.connect(server, workdir=workdir, task_id=task_id)
    return runtime.manifest(workdir=workdir, task_id=task_id)


def safe_read(record, run):
    mcp = record.get("mcp") or {}
    binding = mcp.get("binding") or {}
    return bool(
        binding.get("policy", {}).get("read_only") is True
        and (record.get("managed_run") or {}).get("run_id") == run["id"]
        and (run.get("mcp_manifest") or {}).get(record.get("name")) == binding
        and (run.get("bindings") or {}).get("mcp") == mcp.get("declaration")
    )
