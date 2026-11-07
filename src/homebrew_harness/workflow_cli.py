"""Direct workflow entry point: orchestration needs no outer model conversation."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from homebrew_harness.execution.model_gateway import (
    ModelGateway,
    add_gateway_arguments,
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


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run and resume registered workflows.")
    add_gateway_arguments(parser)
    parser.add_argument(
        "--model-profile", help="Host model capability revision for cache identity."
    )
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path.home() / ".config" / "homebrew-harness" / ".env",
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
    args = parser.parse_args(argv)
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
            from dotenv import load_dotenv
            from openai import OpenAI

            load_dotenv(args.env_file.expanduser(), override=False)
            config = {
                name: os.getenv(name, "").strip()
                for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL")
            }
            missing = [name for name, value in config.items() if not value]
            if missing:
                raise WorkflowError("Missing configuration: " + ", ".join(missing))
            client = OpenAI(
                api_key=config["OPENAI_API_KEY"],
                base_url=config["OPENAI_BASE_URL"],
                timeout=30,
                max_retries=0,
            )
            gateway = ModelGateway(
                client, gateway_limits_from_args(args), workdir=workdir
            )
            runner = ModelWorkflowRunner(
                gateway, config["OPENAI_MODEL"], profile=args.model_profile
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
        return 0 if output["task"]["status"] == "completed" else 1
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
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
