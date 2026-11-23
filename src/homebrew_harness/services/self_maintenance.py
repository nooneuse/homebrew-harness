"""Compile the opt-in coding deployment into existing host-owned contracts."""

from pathlib import Path
import os

from homebrew_harness.execution.bash_policy import load_bash_policy
from homebrew_harness.execution.test_runner import load_test_runner
from homebrew_harness.runs.spec import DEFAULT_TOOLS


CODING_TOOLS = tuple(sorted(set(DEFAULT_TOOLS) | {"bash", "list_artifacts", "get_artifact"}))
REQUIRED_TOOLS = {"read_file", "write_file", "search_text", "list_tests", "run_tests", "get_test_result"}


def defaults(spec):
    """Defaults shape tools and scheduling; the host still declares authority."""
    if spec.profile != "repository_maintenance":
        raise ValueError("self_maintenance requires the repository_maintenance profile.")
    if not isinstance(spec.delivery, dict) or not spec.delivery.get("required_tests"):
        raise ValueError("self_maintenance requires named Delivery required_tests.")
    if not isinstance(spec.maintenance, dict) or not spec.maintenance.get("scopes"):
        raise ValueError("self_maintenance requires an explicit maintenance scope catalog.")
    if not isinstance(spec.run_defaults, dict):
        raise ValueError("Invalid self_maintenance Run defaults.")
    return (
        {"tools": CODING_TOOLS, "allow_tests": spec.delivery["required_tests"], **spec.run_defaults},
        {"base_update": "periodic", "require_review": True, **spec.delivery},
    )


def check_scopes(scopes):
    for name, scope in scopes.items():
        if not REQUIRED_TOOLS <= set(scope["tools"]) or not scope["allow_tests"]:
            raise ValueError(f"self_maintenance scope {name!r} needs file, search and preauthorized test tools and recipes.")


def _program_available(path):
    return Path(path).is_file() and os.access(path, os.X_OK)


def test_registry_identity(runner):
    return {
        "backend": runner.backend.name,
        "environment": runner.backend.fingerprint(),
        "recipes": {name: runner._fingerprint(recipe) for name, recipe in runner.recipes.items()},
        "max_runs": runner.max_runs,
        "total_seconds": runner.total_seconds,
    }


def verify_test_registry(runner, pins):
    if not pins or pins.get("tests") != test_registry_identity(runner):
        raise ValueError("The self_maintenance test registry or isolated executor changed; no test authorization was granted.")


def _check_local_image(cli, image, *, name):
    from homebrew_harness.execution.execution_backend import ContainerBackend, ContainerPolicy
    from homebrew_harness.runtime.limits import current_limits

    policy = ContainerPolicy(cli, image, ("/bin/true",), ("/bin/true",))
    backend = ContainerBackend(policy)
    timeout = current_limits().container_inspect_seconds
    try:
        if not backend.probe(timeout)["available"]:
            raise ValueError(f"self_maintenance {name} requires a reachable Linux container daemon.")
        result = backend._control([cli, "image", "inspect", image, "--format", "{{.Id}}"], timeout)
        if result.returncode != 0 or not result.stdout.strip():
            raise ValueError(f"self_maintenance {name} immutable image is not available locally; no image was pulled.")
    finally:
        backend.wait_closed(0.2)


def preflight(spec, config, control, *, test_runner=None, live=False, return_pins=False):
    """Verify declarations, and optionally check the local daemon and pinned images."""
    from .setup_validation import _external

    roots = (Path(control).resolve(), Path(spec.workspace))
    # The private configuration must not be writable by a Work, even when
    # start is called directly via the SDK instead of the offline validator.
    for key in ("env_file", "models_config", "test_config", "bash_config", "mcp_config", "control_config"):
        value = getattr(config, key)
        if value is not None:
            _external(value, roots)
    for section in (spec.delivery, spec.communication):
        if section.get("credentials_file"):
            _external(section["credentials_file"], roots)

    tools = set(spec.run_defaults["tools"])
    bash = None
    if "bash" in tools:
        if config.bash_config is None:
            raise ValueError("self_maintenance Bash needs an external preauthorized container Bash policy, or omit Bash from Run tools.")
        bash = load_bash_policy(config.bash_config, spec.workspace, mode="container")
        if not bash.preauthorized:
            raise ValueError("self_maintenance Bash requires explicit preauthorized=true in the container policy.")
        _external(bash.cli, roots)
        if not _program_available(bash.cli):
            raise ValueError("self_maintenance container Bash executable is unavailable.")

    if live:
        from homebrew_harness.execution.model_connections import HostModels, read_environment
        from .setup_validation import check_model_profiles

        models = HostModels(spec.workspace, config=config.models_config, alias=config.lead_model,
                            environment=read_environment(config.env_file, spec.workspace, private=True))
        try:
            check_model_profiles(spec, models)
        finally:
            models.close()

    owned = test_runner is None
    runner = load_test_runner(config.test_config, Path(spec.workspace)) if owned else test_runner
    try:
        names = set(spec.run_defaults["allow_tests"]) | set(spec.delivery["required_tests"])
        if runner.backend.name != "container":
            raise ValueError("self_maintenance requires an isolated container TestRunner.")
        if runner.backend.cli_identity is None:
            raise ValueError("self_maintenance container TestRunner executable is unavailable.")
        _external(runner.backend.policy.cli, roots)
        missing = names - runner.recipes.keys()
        if missing:
            raise ValueError("self_maintenance tests are not registered: " + ", ".join(sorted(missing)))
        for name in sorted(names):
            recipe = runner.recipes[name]
            if "lead" not in recipe.roles or not recipe.allow_worktrees:
                raise ValueError(f"self_maintenance test {name!r} must admit the Lead in isolated candidate/repair workspaces.")
            if name in spec.delivery["required_tests"] and not recipe.locked_globs:
                raise ValueError(f"self_maintenance required test {name!r} needs locked_globs for trusted repository test definitions.")
        if live:
            images = {(runner.backend.policy.cli, runner.backend.policy.image): "TestRunner"}
            if bash:
                images.setdefault((bash.cli, bash.image), "Bash")
            for (cli, image), name in images.items():
                _check_local_image(cli, image, name=name)
        pins = {"bash": bash.identity() if bash else None, "tests": test_registry_identity(runner)}
        if live or return_pins:
            return pins
        return {
            "preset": "self_maintenance",
            "tools": sorted(tools),
            "shell": "preauthorized_container_copy" if bash else "not_exposed",
            "shell_network": False if bash else None,
            "shell_workspace_writeback": False if bash else None,
            "test_backend": "container",
            "required_tests": sorted(spec.delivery["required_tests"]),
            "auto_compact": spec.run_spec("Inspect effective configuration").runtime_limits["auto_compact"],
            "base_update": spec.delivery["base_update"],
            "independent_review": spec.delivery["require_review"],
        }
    finally:
        if owned:
            runner.close()
