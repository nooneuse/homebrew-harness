"""s14: real SDK transports, host policies and dynamic MCP tool discovery."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import threading
import time
import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from homebrew_harness.execution.execution_control import (
    acquired,
    check,
    operation_deadline,
    wait_future,
)
from homebrew_harness.execution.execution_leases import ExecutionLeases
from homebrew_harness.execution.mcp_policy import MCPExecutionPolicy, execution_policy
from homebrew_harness.execution.mcp_runner import MCPContainerOwner, MCPWorkspace
from homebrew_harness.execution.model_gateway import Deadline, ModelGatewayError
from homebrew_harness.execution.test_runner import execution_context
from homebrew_harness.runtime.runtime_control import (
    ClosePending,
    Shutdown,
    WorkAccepted,
    Workers,
)
from homebrew_harness.security.data_boundary import redact, register_private_values
from homebrew_harness.security.safe_files import canonical_root
from homebrew_harness.storage.durable_runtime import settle_activity, track_activity

PRIVATE_MODEL_ENV = {
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "DASHSCOPE_API_KEY",
}


class MCPError(ValueError):
    """Safe client errors: provider endpoints and credentials are never included."""

    reason_code = "mcp_preparation_failed"


class MCPBindingChanged(MCPError):
    reason_code = "mcp_binding_changed"


class MCPSelectionMissing(MCPError):
    reason_code = "mcp_selection_unavailable"


def normalize_mcp_name(name: str) -> str:
    if not isinstance(name, str) or not name:
        raise MCPError("MCP names must be nonempty strings.")
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)


@dataclass(frozen=True)
class ToolPolicy:
    action: str = "confirm"
    read_only: bool = False


@dataclass(frozen=True)
class ServerSpec:
    name: str
    transport: str
    command: str = ""
    args: tuple[str, ...] = ()
    cwd: Path | None = None
    url: str = ""
    env: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)
    tools: dict[str, ToolPolicy] = field(default_factory=dict)
    timeout_seconds: float = 20
    connect_timeout_seconds: float = 30
    execution: MCPExecutionPolicy = field(default_factory=MCPExecutionPolicy)
    revision: str = "1"

    def __post_init__(self):
        if not isinstance(self.execution, MCPExecutionPolicy):
            raise MCPError("MCP execution must be a host policy object.")
        if self.execution.mode == "container":
            if (
                self.transport != "stdio"
                or self.env
                or self.headers
                or self.url
                or self.cwd is not None
            ):
                raise MCPError(
                    "Container MCP accepts stdio without host cwd, environment, credentials or HTTP settings."
                )
            if (
                not isinstance(self.command, str)
                or not self.command.startswith("/")
                or "\0" in self.command
                or not isinstance(self.args, tuple)
                or any(not isinstance(a, str) or "\0" in a for a in self.args)
            ):
                raise MCPError(
                    "Container MCP needs a fixed absolute image command and string arguments."
                )


def _resolve_values(values: dict, environment=None) -> dict[str, str]:
    result = {}
    for key, value in values.items():
        if isinstance(value, dict):
            variable = value["env"]
            source = os.environ if environment is None else environment
            if variable not in source:
                raise MCPError(
                    "MCP connection needs an unset environment variable: " + variable
                )
            value = source[variable]
        result[key] = value
    return result


def load_specs(path: Path, *, missing_ok: bool = False) -> dict[str, ServerSpec]:
    if not path.exists() and missing_ok:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise MCPError(
            "Cannot read MCP configuration; check its JSON and file permissions."
        ) from error
    if (
        not isinstance(payload, dict)
        or set(payload) != {"servers"}
        or not isinstance(payload["servers"], dict)
    ):
        raise MCPError("MCP configuration requires a servers object.")
    specs, prefixes = {}, set()
    for name, data in payload["servers"].items():
        safe = normalize_mcp_name(name)
        if len(safe) > 32 or safe in prefixes:
            raise MCPError("MCP server prefixes collide or exceed 32 characters.")
        prefixes.add(safe)
        if not isinstance(data, dict) or set(data) - {
            "transport",
            "command",
            "args",
            "cwd",
            "url",
            "env",
            "headers",
            "tools",
            "timeout_seconds",
            "connect_timeout_seconds",
            "execution",
            "revision",
        }:
            raise MCPError("Unknown MCP server configuration fields.")
        try:
            execution = execution_policy(data.get("execution", {"mode": "host"}))
        except (ValueError, TypeError) as error:
            raise MCPError("Invalid host MCP execution policy: " + str(error)) from None
        transport = data.get("transport")
        if not isinstance(transport, str) or transport not in {
            "stdio",
            "streamable_http",
        }:
            raise MCPError("MCP transport must be stdio or streamable_http.")
        timeout = data.get("timeout_seconds", 20)
        if type(timeout) not in (int, float) or not 0 < timeout <= 120:
            raise MCPError(
                "MCP timeout_seconds must be greater than zero and at most 120."
            )
        connect_timeout = data.get("connect_timeout_seconds", 30)
        if type(connect_timeout) not in (int, float) or not 0 < connect_timeout <= 120:
            raise MCPError(
                "MCP connect_timeout_seconds must be greater than zero and at most 120."
            )
        env, headers = data.get("env", {}), data.get("headers", {})
        for values in (env, headers):
            if not isinstance(values, dict):
                raise MCPError("MCP env and headers must be objects.")
            for key, value in values.items():
                if not isinstance(key, str) or not key:
                    raise MCPError(
                        "MCP environment/header names must be nonempty strings."
                    )
                if not isinstance(value, str) and not (
                    isinstance(value, dict)
                    and set(value) == {"env"}
                    and isinstance(value["env"], str)
                    and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value["env"])
                ):
                    raise MCPError(
                        "MCP environment/header values require a string or an env reference."
                    )
        if any(key in PRIVATE_MODEL_ENV for key in env):
            raise MCPError(
                "MCP subprocesses cannot override private model environment variables."
            )
        policies = {}
        raw_policies = data.get("tools", {})
        if not isinstance(raw_policies, dict):
            raise MCPError(
                "MCP tools policy must be an object keyed by raw tool names."
            )
        for raw_name, policy in raw_policies.items():
            if (
                not isinstance(raw_name, str)
                or not raw_name
                or not isinstance(policy, dict)
                or set(policy) - {"action", "read_only"}
                or not isinstance(policy.get("action", "confirm"), str)
                or policy.get("action", "confirm") not in {"allow", "confirm", "deny"}
                or type(policy.get("read_only", False)) is not bool
            ):
                raise MCPError(
                    "MCP policies require allow/confirm/deny and a boolean read_only."
                )
            policies[raw_name] = ToolPolicy(
                policy.get("action", "confirm"), policy.get("read_only", False)
            )
        command, args, cwd, url = (
            data.get("command", ""),
            data.get("args", []),
            data.get("cwd"),
            data.get("url", ""),
        )
        if transport == "stdio":
            if (
                not isinstance(command, str)
                or not command.strip()
                or not isinstance(args, list)
                or any(not isinstance(arg, str) for arg in args)
            ):
                raise MCPError(
                    "stdio requires a command and an array of string arguments."
                )
            if url or headers:
                raise MCPError("stdio does not accept HTTP url or headers.")
            if cwd is not None and (not isinstance(cwd, str) or not cwd):
                raise MCPError("MCP cwd must be a nonempty path.")
            if execution.mode == "container":
                if cwd is not None or env:
                    raise MCPError(
                        "Container MCP does not accept host cwd or environment settings."
                    )
            else:
                cwd = Path(cwd).expanduser() if cwd else path.parent
                cwd = (cwd if cwd.is_absolute() else path.parent / cwd).resolve()
                if not cwd.is_dir():
                    raise MCPError("Configured MCP working directory does not exist.")
        else:
            if "execution" in data:
                raise MCPError(
                    "Execution policy applies only to local stdio MCP services."
                )
            if not isinstance(url, str):
                raise MCPError("Streamable HTTP requires an endpoint URL.")
            try:
                parsed = urlsplit(url)
                port = parsed.port
            except ValueError as error:
                raise MCPError("MCP endpoint URL is invalid.") from error
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.fragment
                or port == 0
            ):
                raise MCPError(
                    "MCP endpoint requires HTTP(S), no embedded credentials and no fragment."
                )
            if command or args or cwd is not None or env:
                raise MCPError(
                    "Streamable HTTP does not accept subprocess configuration."
                )
        specs[name] = ServerSpec(
            name,
            transport,
            command,
            tuple(args),
            cwd,
            url,
            copy.deepcopy(env),
            copy.deepcopy(headers),
            policies,
            float(timeout),
            float(connect_timeout),
            execution,
            data.get("revision", "1"),
        )
        if (
            not isinstance(specs[name].revision, str)
            or not 1 <= len(specs[name].revision) <= 100
        ):
            raise MCPError("MCP revision must be bounded host text.")
    return specs


def _no_remote_schema(uri: str):
    from referencing.exceptions import NoSuchResource

    raise NoSuchResource(ref=uri)


def validate_mcp_schema(raw: dict):
    """Both input and SDK-validated output schemas must stay self-contained."""
    from jsonschema import Draft202012Validator
    from referencing import Registry

    schema = copy.deepcopy(raw)
    if not isinstance(schema, dict) or schema.get("type", "object") != "object":
        raise MCPError("MCP schemas must describe an object.")
    schema.setdefault("type", "object")
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as error:
        raise MCPError("MCP server advertised an invalid schema.") from error

    def inspect_refs(value):
        if not isinstance(value, dict):
            return
        for key in ("$ref", "$dynamicRef"):
            item = value.get(key)
            if key in value and (not isinstance(item, str) or not item.startswith("#")):
                raise MCPError("MCP schemas may only reference local definitions.")
        for key in (
            "$defs",
            "definitions",
            "properties",
            "patternProperties",
            "dependentSchemas",
        ):
            for item in value.get(key, {}).values():
                inspect_refs(item)
        for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
            for item in value.get(key, []):
                inspect_refs(item)
        for key in (
            "items",
            "contains",
            "additionalProperties",
            "unevaluatedProperties",
            "propertyNames",
            "not",
            "if",
            "then",
            "else",
        ):
            inspect_refs(value.get(key))

    inspect_refs(schema)
    return schema, Draft202012Validator(
        schema, registry=Registry(retrieve=_no_remote_schema)
    )


@dataclass(frozen=True)
class MCPBinding:
    alias: str
    raw_name: str
    definition: dict
    policy: ToolPolicy
    connection: object
    validator: object
    output_schema: dict | None = None

    def identity(self):
        from .selection import digest

        return {
            "server": self.connection.spec.name,
            "raw_tool": self.raw_name,
            "alias": self.alias,
            "definition": digest(
                {"input": self.definition, "output": self.output_schema}
            ),
            "policy": {
                "action": self.policy.action,
                "read_only": self.policy.read_only,
            },
        }

    def validate(self, arguments: dict) -> None:
        if not isinstance(arguments, dict):
            raise MCPError("MCP arguments must be an object.")
        try:
            error = next(self.validator.iter_errors(arguments), None)
        except Exception as failure:
            raise MCPError(
                "MCP input schema could not validate these arguments."
            ) from failure
        if error is not None:
            location = "/".join(str(part) for part in error.path) or "arguments"
            raise MCPError(
                f"MCP input validation failed at {location} ({error.validator})."
            )


class _RequestActivity:
    """One call's accepted lifetime, including failed transport cleanup."""

    def __init__(self):
        self.lock = threading.Lock()
        self.started = False
        self.finished = False
        self.operation = track_activity(self)

    def start(self):
        with self.lock:
            if self.finished:
                return False
            self.started = True
            return True

    def finish(self):
        with self.lock:
            if not self.finished:
                self.finished = True
                settle_activity(self.operation, self)

    def abandon(self):
        with self.lock:
            if not self.started and not self.finished:
                self.finished = True
                settle_activity(self.operation, self)


class _Connection:
    """Each SDK context is entered and exited by this one long-lived task."""

    def __init__(self, spec: ServerSpec, owner=None, workspace=None, environment=None):
        self.spec = spec
        self.environment = environment
        self.identity = "mcp_" + uuid.uuid4().hex
        self.owner = owner
        self.workspace = workspace
        self.queue = asyncio.Queue()
        self.ready = asyncio.get_running_loop().create_future()
        self.task = asyncio.create_task(self._serve())
        self.closed = False
        self.stopping = False
        self.info = None
        self.tools = []
        self.secrets = []
        self.active_waiter = None
        self.requests = set()
        self.cancel_requested = False
        self.used_at = time.monotonic()

    def abort(self):
        self.stopping = True
        if self.owner is not None:
            self.owner.cancel.set()
        if not self.cancel_requested and not self.task.done():
            self.cancel_requested = True
            self.task.cancel()

    async def _list_tools(self, session):
        tools, cursor, seen = [], None, set()
        for _ in range(100):
            result = await session.list_tools(cursor=cursor)
            for tool in result.tools:
                definition = tool.model_dump(
                    mode="json", by_alias=True, exclude_none=True
                )
                validate_mcp_schema(definition["inputSchema"])
                if "outputSchema" in definition:
                    validate_mcp_schema(definition["outputSchema"])
                tools.append(definition)
                if (
                    len(tools) > 128
                    or sum(len(json.dumps(item).encode()) for item in tools)
                    > 8 * 1024 * 1024
                ):
                    raise MCPError(
                        "MCP tool discovery exceeds the count or content limit."
                    )
            cursor = result.nextCursor
            if cursor is None:
                return tools
            if cursor in seen:
                raise MCPError("MCP tools/list repeated a pagination cursor.")
            seen.add(cursor)
        raise MCPError("MCP tools/list pagination limit exceeded.")

    async def _serve(self):
        pending = None
        try:
            from mcp import ClientSession
            from mcp.client.stdio import get_default_environment
            from mcp.client.streamable_http import streamable_http_client

            from homebrew_harness.tools.mcp.stdio import controlled_stdio

            async with AsyncExitStack() as stack:
                spec = self.spec
                if spec.transport == "stdio":
                    from homebrew_harness.configuration import public_environment

                    env = public_environment(
                        {
                            key: value
                            for key, value in get_default_environment().items()
                            if key not in PRIVATE_MODEL_ENV
                        }
                    )
                    supplied = _resolve_values(spec.env, self.environment)
                    env.update(supplied)
                    self.secrets = [
                        value
                        for key, value in supplied.items()
                        if re.search(r"key|token|secret|password", key, re.I) and value
                    ]
                    if self.owner is not None and spec.execution.mode == "host":
                        self.owner.environment = env
                    streams = await stack.enter_async_context(
                        controlled_stdio(
                            spec,
                            {}
                            if self.owner is not None
                            and spec.execution.mode == "container"
                            else env,
                            owner=self.owner,
                        )
                    )
                else:
                    headers = _resolve_values(spec.headers, self.environment)
                    self.secrets = [
                        spec.url,
                        *[
                            value
                            for key, value in headers.items()
                            if re.search(
                                r"authorization|key|token|secret|password", key, re.I
                            )
                            and value
                        ],
                    ]
                    for key, value in headers.items():
                        if key.lower() == "authorization" and " " in value:
                            self.secrets.append(value.split(" ", 1)[1])
                    http = await stack.enter_async_context(
                        httpx.AsyncClient(
                            headers=headers,
                            timeout=httpx.Timeout(
                                spec.connect_timeout_seconds, read=300
                            ),
                            follow_redirects=False,
                        )
                    )
                    streams = await stack.enter_async_context(
                        streamable_http_client(spec.url, http_client=http)
                    )
                session = await stack.enter_async_context(
                    ClientSession(
                        streams[0],
                        streams[1],
                        read_timeout_seconds=timedelta(
                            seconds=spec.connect_timeout_seconds
                        ),
                    )
                )
                initialized = await session.initialize()
                self.info = initialized.protocolVersion
                self.tools = await self._list_tools(session)
                if self.ready.cancelled():
                    return
                self.ready.set_result(None)
                while True:
                    operation, payload, pending, deadline = await self.queue.get()
                    if operation == "close":
                        break
                    if pending.cancelled():
                        continue
                    if self.stopping:
                        pending.set_exception(
                            MCPError("MCP session is closing; request was not sent.")
                        )
                        continue
                    try:
                        if deadline.remaining() == 0:
                            pending.set_exception(
                                MCPError(
                                    "MCP queue deadline expired; request was not sent."
                                )
                            )
                            pending = None
                            continue
                        self.active_waiter = pending
                        ticket = getattr(pending, "_mcp_ticket", None)
                        if ticket is not None:
                            if not ticket.start():
                                self.active_waiter = None
                                if not pending.done():
                                    pending.set_exception(
                                        MCPError(
                                            "MCP request cancelled before dispatch."
                                        )
                                    )
                                pending = None
                                continue
                            self.requests.add(ticket)
                        if operation == "list":
                            result = await asyncio.wait_for(
                                self._list_tools(session), timeout=deadline.remaining()
                            )
                        else:
                            result = await asyncio.wait_for(
                                session.call_tool(
                                    payload[0],
                                    arguments=payload[1],
                                    read_timeout_seconds=timedelta(
                                        seconds=spec.timeout_seconds
                                    ),
                                ),
                                timeout=deadline.remaining(),
                            )
                            result = result.model_dump(
                                mode="json", by_alias=True, exclude_none=True
                            )
                        if ticket is not None:
                            ticket.finish()
                            self.requests.discard(ticket)
                        if not pending.done():
                            pending.set_result(result)
                    except Exception as error:
                        if not pending.done():
                            pending.set_exception(
                                MCPError(
                                    f"MCP request failed ({type(error).__name__}); outcome may be unknown. It was not retried."
                                )
                            )
                        self.stopping = True
                        pending = None
                        break  # Reconnect explicitly after protocol/transport failure.
                    self.active_waiter = None
                    pending = None
        except BaseException as error:
            safe = MCPError(f"MCP transport stopped ({type(error).__name__}).")
            safe.__cause__ = error
            if not self.ready.done():
                self.ready.set_exception(safe)
            if pending is not None and not pending.done():
                pending.set_exception(safe)
        finally:
            self.closed = True
            for ticket in self.requests:
                ticket.finish()
            self.requests.clear()
            while not self.queue.empty():
                _, _, waiter, _ = self.queue.get_nowait()
                if not waiter.done():
                    waiter.set_exception(MCPError("MCP transport is closed."))
            if pending is not None and not pending.done():
                pending.set_result(None)

    async def request(self, operation: str, payload=None, deadline=None, ticket=None):
        if self.closed or self.stopping:
            raise MCPError(
                "MCP transport is closed; reconnect explicitly before using it."
            )
        self.used_at = time.monotonic()
        waiter = asyncio.get_running_loop().create_future()
        waiter._mcp_ticket = ticket
        deadline = deadline or Deadline.after(self.spec.timeout_seconds)
        await self.queue.put((operation, payload, waiter, deadline))
        try:
            return await waiter
        except asyncio.CancelledError:
            # A timed-out queued call is skipped. An active call invalidates its transport.
            if self.active_waiter is waiter:
                self.abort()
            raise

    async def close(self):
        self.stopping = True
        if not self.closed:
            waiter = asyncio.get_running_loop().create_future()
            waiter.set_result(
                None
            )  # The actor task itself is the close acknowledgement.
            await self.queue.put(("close", None, waiter, Deadline.after(0)))
        await self.task

    async def close_idle(self):
        # This decision and the stopping flag execute on the SDK event loop;
        # a queued or active invocation cannot race an idle eviction.
        if (
            self.closed
            or self.stopping
            or self.active_waiter is not None
            or not self.queue.empty()
        ):
            return False
        await self.close()
        return self.owner is None or not self.owner._leased


class MCPRuntime:
    def __init__(
        self,
        specs: dict[str, ServerSpec],
        *,
        workdir: Path,
        reserved_names=(),
        environment=None,
        selection=None,
        delegation="lead_only",
        resource_scope=None,
        max_container_connections=8,
    ):
        if (
            type(max_container_connections) is not int
            or not 1 <= max_container_connections <= 32
        ):
            raise MCPError("MCP container connection limit must be between 1 and 32.")
        self.max_container_connections = max_container_connections
        from .selection import declaration, validate_selection, host_executable
        from homebrew_harness.execution.test_runner import executable_identity

        self.environment = (
            dict(os.environ if environment is None else environment)
            if environment is not None or selection is not None
            else None
        )
        self.selection = (
            validate_selection(selection) if selection is not None else None
        )
        if not isinstance(delegation, str) or delegation not in {
            "lead_only",
            "read_only",
        }:
            raise MCPError("Invalid MCP delegation policy.")
        self.delegation = delegation
        self.resource_scope = dict(resource_scope or {})
        self.specs = (
            {name: specs[name] for name in self.selection}
            if self.selection is not None and set(self.selection) <= set(specs)
            else dict(specs)
        )
        self.declaration = (
            declaration(self.specs, self.selection, self.environment)
            if self.selection is not None
            else None
        )
        self.expected_manifest = None
        self.prepared = False
        self._host_executables = {}
        if self.selection is not None:
            for name, spec in self.specs.items():
                if spec.transport == "stdio" and spec.execution.mode == "host":
                    executable = host_executable(
                        spec, _resolve_values(spec.env, self.environment)
                    )
                    self._host_executables[name] = (
                        executable,
                        executable_identity(executable),
                    )
        self.workdir = canonical_root(workdir)
        self.leases = ExecutionLeases(self.workdir)
        self._scoped_bindings = {}
        self.workspace_scoped = any(
            spec.execution.inputs == "workspace" for spec in self.specs.values()
        )
        self.reserved_names = set(reserved_names)
        self._connections: dict[str, _Connection] = {}
        self._bindings: dict[str, MCPBinding] = {}
        self._lock, self._connect_lock = threading.RLock(), threading.RLock()
        self._loop = None
        self._thread = None
        self._closed = False
        self._actors: set[_Connection] = set()
        self._shutdown = Shutdown()
        self._close_finished = self._shutdown.finished
        self._loop_workers = Workers()
        self._loop_ready = threading.Event()
        self._cleanup_future = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def _ensure_loop(self):
        with acquired(self._lock, operation_deadline(5)):
            if self._closed:
                raise MCPError("MCP runtime is closed.")
            if self._loop is not None:
                return
            started = self._loop_ready
            started.clear()

            def run():
                # SDK exception tracebacks can contain remote URLs or payloads.
                # The harness reports sanitized MCPError messages instead.
                logger = logging.getLogger("mcp")
                logger.addHandler(logging.NullHandler())
                logger.propagate = False
                self._loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self._loop)
                started.set()
                self._loop.run_forever()
                self._loop.close()

            try:
                self._loop_workers.start(
                    run,
                    name="mcp-transport",
                    track_operation=False,
                    on_created=lambda thread: setattr(self, "_thread", thread),
                )
            except WorkAccepted:
                pass  # The accepted event loop keeps its resource owner.
            if not started.wait(timeout=min(5, operation_deadline(5).remaining())):
                raise MCPError("Could not start MCP transport loop.")

    def _submit(self, coroutine, timeout: float):
        deadline = operation_deadline(timeout)
        try:
            check(deadline)
            self._ensure_loop()
        except BaseException:
            coroutine.close()
            raise
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        except Exception as error:
            coroutine.close()
            raise MCPError("MCP transport loop is unavailable.") from error
        try:
            return wait_future(future, deadline)
        except (TimeoutError, ModelGatewayError) as error:
            future.cancel()
            raise MCPError(
                "MCP request timed out; outcome may be unknown and no automatic retry was made."
            ) from error
        except MCPError:
            raise
        except Exception as error:
            raise MCPError(f"MCP client failed ({type(error).__name__}).") from error

    async def _open(self, spec: ServerSpec, workspace=None, deadline=None):
        if self._closed:
            raise MCPError("MCP runtime closed before connection acceptance.")
        if spec.name in self._host_executables:
            from homebrew_harness.execution.test_runner import executable_identity

            executable, identity = self._host_executables[spec.name]
            if executable_identity(executable) != identity:
                raise MCPBindingChanged(
                    "MCP executable changed before connection acceptance."
                )
            spec = replace(spec, command=executable)
        owner = (
            MCPContainerOwner(
                spec,
                self.workdir,
                workspace,
                self.leases,
                deadline or Deadline.after(spec.connect_timeout_seconds),
                resource_scope=self.resource_scope,
            )
            if spec.execution.mode == "container"
            else None
        )
        if owner is None and spec.transport == "stdio" and self.resource_scope:
            from homebrew_harness.execution.mcp_process import MCPProcessOwner

            owner = MCPProcessOwner(
                spec,
                self.workdir,
                self.leases,
                self.resource_scope,
                deadline or Deadline.after(spec.connect_timeout_seconds),
                self.environment,
            )
        connection = _Connection(spec, owner, workspace, self.environment)
        self._actors.add(connection)
        try:
            await connection.ready
            return connection
        except BaseException:
            connection.abort()
            await asyncio.gather(connection.task, return_exceptions=True)
            raise

    def _make_bindings(
        self, connection: _Connection, definitions: list[dict]
    ) -> dict[str, MCPBinding]:
        bindings = {}
        for tool in definitions:
            raw = tool.get("name")
            if (
                self.selection is not None
                and raw not in self.selection[connection.spec.name]
            ):
                continue
            alias = f"mcp__{normalize_mcp_name(connection.spec.name)}__{normalize_mcp_name(raw)}"
            if len(alias) > 64 or alias in bindings or alias in self.reserved_names:
                raise MCPError(
                    "MCP names collide after normalization or exceed the 64-character tool-name limit."
                )
            schema, validator = validate_mcp_schema(tool.get("inputSchema"))
            if "outputSchema" in tool:
                validate_mcp_schema(tool["outputSchema"])
            policy = connection.spec.tools.get(raw, ToolPolicy())
            description = tool.get("description", "")
            if not isinstance(description, str):
                raise MCPError("MCP tool description must be text.")
            definition = {
                "type": "function",
                "function": {
                    "name": alias,
                    "description": f"External MCP tool. Host policy: {policy.action}. "
                    + self._redact(description, connection)
                    + (
                        " Container inputs: "
                        + connection.spec.execution.inputs
                        + "; no host writeback."
                        if getattr(connection, "owner", None) is not None
                        and connection.spec.execution.mode == "container"
                        else ""
                    ),
                    "parameters": self._redact(schema, connection),
                },
            }
            bindings[alias] = MCPBinding(
                alias,
                raw,
                definition,
                policy,
                connection,
                validator,
                copy.deepcopy(tool.get("outputSchema")),
            )
            if (
                self.expected_manifest is not None
                and self.expected_manifest.get(alias) != bindings[alias].identity()
            ):
                raise MCPBindingChanged("MCP tool definition changed before reconnect.")
        if self.selection is not None:
            from .selection import selected_names

            required = selected_names(
                {connection.spec.name: self.selection[connection.spec.name]}
            )
            if required - set(bindings):
                raise MCPSelectionMissing(
                    "Selected MCP tools are missing from the service."
                )
        return bindings

    def connect(self, name: str, *, workdir=None, task_id=None) -> dict:
        if name not in self.specs:
            raise MCPError(
                "Unknown MCP server. Choose a name from the host configuration."
            )
        spec = self.specs[name]
        workspace = (
            MCPWorkspace.select(self.workdir, workdir or self.workdir, task_id)
            if spec.execution.inputs == "workspace"
            else None
        )
        key = (name, workspace.key) if workspace else name
        with acquired(
            self._connect_lock, operation_deadline(spec.connect_timeout_seconds)
        ):
            for old_key, old_connection in list(self._connections.items()):
                if old_connection.closed and old_connection.task.done():
                    self._connections.pop(old_key, None)
                    self._scoped_bindings.pop(old_key, None)
                    self._bindings = {
                        alias: binding
                        for alias, binding in self._bindings.items()
                        if binding.connection is not old_connection
                    }
            self._actors = {
                actor
                for actor in self._actors
                if not actor.task.done()
                or actor.owner is not None
                and actor.owner._leased
            }
            connection = self._connections.get(key)
            if connection is not None and (connection.closed or connection.stopping):
                self._disconnect_key(key)
                connection = None
            if connection is not None and workspace is not None:
                try:
                    connection.owner.validate(
                        self.workdir,
                        workspace.cwd,
                        task_id,
                        operation_deadline(spec.connect_timeout_seconds),
                    )
                except ValueError:
                    self._disconnect_key(key)
                    connection = None
            new = connection is None
            if new:

                def container_count():
                    return sum(
                        c.spec.execution.mode == "container"
                        and (not c.closed or c.owner is not None and c.owner._leased)
                        for c in self._actors
                    )

                if (
                    self.selection is not None
                    and workspace is not None
                    and container_count() >= self.max_container_connections
                ):
                    candidates = sorted(
                        [
                            (k, c)
                            for k, c in self._connections.items()
                            if c.workspace is not None
                            and c.spec.execution.mode == "container"
                        ],
                        key=lambda item: item[1].used_at,
                    )
                    for old_key, actor in candidates:
                        if self._submit(
                            actor.close_idle(),
                            operation_deadline(
                                spec.connect_timeout_seconds
                            ).remaining(),
                        ):
                            self._disconnect_key(old_key)
                            if container_count() < self.max_container_connections:
                                break
                if (
                    spec.execution.mode == "container"
                    and container_count() >= self.max_container_connections
                ):
                    raise MCPError(
                        "Too many active MCP containers; close unused host connections before opening another."
                    )
                connection = self._submit(
                    self._open(
                        spec,
                        workspace,
                        operation_deadline(spec.connect_timeout_seconds),
                    ),
                    spec.connect_timeout_seconds,
                )
                definitions = connection.tools
            else:
                definitions = self._submit(
                    connection.request(
                        "list",
                        deadline=operation_deadline(spec.connect_timeout_seconds),
                    ),
                    spec.connect_timeout_seconds,
                )
            try:
                candidate = self._make_bindings(connection, definitions)
                with self._lock:
                    other = {
                        alias: binding
                        for alias, binding in self._bindings.items()
                        if binding.connection.spec.name != name
                    }
                    views = [dict(other)]
                    if workspace:
                        for other_key, group in self._scoped_bindings.items():
                            if other_key != key and other_key[1] == workspace.key:
                                views[0].update(group)
                    else:
                        scoped = {}
                        for (_, scope_key), group in self._scoped_bindings.items():
                            scoped.setdefault(scope_key, dict(other)).update(group)
                        views.extend(scoped.values())
                    if any(
                        set(candidate) & set(view)
                        or len(view) + len(candidate) + len(self.reserved_names) > 128
                        for view in views
                    ):
                        raise MCPError(
                            "MCP tool pool collides or exceeds 128 total tools."
                        )
                    self._connections[key] = connection
                    if workspace:
                        self._scoped_bindings[key] = candidate
                    else:
                        self._bindings = {**other, **candidate}
            except Exception:
                if new:
                    self._submit(connection.close(), spec.timeout_seconds)
                else:
                    self._disconnect_key(key)
                raise
            return {
                "server": name,
                "transport": spec.transport,
                "protocol": connection.info,
                "tools": list(candidate),
                "execution": connection.owner.metadata()
                if connection.owner
                else {"mode": "host"},
            }

    def run_connect(self, name: str, *, workdir: Path) -> str:
        return json.dumps(
            self.connect(
                name, workdir=workdir, task_id=execution_context("lead").task_id
            ),
            ensure_ascii=False,
        )

    def snapshot(
        self, *, workdir=None, task_id=None, allow_workspace=True
    ) -> tuple[list[dict], dict[str, MCPBinding]]:
        selected = (
            MCPWorkspace.select(self.workdir, workdir or self.workdir, task_id)
            if self.workspace_scoped and allow_workspace
            else None
        )
        with self._lock:
            bindings = dict(self._bindings)
            for (name, key), group in self._scoped_bindings.items():
                if selected is not None and key == selected.key:
                    bindings.update(group)
            bindings = {
                name: binding
                for name, binding in bindings.items()
                if not binding.connection.closed and not binding.connection.stopping
            }
            for binding in bindings.values():
                binding.connection.used_at = time.monotonic()
            return [
                copy.deepcopy(binding.definition) for binding in bindings.values()
            ], bindings

    def manifest(self, *, workdir=None, task_id=None):
        _, bindings = self.snapshot(workdir=workdir, task_id=task_id)
        result = {name: binding.identity() for name, binding in bindings.items()}
        if self.selection is not None:
            from .selection import selected_names

            if set(result) != set(selected_names(self.selection)):
                raise MCPError("Selected MCP pool is not ready in this workspace.")
        return result

    def descriptor(self, name, *, workdir=None, task_id=None):
        _, bindings = self.snapshot(workdir=workdir, task_id=task_id)
        binding = bindings.get(name)
        if binding is None:
            raise MCPError("MCP invocation has no current binding.")
        self.validate_context(
            binding, workdir=workdir, task_id=task_id, check_inputs=False
        )
        value = {
            "binding": binding.identity(),
            "declaration": self.declaration,
            "cwd": str((workdir or self.workdir).relative_to(self.workdir)),
            "task_id": task_id,
        }
        if binding.connection.spec.execution.mode == "container":
            value["execution"] = binding.connection.owner.metadata()
            value["execution"].pop("cleanup_complete", None)
        return value

    def visible(self, binding, role):
        return (
            self.selection is None
            or role == "lead"
            or self.delegation == "read_only"
            and binding.policy.action == "allow"
            and binding.policy.read_only
        )

    def ensure_scope(self, *, workdir=None, task_id=None, role="lead"):
        if self.selection is None or not self.prepared:
            return
        for name, tools in self.selection.items():
            if role != "lead" and (
                self.delegation != "read_only"
                or not any(
                    self.specs[name].tools[t].action == "allow"
                    and self.specs[name].tools[t].read_only
                    for t in tools
                )
            ):
                continue
            spec = self.specs[name]
            workspace = (
                MCPWorkspace.select(self.workdir, workdir or self.workdir, task_id)
                if spec.execution.inputs == "workspace"
                else None
            )
            key = (name, workspace.key) if workspace else name
            with self._lock:
                connection = self._connections.get(key)
            if (
                connection is not None
                and not connection.closed
                and not connection.stopping
            ):
                if workspace is None:
                    continue
                try:
                    connection.owner.validate(
                        self.workdir,
                        workspace.cwd,
                        task_id,
                        operation_deadline(spec.connect_timeout_seconds),
                    )
                    continue
                except ValueError:
                    pass
            self.connect(name, workdir=workdir, task_id=task_id)

    def checkpoint(self, *, workdir=None, task_id=None):
        with self._lock:
            bindings = dict(self._bindings)
            if self.workspace_scoped:
                scope = MCPWorkspace.select(
                    self.workdir, workdir or self.workdir, task_id
                )
                for (_, key), group in self._scoped_bindings.items():
                    if key == scope.key:
                        bindings.update(group)
        selection = self.selection or {}
        if self.selection is None:
            for binding in bindings.values():
                selection.setdefault(binding.connection.spec.name, []).append(
                    binding.raw_name
                )
        from .selection import declaration

        try:
            declared = (
                self.declaration
                if self.selection is not None
                else declaration(self.specs, selection, self.environment)
            )
        except ValueError:
            return {"supported": False}
        return {
            "supported": True,
            "declaration": declared,
            "manifest": {name: b.identity() for name, b in bindings.items()},
            "delegation": self.delegation,
        }

    def _redact(self, value, connection):
        register_private_values(*connection.secrets)
        value = redact(value)
        if isinstance(value, str):
            for secret in connection.secrets:
                value = value.replace(secret, "[redacted]")
        elif isinstance(value, list):
            value = [self._redact(item, connection) for item in value]
        elif isinstance(value, dict):
            value = {key: self._redact(item, connection) for key, item in value.items()}
        return value

    def validate_context(
        self, binding, *, workdir=None, task_id=None, deadline=None, check_inputs=True
    ):
        if binding.connection not in self._actors:
            raise MCPError("MCP binding does not belong to this runtime.")
        if binding.connection.closed or binding.connection.stopping:
            raise MCPError("MCP connection is closing; reconnect explicitly.")
        if (
            binding.connection.owner is not None
            and binding.connection.spec.execution.mode == "container"
        ):
            try:
                binding.connection.owner.validate(
                    self.workdir,
                    workdir or self.workdir,
                    task_id,
                    deadline
                    or operation_deadline(binding.connection.spec.timeout_seconds),
                    check_inputs=check_inputs,
                )
            except ValueError as error:
                raise MCPError(str(error)) from None

    def call(
        self, binding: MCPBinding, arguments: dict, *, workdir=None, task_id=None
    ) -> str:
        binding.validate(arguments)
        deadline = operation_deadline(binding.connection.spec.timeout_seconds)
        self.validate_context(
            binding, workdir=workdir, task_id=task_id, deadline=deadline
        )
        ticket = _RequestActivity()
        try:
            result = self._submit(
                binding.connection.request(
                    "call",
                    (binding.raw_name, copy.deepcopy(arguments)),
                    deadline=deadline,
                    ticket=ticket,
                ),
                deadline.remaining(),
            )
        except MCPError as error:
            error.outcome = "unknown" if ticket.started else "not_executed"
            # Publish invalidation before the caller can explicitly reconnect.
            # A cancelled queued request never invalidates a peer's active call.
            if ticket.started:
                binding.connection.stopping = True
                if self._loop is not None:
                    self._loop.call_soon_threadsafe(binding.connection.abort)
            if self.selection is not None and binding.policy.read_only:
                return json.dumps(
                    {
                        "error": str(error),
                        "outcome": "failed",
                        "request_state": "sent_response_unknown"
                        if ticket.started
                        else "not_sent",
                        "read_only": True,
                        "retryable": True,
                    }
                )
            raise
        finally:
            ticket.abandon()  # A sent failed request belongs to its closing actor.
        result = self._redact(result, binding.connection)
        if binding.connection.owner is not None:
            result["harness_execution"] = binding.connection.owner.metadata()
        return json.dumps(result, ensure_ascii=False)

    def _disconnect_key(self, key):
        connection = self._connections.pop(key, None)
        with self._lock:
            self._scoped_bindings.pop(key, None)
            if connection is not None:
                self._bindings = {
                    alias: binding
                    for alias, binding in self._bindings.items()
                    if binding.connection is not connection
                }
        if connection is not None and not connection.closed:
            self._submit(connection.close(), connection.spec.timeout_seconds)

    def disconnect(self, name: str, *, workdir=None, task_id=None) -> None:
        selected = (
            MCPWorkspace.select(self.workdir, workdir, task_id)
            if workdir is not None and self.specs[name].execution.inputs == "workspace"
            else None
        )
        with acquired(self._connect_lock, operation_deadline(5)):
            for key, connection in list(self._connections.items()):
                if connection.spec.name == name and (
                    selected is None or connection.workspace == selected
                ):
                    self._disconnect_key(key)

    def connection_status(self):
        with self._lock:
            actors = list(self._actors)
        return {
            "connections": [
                {
                    "id": c.identity,
                    "server": c.spec.name,
                    "transport": c.spec.transport,
                    "execution": c.spec.execution.mode,
                    "state": "closed"
                    if c.closed
                    else "closing"
                    if c.stopping
                    else "open"
                    if c.ready.done()
                    else "connecting",
                    "task_id": c.workspace.task_id if c.workspace else None,
                    "workspace": c.workspace.cwd.relative_to(self.workdir).as_posix()
                    if c.workspace
                    else None,
                    "cleanup_pending": c.stopping and not c.closed,
                }
                for c in sorted(actors, key=lambda actor: actor.identity)
            ]
        }

    def close_connection(self, identity):
        if not isinstance(identity, str) or not re.fullmatch(
            r"mcp_[0-9a-f]{32}", identity
        ):
            raise MCPError("Use a connection ID from /mcp.")
        with acquired(self._connect_lock, operation_deadline(2)):
            with self._lock:
                connection = next(
                    (c for c in self._actors if c.identity == identity), None
                )
                if connection is None:
                    raise MCPError(
                        "MCP connection is no longer registered; inspect /mcp."
                    )
                for key, selected in list(self._connections.items()):
                    if selected is connection:
                        self._connections.pop(key, None)
                        self._scoped_bindings.pop(key, None)
                self._bindings = {
                    name: binding
                    for name, binding in self._bindings.items()
                    if binding.connection is not connection
                }
                connection.stopping = True
                if connection.owner is not None:
                    connection.owner.cancel.set()
            if not connection.closed:
                self._loop.call_soon_threadsafe(connection.abort)
            return {
                "id": identity,
                "state": "closed" if connection.closed else "closing",
                "cleanup_pending": not connection.closed,
            }

    def close(self, timeout=5) -> None:
        """Stop accepting requests; retries share the existing shutdown owner."""
        with self._lock:
            self._closed = True
        self._shutdown.request(self._finish_close, timeout)
        if (
            self._shutdown.error
            and not self._shutdown.started
            and not self._close_finished.is_set()
        ):
            raise ClosePending("MCP close was not completed; retry host close.")

    def _finish_close(self):
        # _ensure_loop serialized acceptance under _lock. Any accepted transport
        # thread must become visible before the loop can be shut down.
        thread = self._thread
        if thread is not None and thread.ident is not None:
            while self._loop is None and thread.is_alive():
                self._loop_ready.wait(0.05)
        loop = self._loop
        if loop is None:
            return

        async def finish_actors():
            actors = list(self._actors)
            for connection in actors:
                connection.stopping = True
                if not connection.task.done():
                    connection.abort()
            await asyncio.gather(*(c.task for c in actors), return_exceptions=True)

        if self._cleanup_future is None:
            if loop.is_closed():
                if any(not actor.closed for actor in self._actors):
                    raise ClosePending(
                        "MCP event loop closed before actor cleanup completed."
                    )
            else:
                coroutine = finish_actors()
                try:
                    self._cleanup_future = asyncio.run_coroutine_threadsafe(
                        coroutine, loop
                    )
                except BaseException:
                    coroutine.close()
                    raise
        if self._cleanup_future is not None:
            self._cleanup_future.result()
        for actor in self._actors:
            if actor.owner is not None and actor.owner._leased:
                actor.owner.settle()
        if not loop.is_closed():
            loop.call_soon_threadsafe(loop.stop)
        self._loop_workers.wait(None)

    def wait_closed(self, timeout=5):
        return self._close_finished.wait(timeout)

    def shutdown_status(self):
        return {
            "closed": self._closed,
            "cleanup_pending": self._closed and not self._close_finished.is_set(),
            "retryable": self._closed
            and not self._shutdown.started
            and not self._close_finished.is_set(),
            "close_error": self._shutdown.error,
        }
