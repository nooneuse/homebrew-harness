"""One effective tool snapshot and a monotonic, host-owned delegation scope."""

import copy
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

_ACCESS = ContextVar("effective_tool_access", default=None)


def intersect_access(allowlist):
    if allowlist is not None and (
        not isinstance(allowlist, frozenset)
        or any(not isinstance(name, str) for name in allowlist)
    ):
        raise ValueError(
            "Tool allowlist must be a host-supplied frozenset of tool names."
        )
    parent = _ACCESS.get()
    return (
        parent
        if allowlist is None
        else allowlist
        if parent is None
        else parent & allowlist
    )


@contextmanager
def member_protocol_scope():
    """Host-approved members receive their local plan response endpoint."""
    parent = _ACCESS.get()
    token = _ACCESS.set(parent | {"submit_plan"} if parent is not None else None)
    try:
        yield
    finally:
        _ACCESS.reset(token)


@dataclass(frozen=True)
class ToolPool:
    tools: list
    handlers: dict
    bindings: dict

    @property
    def names(self):
        return frozenset(tool["function"]["name"] for tool in self.tools)

    @contextmanager
    def scope(self):
        token = _ACCESS.set(intersect_access(self.names))
        try:
            yield
        finally:
            _ACCESS.reset(token)

    @classmethod
    def build(
        cls,
        definitions,
        handlers,
        capabilities,
        *,
        tests=None,
        workflow=None,
        background=None,
        mcp=None,
        allow_connect=True,
        allowlist=None,
        bash_policy=None,
        artifacts=None,
        mcp_workdir=None,
        mcp_task_id=None,
        mcp_allow_workspace=True,
        models=None,
        run_tools=frozenset(),
    ):
        access = intersect_access(allowlist)
        catalog = workflow.registry.catalog() if workflow is not None else []
        recipes = tests.recipes if tests is not None else {}
        tools = []
        from homebrew_harness.runs.spec import RUN_TOOLS

        if not isinstance(run_tools, frozenset) or run_tools - RUN_TOOLS:
            raise ValueError(
                "Only managed host control tools can extend this snapshot."
            )
        for definition in definitions:
            if definition["function"]["name"] not in capabilities.tools() | run_tools:
                continue
            tool = copy.deepcopy(definition)
            function = tool["function"]
            name = function["name"]
            if name not in handlers or access is not None and name not in access:
                continue
            if name in {"task", "spawn_teammate"}:
                catalog_models = models.catalog() if models is not None else []
                if catalog_models:
                    function["parameters"]["properties"]["model_profile"]["enum"] = [
                        m["name"] for m in catalog_models
                    ]
                    function["description"] += (
                        " Optional model_profile selects a host model alias; omission inherits the caller model (teammates may use an exact host role default). Available models: "
                        + json.dumps(catalog_models, ensure_ascii=False)
                    )
                else:
                    function["parameters"]["properties"].pop("model_profile", None)
            if (
                name in {"list_artifacts", "get_artifact", "publish_artifacts"}
                and artifacts is None
            ):
                continue
            if name == "bash":
                profiles = getattr(bash_policy, "outputs", ())
                if profiles and artifacts is not None:
                    function["parameters"]["properties"]["artifact_profile"]["enum"] = [
                        p.name for p in profiles
                    ]
                    function["description"] += (
                        " Optional host output rules: "
                        + json.dumps([p.data() for p in profiles])
                        + ". Results are collected into isolated bundles; publish_artifacts needs separate approval."
                    )
                else:
                    function["parameters"]["properties"].pop("artifact_profile", None)
            if name == "workflow":
                if not catalog:
                    continue
                function["description"] += " Saved workflows: " + json.dumps(
                    catalog, ensure_ascii=False
                )
            if name in {"list_tests", "run_tests", "get_test_result"}:
                if not recipes:
                    continue
                if name == "run_tests":
                    function["description"] += " Registered names: " + ", ".join(
                        recipes
                    )
            if name == "bash" and bash_policy is not None:
                function["description"] += (
                    " Execution target: a filtered isolated workspace copy, network disabled, no host-file writeback. Use file tools for persistent changes; run_tests for registered verification."
                    if bash_policy.mode == "container"
                    else " Execution target: host in the assigned cwd."
                )
            if name == "bash" and (background is None or not background.allow_async):
                function["parameters"]["properties"].pop("run_in_background", None)
                function["description"] += (
                    " This loop supports synchronous execution only."
                )
            if name == "connect_mcp":
                if mcp is None or not mcp.specs or not allow_connect:
                    continue
                function["description"] += " Configured servers: " + ", ".join(
                    mcp.specs
                )
                function["parameters"]["properties"]["name"]["enum"] = list(mcp.specs)
            tools.append(tool)
        bindings = {}
        if capabilities.has("mcp") and mcp is not None:
            external, candidates = (
                mcp.snapshot(
                    workdir=mcp_workdir,
                    task_id=mcp_task_id,
                    allow_workspace=mcp_allow_workspace,
                )
                if getattr(mcp, "workspace_scoped", False) is True
                else mcp.snapshot()
            )
            for tool in external:
                name = tool["function"]["name"]
                if access is None or name in access:
                    tools.append(copy.deepcopy(tool))
                    bindings[name] = candidates[name]
        names = [tool["function"]["name"] for tool in tools]
        if len(names) != len(set(names)) or len(names) > 128:
            raise ValueError("Effective tool pool collides or exceeds 128 tools.")
        return cls(
            tools,
            {name: handler for name, handler in handlers.items() if name in names},
            bindings,
        )
