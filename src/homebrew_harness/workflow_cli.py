"""Direct workflow entry point: orchestration needs no outer model conversation."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from homebrew_harness.cli_arguments import (
    add_gateway_arguments,
    add_location_arguments,
    add_direct_model_arguments,
)


def register(groups):
    parser = groups.add_parser(
        "workflow", help="Run and resume fixed workflow scripts."
    )
    add_location_arguments(parser)
    add_gateway_arguments(parser)
    add_direct_model_arguments(parser)
    parser.add_argument(
        "--model-profile", help="Host model capability revision for cache identity."
    )
    parser.add_argument("--max-output-tokens", type=int, default=2048)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=float, default=300)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    commands.add_parser(
        "demo", help="Offline run followed by a fresh runtime resuming the journal."
    )
    run = commands.add_parser("run")
    run.add_argument("name")
    inputs = run.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--args-file", type=Path)
    inputs.add_argument("--changes-file", type=Path)
    resume = commands.add_parser("resume")
    resume.add_argument("run_id")
    resume.add_argument("--args-file", type=Path)
    resume.add_argument(
        "--demo", action="store_true", help="Resume only an offline demo run."
    )
    status = commands.add_parser("status")
    status.add_argument("run_id")
    for command in commands.choices.values():
        add_location_arguments(command)
    parser.set_defaults(_handler=__name__, _command_parser=parser)


def execute(args, parser):
    from homebrew_harness.execution.model_gateway import (
        ModelGateway,
        gateway_limits_from_args,
    )
    from homebrew_harness.orchestration.workflow_runtime import (
        ModelWorkflowRunner,
        WorkflowError,
        WorkflowLimits,
        WorkflowRuntime,
    )
    from homebrew_harness.orchestration.workflows import (
        DEMO_CHANGES,
        DemoWorkflowRunner,
        default_registry,
    )

    client = None
    try:
        registry = default_registry()
        if args.command == "list":
            print(json.dumps(registry.catalog(), ensure_ascii=False, indent=2))
            return 0
        limits = WorkflowLimits(
            concurrency=args.concurrency,
            max_output_tokens=args.max_output_tokens,
            timeout_seconds=args.timeout_seconds,
        )
        workdir = args.cwd.expanduser().resolve()
        offline = (
            args.command in {"demo", "status"} or args.command == "resume" and args.demo
        )
        if offline:
            runner = DemoWorkflowRunner()
        else:
            from homebrew_harness.execution.model_connections import (
                HostModels,
                read_environment,
            )

            from homebrew_harness.execution.providers.backends import backend_scope
            from homebrew_harness.orchestration.workflow_runtime import WorkflowStore

            saved_binding = (
                WorkflowStore(workdir).inspect(args.run_id).get("model_binding")
                if args.command == "resume"
                else None
            )
            with backend_scope(saved_binding):
                host_models = HostModels(
                    workdir,
                    config=args.models_config,
                    alias=args.model,
                    environment=read_environment(args.env_file, workdir),
                )
            binding = host_models.open(output_tokens=args.max_output_tokens)
            client = binding.client
            gateway = ModelGateway(
                client, gateway_limits_from_args(args), workdir=workdir
            )
            limits = WorkflowLimits(
                concurrency=args.concurrency,
                max_output_tokens=min(
                    args.max_output_tokens, binding.max_output_tokens
                ),
                timeout_seconds=args.timeout_seconds,
            )
            host_models.track_gateway(gateway)
            runner = ModelWorkflowRunner(
                binding, gateway=gateway, profile=args.model_profile
            )

        def event(kind, **fields):
            print(
                json.dumps({"event": kind, **fields}, ensure_ascii=False),
                file=sys.stderr,
            )

        runtime = WorkflowRuntime(
            workdir, runner, registry, limits=limits, on_event=event
        )
        if args.command == "status":
            print(
                json.dumps(runtime.inspect(args.run_id), ensure_ascii=False, indent=2)
            )
            return 0
        if args.command == "demo":
            first = runtime.run("review-changes", {"changes": DEMO_CHANGES})
            resumed = WorkflowRuntime(
                workdir, DemoWorkflowRunner(), registry, limits=limits, on_event=event
            ).run("review-changes", resume_from_run_id=first["task"]["run_id"])
            print(
                json.dumps(
                    {"mode": "offline_demo", "first": first, "resumed": resumed},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0 if resumed["task"]["status"] == "completed" else 1
        inputs = None
        if args.args_file is not None:
            inputs = json.loads(args.args_file.expanduser().read_text(encoding="utf-8"))
        if args.command == "run":
            if args.changes_file is not None:
                inputs = {
                    "changes": args.changes_file.expanduser().read_text(
                        encoding="utf-8"
                    )
                }
            output = runtime.run(args.name, inputs)
        else:
            snapshot = runtime.inspect(args.run_id)
            output = runtime.run(
                snapshot["name"],
                inputs,
                args.run_id,
                workdir=workdir / snapshot["execution_dir"],
            )
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return (
            0
            if output["task"]["status"] == "completed"
            else 2
            if output.get("execution_pending")
            else 1
        )
    except WorkflowError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print("Cannot read valid workflow inputs or configuration.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "Interrupted; inspect the saved run and resume completed checkpoints.",
            file=sys.stderr,
        )
        return 130
    except Exception as error:
        print(
            "Workflow request failed (" + type(error).__name__ + ").", file=sys.stderr
        )
        return 1
    finally:
        if "gateway" in locals():
            gateway.close()
            print(
                "[model usage] " + json.dumps(gateway.summary(), ensure_ascii=False),
                file=sys.stderr,
            )
        if "runtime" in locals():
            runtime.close()
        if "host_models" in locals():
            closed = host_models.close_when_idle(
                limits.cleanup_seconds,
                wait=(lambda: runtime.wait_closed(None))
                if "runtime" in locals()
                else None,
            )
            if not closed:
                print(
                    "Workflow shutdown remains pending; inspect its saved state before resuming.",
                    file=sys.stderr,
                )
                return 2
