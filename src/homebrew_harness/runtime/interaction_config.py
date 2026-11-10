"""Small host configuration for automatic entry; no CLI Namespace in runtime."""

import json
import math
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from pathlib import Path

from homebrew_harness.execution.bash_policy import HOST_POLICY, load_bash_policy
from homebrew_harness.execution.model_connections import HostModels, read_environment
from homebrew_harness.execution.model_gateway import GatewayLimits, ModelGateway
from homebrew_harness.execution.test_runner import load_test_runner
from homebrew_harness.runtime.capabilities import OPTIONAL, Capabilities


@dataclass(frozen=True)
class InteractionConfig:
    env_file: Path = Path.home() / ".config/homebrew-harness/.env"
    models_config: Path | None = None
    test_config: Path = Path.home() / ".config/homebrew-harness/tests.json"
    bash_config: Path | None = None
    mcp_config: Path = Path.home() / ".config/homebrew-harness/mcp.json"
    control_config: Path | None = None
    lead_model: str | None = None
    allowed: tuple = ("subagents", "background", "tasks", "teams", "worktrees")
    allow_tests: tuple = ()
    write_paths: tuple = (".",)
    memory_mode: str = "off"
    demo_workflows: bool = False
    managed_bash: bool = False
    mcp_delegation: str = "lead_only"
    model_planning: bool = True
    prefer_session: bool = False
    detached: bool = False
    total_seconds: float = 3600
    turn_seconds: float = 300
    request_seconds: float = 60
    shutdown_seconds: float = 5
    context_chars: int = 50000
    output_tokens: int = 4096
    max_turns: int = 20

    def __post_init__(self):
        if (
            not isinstance(self.allowed, (list, tuple))
            or any(not isinstance(v, str) for v in self.allowed)
            or set(self.allowed) - OPTIONAL
        ):
            raise ValueError("Invalid automatic host capability catalog.")
        Capabilities(self.allowed, self.memory_mode)
        if not isinstance(self.mcp_delegation, str) or self.mcp_delegation not in {
            "lead_only",
            "read_only",
        }:
            raise ValueError("Invalid MCP delegation preference.")
        for name in (
            "demo_workflows",
            "managed_bash",
            "model_planning",
            "prefer_session",
            "detached",
        ):
            if type(getattr(self, name)) is not bool:
                raise ValueError("Host preferences must be boolean.")
        for name in (
            "total_seconds",
            "turn_seconds",
            "request_seconds",
            "shutdown_seconds",
        ):
            value = getattr(self, name)
            if (
                type(value) not in {int, float}
                or not math.isfinite(value)
                or not (0.1 if name == "total_seconds" else 0.01) <= value <= 604800
            ):
                raise ValueError("Invalid host deadline: " + name)
        for name, low, high in (
            ("context_chars", 10000, 2000000),
            ("output_tokens", 128, 32768),
            ("max_turns", 1, 100),
        ):
            if (
                type(getattr(self, name)) is not int
                or not low <= getattr(self, name) <= high
            ):
                raise ValueError("Invalid host limit: " + name)
        for name in ("allow_tests", "write_paths"):
            if not isinstance(getattr(self, name), (tuple, list)) or any(
                not isinstance(v, str) or not v for v in getattr(self, name)
            ):
                raise ValueError("Invalid host scope: " + name)
        # Reuse RunSpec's scope validation rather than define another path policy.
        from homebrew_harness.runs.spec import RunSpec

        RunSpec("validate host scope", write_paths=self.write_paths)

    @classmethod
    def load(cls, path, workdir, **overrides):
        data = {}
        if path is not None:
            path = Path(path).expanduser().resolve()
            if path.is_relative_to(Path(workdir).resolve()):
                raise ValueError(
                    "Automatic host configuration must be outside the workspace."
                )
            with path.open("rb") as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise ValueError("Automatic host configuration exceeds 64 KiB.")
            data = json.loads(raw)
            if not isinstance(data, dict) or set(data) - set(cls.__dataclass_fields__):
                raise ValueError("Unknown automatic host settings.")
        for name in (
            "env_file",
            "models_config",
            "test_config",
            "bash_config",
            "mcp_config",
            "control_config",
        ):
            if name in data and data[name] is not None:
                data[name] = Path(data[name]).expanduser()
        data.update(
            {key: value for key, value in overrides.items() if value is not None}
        )
        return cls(**data)

    def worker_arguments(self, workdir):
        args = []
        for name in (
            "env_file",
            "models_config",
            "test_config",
            "bash_config",
            "mcp_config",
        ):
            path = getattr(self, name)
            if path is not None:
                path = Path(path).expanduser().resolve()
                if path.is_relative_to(Path(workdir).resolve()):
                    raise ValueError(
                        "Execution configuration must remain outside the workspace."
                    )
                args.extend(("--" + name.replace("_", "-"), str(path)))
        return tuple(args)

    def identity(self):
        # Paths are private host references; never copy connection values.
        return json.loads(json.dumps(asdict(self), default=str))


class InteractionAssembler:
    def __init__(
        self,
        workdir,
        config,
        *,
        client_factory=None,
        workflow_registry=None,
        environment=None,
        model_provider=None,
        model_bindings=None,
    ):
        from homebrew_harness.orchestration.workflow_runtime import WorkflowRegistry
        from homebrew_harness.orchestration.workflows import default_registry
        from homebrew_harness.tools.mcp.runtime import load_specs

        self.workdir, self.config = Path(workdir).resolve(), config
        config.worker_arguments(self.workdir)
        self.environment = (
            dict(environment)
            if environment is not None
            else read_environment(config.env_file, self.workdir)
        )
        self.model_bindings = model_bindings
        self.model_provider = model_provider
        self.registry = workflow_registry or (
            default_registry() if config.demo_workflows else WorkflowRegistry()
        )
        self.mcp_specs = (
            load_specs(
                config.mcp_config,
                missing_ok=config.mcp_config == InteractionConfig().mcp_config,
            )
            if "mcp" in config.allowed
            else {}
        )
        self.allowed = set(config.allowed)
        if self.registry.catalog():
            self.allowed.add("workflow")
        else:
            self.allowed.discard("workflow")
        if not self.mcp_specs:
            self.allowed.discard("mcp")
        self.factory = client_factory
        tests = load_test_runner(
            config.test_config,
            self.workdir,
            missing_ok=config.test_config == InteractionConfig().test_config,
        )
        try:
            self.tests_identity = self.test_identity(tests)
            self.planning_context = {
                "tests": sorted(tests.recipes),
                "write_paths": list(config.write_paths),
                "managed_bash": config.managed_bash,
                "mcp": {
                    name: {
                        tool: {"action": policy.action, "read_only": policy.read_only}
                        for tool, policy in spec.tools.items()
                        if policy.action != "deny"
                    }
                    for name, spec in self.mcp_specs.items()
                },
            }
        finally:
            tests.close()
        self.planning_models = self.models()
        try:
            self.binding = self.planning_models.open(
                context_chars=config.context_chars, output_tokens=config.output_tokens
            )
            self.gateway = ModelGateway(
                self.binding.client,
                GatewayLimits(request_timeout=config.request_seconds),
                workdir=self.workdir,
            )
            self.planning_models.track_gateway(self.gateway)
            self.planning_context["model_aliases"] = sorted(
                self.planning_models.registry.specs
            )
            self.bash_identity = (
                load_bash_policy(config.bash_config, self.workdir)
                if config.bash_config
                else HOST_POLICY
            ).identity()
            from homebrew_harness.execution.test_runner import digest

            self.prepared_identity = {
                "model": self.binding.identity(),
                "models": self.planning_models.registry.manifest(),
                "bash": self.bash_identity,
                "tests": self.tests_identity,
                "mcp": digest(
                    json.loads(
                        json.dumps(
                            {
                                name: asdict(spec)
                                for name, spec in self.mcp_specs.items()
                            },
                            default=str,
                        )
                    )
                ),
                "workflows": {
                    entry["name"]: self.registry.get(entry["name"]).version
                    for entry in self.registry.catalog()
                },
            }
        except BaseException:
            if hasattr(self, "gateway"):
                self.gateway.close()
            self.planning_models.close_when_idle(config.shutdown_seconds)
            raise

    @staticmethod
    def test_identity(tests):
        return {
            "recipes": {
                name: tests._fingerprint(recipe)
                for name, recipe in tests.recipes.items()
            },
            "max_runs": tests.max_runs,
            "total_seconds": tests.total_seconds,
        }

    def check_models(self, models, binding):
        if (
            binding.identity() != self.binding.identity()
            or models.registry.manifest() != self.planning_models.registry.manifest()
        ):
            raise ValueError(
                "Host model configuration changed during planning; reopen the host with the intended catalog."
            )

    def models(self):
        from homebrew_harness.execution.providers.backends import backend_scope

        with backend_scope(self.model_bindings, getattr(self, "binding", None)):
            return self._models()

    def _models(self):
        if self.model_provider is not None:
            return self.model_provider()
        return HostModels(
            self.workdir,
            config=self.config.models_config,
            alias=self.config.lead_model,
            environment=self.environment,
            client_factory=self.factory,
        )

    def session(
        self,
        route,
        *,
        resume=None,
        run_control=None,
        hooks=None,
        mode="interactive",
        gateway_limits=None,
    ):
        from homebrew_harness.orchestration.workflow_runtime import (
            ModelWorkflowRunner,
            WorkflowRuntime,
        )
        from homebrew_harness.runtime.session import HarnessSession
        from homebrew_harness.tools.mcp.runtime import MCPRuntime
        from homebrew_harness.tools.catalog import TOOL_SCHEMAS

        c = self.config
        with ExitStack() as resources:
            models = self.models()
            resources.callback(models.close)
            binding = models.open(
                context_chars=c.context_chars, output_tokens=c.output_tokens
            )
            self.check_models(models, binding)
            tests = load_test_runner(
                c.test_config,
                self.workdir,
                missing_ok=c.test_config == InteractionConfig().test_config,
            )
            resources.callback(tests.close)
            if self.test_identity(tests) != self.tests_identity:
                raise ValueError("Test registry changed during task preparation.")
            if run_control is None:
                for name in c.allow_tests:
                    tests.approve(name)
            bash = (
                load_bash_policy(c.bash_config, self.workdir)
                if c.bash_config
                else HOST_POLICY
            )
            if bash.identity() != self.bash_identity:
                raise ValueError("Bash policy changed during task preparation.")
            mcp = (
                MCPRuntime(
                    self.mcp_specs,
                    workdir=self.workdir,
                    reserved_names=TOOL_SCHEMAS,
                    environment=self.environment,
                    selection=route.mcp or None,
                    delegation=c.mcp_delegation,
                    resource_scope={
                        **run_control.header,
                        "session_id": run_control.state["session_id"],
                    }
                    if run_control
                    else None,
                )
                if "mcp" in route.capabilities
                else None
            )
            if mcp:
                resources.callback(mcp.close)
            workflow = (
                WorkflowRuntime(
                    self.workdir, ModelWorkflowRunner(binding), self.registry
                )
                if "workflow" in route.capabilities
                else None
            )
            if workflow:
                resources.callback(workflow.close)
            session = HarnessSession(
                binding.client,
                binding.model,
                self.workdir,
                hooks=hooks,
                mode=mode,
                run_control=run_control,
                model_registry=models.registry,
                lead_model_profile=models.alias,
                test_runner=tests,
                bash_policy=bash,
                mcp_runtime=mcp,
                workflow_runtime=workflow,
                capabilities=Capabilities(route.capabilities, c.memory_mode),
                gateway_limits=gateway_limits
                or GatewayLimits(
                    request_timeout=c.request_seconds, session_timeout=c.total_seconds
                ),
                context_chars=c.context_chars,
                worker_output_tokens=c.output_tokens,
                turn_timeout=c.turn_seconds,
                shutdown_timeout=c.shutdown_seconds,
                max_turns=c.max_turns,
                resume_session=resume,
            )
            resources.pop_all()
            return session

    def run_spec(self, route, prompt, *, goal=None, remaining=None):
        from homebrew_harness.runs.spec import RunSpec

        c = self.config
        # Check resource bindings before accepting a business Run. Probe owns no
        # session lease and performs no business model/tool calls.
        with ExitStack() as resources:
            models = self.models()
            resources.callback(models.close)
            binding = models.open(
                context_chars=c.context_chars, output_tokens=c.output_tokens
            )
            for member in route.members:
                models.registry.validate(member.get("model_profile")) if member.get(
                    "model_profile"
                ) else None
            self.check_models(models, binding)
            tests = load_test_runner(
                c.test_config,
                self.workdir,
                missing_ok=c.test_config == InteractionConfig().test_config,
            )
            resources.callback(tests.close)
            if self.test_identity(tests) != self.tests_identity:
                raise ValueError("Test registry changed during task preparation.")
            for name in c.allow_tests:
                tests._recipe(name)
            if c.bash_config:
                load_bash_policy(c.bash_config, self.workdir)
            spec = RunSpec(
                prompt,
                goal=goal or route.goal_candidate,
                enabled=route.capabilities,
                memory_mode=c.memory_mode,
                mcp=route.mcp,
                mcp_delegation=c.mcp_delegation if route.mcp else "lead_only",
                stages=route.stages,
                members=route.members,
                write_paths=c.write_paths,
                allow_tests=c.allow_tests,
                model_profile=binding.profile,
                context_chars=binding.context_chars,
                output_tokens=binding.max_output_tokens,
                total_seconds=remaining if remaining is not None else c.total_seconds,
                episode_seconds=c.turn_seconds,
                request_seconds=c.request_seconds,
                episode_model_calls=c.max_turns,
                shutdown_seconds=c.shutdown_seconds,
            )
            if c.managed_bash:
                from dataclasses import replace

                spec = replace(spec, tools=tuple(sorted(set(spec.tools) | {"bash"})))
            for definition in [
                spec.goal,
                *(stage["acceptance"] for stage in spec.stages),
            ]:
                for predicate in (definition or {}).get("predicates", []):
                    if predicate["type"] == "tests_passed":
                        tests._recipe(predicate["recipe"])
            if set(spec.enabled) - self.allowed:
                raise ValueError(
                    "Stage definitions implicitly exceed the host capability catalog."
                )
            if "worktrees" in spec.enabled:
                from homebrew_harness.runs.controller import git_head

                git_head(self.workdir)
                # Actual worktree preparation also checks this boundary.
                import subprocess

                result = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=self.workdir,
                    capture_output=True,
                    timeout=5,
                    check=True,
                )
                if result.stdout:
                    raise ValueError("Worktree tasks require a clean HEAD workspace.")
            from homebrew_harness.runs.controller import git_head

            self.run_bindings = {
                "model": binding.identity(),
                "models": models.registry.manifest(),
                "capabilities": Capabilities(spec.enabled, spec.memory_mode).manifest(),
                "bash": (
                    load_bash_policy(c.bash_config, self.workdir)
                    if c.bash_config
                    else HOST_POLICY
                ).identity(),
            }
            if spec.mcp:
                from homebrew_harness.tools.mcp.selection import declaration

                self.run_bindings["mcp"] = declaration(
                    self.mcp_specs, spec.mcp, self.environment
                )
            self.run_head = git_head(self.workdir)
            from homebrew_harness.runs.store import RunStore
            from homebrew_harness.runs.services import RunServices

            service_store = RunStore(self.workdir)
            try:
                services = RunServices.load(
                    service_store, c.control_config, environment=self.environment
                )
                try:
                    self.run_control_binding = {
                        "control_identity": services.identity,
                        "notify_enabled": services.notification is not None,
                        "source_enabled": services.source is not None,
                    }
                finally:
                    services.close()
            finally:
                service_store.close()
            return spec

    def close(self):
        self.gateway.close()
        return self.planning_models.close_when_idle(self.config.shutdown_seconds)
