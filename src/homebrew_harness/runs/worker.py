"""Dedicated managed Session process using external host configuration."""

import argparse
import json
import time
from pathlib import Path


from homebrew_harness.cli_arguments import (
    add_environment_argument,
    add_host_config_arguments,
)
from homebrew_harness.execution.model_gateway import GatewayLimits

from .controller import RunController
from .store import RunStore


def make_session(store, control, args):
    from homebrew_harness.runtime.interaction_config import (
        InteractionConfig,
        InteractionAssembler,
    )
    from homebrew_harness.runtime.task_routing import TaskRoute

    spec = control.spec
    selection = getattr(args, "model_selection", None)
    if selection and control.state.get("selected_model") and control.state["selected_model"] != selection.get("profile"):
        selection = None
    config = InteractionConfig(
        env_file=args.env_file,
        models_config=args.models_config,
        model_selection=selection,
        test_config=args.test_config,
        bash_config=args.bash_config,
        mcp_config=args.mcp_config,
        lead_model=control.state.get("selected_model") or spec.model_profile,
        allowed=spec.enabled,
        memory_mode=spec.memory_mode,
        allow_tests=spec.allow_tests,
        write_paths=spec.write_paths,
        mcp_delegation=spec.mcp_delegation,
        demo_workflows="workflow" in spec.enabled,
        total_seconds=spec.total_seconds,
        turn_seconds=spec.episode_seconds,
        request_seconds=spec.request_seconds,
        shutdown_seconds=spec.shutdown_seconds,
        context_chars=spec.context_chars,
        output_tokens=spec.output_tokens,
        max_turns=spec.episode_model_calls,
    )
    from homebrew_harness.execution.providers.backends import session_backend_bindings

    builder = InteractionAssembler(
        store.root,
        config,
        model_bindings=[
            control.state.get("expected_binding"),
            session_backend_bindings(store.root, control.state["session_id"]),
        ],
    )
    try:
        session = builder.session(
            TaskRoute(
                execution_mode="managed", capabilities=spec.enabled, mcp=spec.mcp
            ),
            run_control=control,
            mode="service",
            gateway_limits=GatewayLimits(
                request_timeout=spec.request_seconds, max_failures=spec.retries + 1
            ),
        )
        return session, None
    finally:
        builder.close()


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
