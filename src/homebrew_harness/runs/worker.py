"""Dedicated managed Session process using external host configuration."""

import argparse
import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv

from homebrew_harness import Capabilities, HarnessSession
from homebrew_harness.cli_arguments import (
    add_environment_argument,
    add_host_config_arguments,
)
from homebrew_harness.execution.bash_policy import HOST_POLICY, load_bash_policy
from homebrew_harness.execution.model_gateway import GatewayLimits
from homebrew_harness.execution.model_connections import HostModels
from homebrew_harness.execution.test_runner import load_test_runner

from .controller import RunController
from .store import RunStore


def make_session(store, control, args):
    for path in (args.env_file, args.models_config, args.test_config, args.bash_config):
        if path is not None and path.expanduser().resolve().is_relative_to(store.root):
            raise ValueError(
                "Managed host configuration must be outside the workspace."
            )
    load_dotenv(args.env_file.expanduser(), override=False)
    from contextlib import ExitStack

    with ExitStack() as resources:
        host_models = HostModels(
            store.root,
            config=args.models_config,
            alias=control.state.get("selected_model") or control.spec.model_profile,
            environment=os.environ,
        )
        models = host_models.registry
        resources.callback(models.close)
        tests = load_test_runner(
            args.test_config.expanduser(),
            store.root,
            missing_ok=args.test_config.expanduser()
            == Path.home() / ".config/homebrew-harness/tests.json",
        )
        resources.callback(tests.close)
        bash = (
            load_bash_policy(args.bash_config, store.root)
            if args.bash_config
            else HOST_POLICY
        )
        alias = host_models.alias
        binding = host_models.open(
            context_chars=control.spec.context_chars,
            output_tokens=control.spec.output_tokens,
        )
        raw, model = binding.client, binding.model
        session = HarnessSession(
            raw,
            model,
            store.root,
            model_registry=models,
            lead_model_profile=alias,
            capabilities=Capabilities(control.spec.enabled, control.spec.memory_mode),
            run_control=control,
            test_runner=tests,
            bash_policy=bash,
            context_chars=control.spec.context_chars,
            worker_output_tokens=control.spec.output_tokens,
            gateway_limits=GatewayLimits(
                request_timeout=control.spec.request_seconds,
                max_failures=control.spec.retries + 1,
            ),
            shutdown_timeout=control.spec.shutdown_seconds,
            mode="service",
        )
        resources.pop_all()
        return session, None


def exit_status(value):
    if value["status"] == "completed" and value["cleanup_complete"]:
        return 0
    if value["status"] == "failed":
        return 1
    if value["status"] == "cancelled":
        return 130
    return 2


def main(argv=None):
    parser = argparse.ArgumentParser(description="Managed execution worker")
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--generation", type=int, required=True)
    add_environment_argument(parser, defaults=True)
    add_host_config_arguments(parser)
    args = parser.parse_args(argv)
    store = RunStore(args.cwd)
    control = RunController(store, args.run_id, args.owner, args.generation)
    session = raw = None
    cleanup = False
    construction_cleanup = True
    try:
        session, raw = make_session(store, control, args)
        outcome = control.execute()
        print(json.dumps(outcome.data(), ensure_ascii=False), flush=True)
    except Exception as error:
        construction_cleanup = not bool(
            getattr(error, "construction_cleanup_errors", None)
        )
        from .spec import RunOutcome

        control.store.update(
            control.header,
            {
                "status": "blocked",
                "last_outcome": RunOutcome(
                    control.id,
                    control.state["episode"],
                    "blocked",
                    "configuration_or_execution_failed",
                    "lead:" + control.state["session_id"],
                    required_host_actions=(type(error).__name__,),
                ).data(),
            },
        )
    finally:
        if session is None:
            cleanup = construction_cleanup
        if session is not None:
            started = time.monotonic()
            session.close()
            cleanup = (
                session.wait_closed(
                    max(0, control.spec.shutdown_seconds - (time.monotonic() - started))
                )
                and session.shutdown_status()["persistence_ok"]
            )
        if raw is not None and cleanup:
            raw.close()
        control.close_heartbeat()
        store.ended(control.header, cleanup=cleanup)
    result = exit_status(store.get(control.id))
    store.close()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
