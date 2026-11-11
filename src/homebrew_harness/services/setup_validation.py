"""Offline deployment checks using the same host and Service contracts as start."""

import os
from pathlib import Path

from homebrew_harness.execution.model_connections import HostModels, read_environment
from homebrew_harness.execution.test_runner import load_test_runner
from homebrew_harness.platforms.registry import connection
from homebrew_harness.runtime.interaction_config import InteractionConfig
from .spec import ServiceSpec


def _external(path, roots):
    path = Path(path).expanduser()
    if not path.is_absolute() or path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ValueError("Deployment files require absolute paths without symlinks.")
    if any(path.resolve().is_relative_to(root) for root in roots):
        raise ValueError("Deployment files must remain outside control and project directories.")
    return path


def validate_service(spec_file, host_file, control):
    """Do not start owners, run tests, call models or query/write a platform."""
    control = Path(control).expanduser().resolve()
    _external(spec_file, (control,))
    spec = ServiceSpec.load(spec_file, control)
    roots = (control, Path(spec.workspace))
    _external(spec_file, roots)
    host_file = _external(host_file, roots)
    config = InteractionConfig.load(host_file, control)
    for key in ("env_file", "models_config", "test_config", "bash_config", "mcp_config", "control_config"):
        value = getattr(config, key)
        if value is not None:
            _external(value, roots)
    config.worker_arguments(spec.workspace)
    environment = read_environment(config.env_file, spec.workspace, private=True)
    models = HostModels(spec.workspace, config=config.models_config, alias=config.lead_model, environment=environment)
    try:
        aliases = {
            spec.maintenance.get("model_profile"),
            spec.delivery.get("review_model"),
            spec.communication.get("model_profile"),
            spec.communication.get("review_model"),
            spec.operations.get("audit_model"),
            spec.run_defaults.get("model_profile"),
            *spec.run_defaults.get("fallback_models", ()),
            *(member.get("model_profile") for member in spec.run_defaults.get("members", ())),
        } - {None}
        for alias in aliases:
            models.registry.validate(alias)
    finally:
        models.close()

    if config.bash_config:
        from homebrew_harness.execution.bash_policy import load_bash_policy

        load_bash_policy(config.bash_config, Path(spec.workspace))
    runner = load_test_runner(config.test_config, Path(spec.workspace))
    names = set(config.allow_tests) | set(spec.run_defaults.get("allow_tests", ()))
    names.update(spec.delivery.get("required_tests", ()))
    names.update(spec.communication.get("required_tests", ()))
    for scope in spec.maintenance.get("scopes", {}).values():
        names.update(scope["allow_tests"])
    try:
        if names - runner.recipes.keys():
            raise ValueError("A selected fixed test is not registered in the host test configuration.")
        if any("lead" not in runner.recipes[name].roles for name in names):
            raise ValueError("Service fixed tests must allow the lead role.")
        if any(not runner.recipes[name].allow_worktrees for name in spec.delivery.get("required_tests", ())):
            raise ValueError("Delivery fixed tests must support isolated candidate workspaces.")
        if spec.communication.get("required_tests") and runner.backend.name != "container":
            raise ValueError("Remote PR tests require the TestRunner container backend; static review needs no Docker.")
        if spec.communication.get("require_tests_for_approval") and not spec.communication.get("required_tests"):
            raise ValueError("Approval test policy requires named PR tests.")
        container = runner.backend.name == "container"
    finally:
        runner.close()

    instances = {}
    for source in spec.sources:
        transport, _identity = connection(source, environment, roots)
        try:
            instances[(source["provider"], source["instance"])] = transport.endpoint.rstrip("/")
        finally:
            transport.close()
    if spec.delivery:
        from .delivery.spec import read_values

        path = spec.delivery.get("credentials_file")
        if path:
            _external(path, roots)
        values = read_values(spec.delivery, environment, roots)
        declaration = {
            **spec.delivery,
            "token_env": spec.delivery["api_token_env"],
        }
        dedicated = read_environment(path, control, private=True, inherit_environment=False) if path else environment
        _check_connection(declaration, dedicated, roots, instances)
        program = Path(spec.delivery["git_executable"])
        if not program.is_file() or not os.access(program, os.X_OK):
            raise ValueError("The declared Git executable is unavailable.")
        # read_values owns all remote/identity/token validation; do not expose values.
        del values
    if spec.communication:
        from .communication.spec import open_projects

        path = spec.communication.get("credentials_file")
        if path:
            _external(path, roots)
        dedicated = read_environment(path, control, private=True, inherit_environment=False) if path else environment
        for declaration in spec.communication["projects"]:
            _check_connection(declaration, dedicated, roots, instances)
        projects = open_projects(spec.communication, environment, roots)
        for adapter, _identity in projects.values():
            adapter.close()

    pending = ["Platform access, Git remote access and server-side branch rules are checked during service operation."]
    if container:
        pending.append("Docker host and image availability have not been tested by this offline check.")
    return {
        "state": "validated",
        "profile": spec.profile,
        "sources": len(spec.sources),
        "fixed_tests": sorted(names),
        "merge_mode": spec.delivery.get("merge", {}).get("mode"),
        "pending": pending,
    }


def _check_connection(declaration, environment, roots, instances):
    transport, _identity = connection(declaration, environment, roots)
    try:
        expected = instances.get((declaration["provider"], declaration["instance"]))
        if expected is not None and expected != transport.endpoint.rstrip("/"):
            raise ValueError("Source and publisher must use the same declared platform instance.")
    finally:
        transport.close()
