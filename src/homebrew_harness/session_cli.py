"""Session command execution and interactive input routing."""

from __future__ import annotations

from homebrew_harness.execution.model_connections import HostModels

import json
import math
import os
import sys
from contextlib import ExitStack
from pathlib import Path

from dotenv import load_dotenv

import homebrew_harness.configuration
import homebrew_harness.runtime.assembly
import homebrew_harness.runtime.errors
import homebrew_harness.tools.catalog
from homebrew_harness.context.compaction import CompactionError
from homebrew_harness.context.memory import MemoryError
from homebrew_harness.execution.background import termination_guard
from homebrew_harness.execution.bash_policy import HOST_POLICY, load_bash_policy
from homebrew_harness.execution.model_gateway import (
    ModelGatewayError,
    gateway_limits_from_args,
)
from homebrew_harness.execution.test_runner import load_test_runner
from homebrew_harness.orchestration.cron_scheduler import CronError
from homebrew_harness.orchestration.goal_loop import GoalError
from homebrew_harness.orchestration.workflow_runtime import (
    ModelWorkflowRunner,
    WorkflowRuntime,
)
from homebrew_harness.runtime.capabilities import from_args as capabilities_from_args
from homebrew_harness.runtime.hooks import HookError
from homebrew_harness.security.data_boundary import redact
from homebrew_harness.tools.mcp.runtime import MCPError, MCPRuntime, load_specs


def host_exit_status(session, status=0):
    session.close()
    shutdown = session.shutdown_status()
    if shutdown["state"] == "error":
        return 1
    if shutdown["state"] == "pending" and status == 0:
        return 2
    return status


def execute(args, parser) -> int:
    from homebrew_harness.runtime.session import HarnessSession

    load_dotenv(args.env_file.expanduser(), override=False)
    selected = capabilities_from_args(args)
    if args.demo_workflows:
        selected = selected.include("workflow")
    workdir = args.cwd.expanduser().resolve()
    if not workdir.is_dir():
        parser.error("--cwd must be an existing directory")
    if not all(
        math.isfinite(value) and value >= 0
        for value in (args.wait_timeout, args.shutdown_timeout)
    ):
        parser.error("wait and shutdown timeouts must be nonnegative")
    if args.max_turns < 1:
        parser.error("--max-turns must be positive")
    if args.context_chars < 10_000:
        parser.error("--context-chars must be at least 10000")
    skills_dir = (
        args.skills_dir.expanduser().resolve()
        if args.skills_dir is not None
        else workdir / "skills"
    )
    if args.skills_dir is not None and not skills_dir.is_dir():
        parser.error("--skills-dir must be an existing directory")

    from homebrew_harness.execution.providers.backends import (
        backend_scope,
        session_backend_bindings,
    )

    try:
        with backend_scope(session_backend_bindings(workdir, args.resume_session)):
            host_models = HostModels(
                workdir,
                config=args.models_config,
                alias=args.lead_model,
                environment=os.environ,
            )
    except ValueError as error:
        print(redact(str(error)), file=sys.stderr)
        return 1
    models, lead_alias = host_models.registry, host_models.alias
    effective_model = (
        models.specs[lead_alias].model
        if lead_alias
        else host_models.environment["OPENAI_MODEL"]
    )
    skill_loader = None
    if args.skills_dir is not None:
        try:
            skill_loader = homebrew_harness.runtime.assembly.SkillLoader(
                skills_dir, include_builtin=True
            )
        except (OSError, ValueError, RuntimeError):
            parser.error("Cannot scan the configured skills directory")

    try:
        from homebrew_harness.orchestration.goal_input import load_goal_input

        goal_spec = (
            load_goal_input(args.goal_spec) if args.goal_spec is not None else None
        )
        prompt = (
            args.prompt
            if args.prompt is not None
            else goal_spec["condition"]
            if goal_spec
            else None
        )
        with ExitStack() as host_resources:
            if args.bash_mode == "host":
                bash_policy = HOST_POLICY
            elif args.bash_config is not None:
                bash_policy = load_bash_policy(
                    args.bash_config, workdir, mode=args.bash_mode
                )
            elif args.bash_mode == "container":
                raise ValueError(
                    "Container Bash requires --bash-config; no host fallback is allowed."
                )
            else:
                bash_policy = HOST_POLICY
            test_path = args.test_config.expanduser()
            test_runner = load_test_runner(
                test_path,
                workdir,
                missing_ok=test_path
                == Path.home() / ".config" / "homebrew-harness" / "tests.json",
            )
            host_resources.callback(test_runner.close)
            for name in args.allow_tests:
                test_runner.approve(name)
            mcp_path = args.mcp_config.expanduser().resolve()
            specs = (
                load_specs(
                    mcp_path,
                    missing_ok=mcp_path
                    == (
                        Path.home() / ".config" / "homebrew-harness" / "mcp.json"
                    ).resolve(),
                )
                if selected.has("mcp")
                else {}
            )
            binding = host_models.open(
                context_chars=args.context_chars,
                output_tokens=args.worker_output_tokens or 4096,
            )
            client, effective_model = binding.client, binding.model
            with termination_guard():
                mcp_runtime = (
                    MCPRuntime(
                        specs,
                        workdir=workdir,
                        reserved_names=homebrew_harness.tools.catalog.TOOL_SCHEMAS,
                    )
                    if selected.has("mcp")
                    else None
                )
                if mcp_runtime is not None:
                    host_resources.callback(mcp_runtime.close)
                workflow_runtime = None
                if args.demo_workflows:
                    from homebrew_harness.orchestration.workflows import (
                        default_registry,
                    )

                    workflow_runtime = WorkflowRuntime(
                        workdir,
                        ModelWorkflowRunner(binding, profile=args.model_profile),
                        default_registry(),
                    )
                    host_resources.callback(workflow_runtime.close)
                with HarnessSession(
                    client,
                    effective_model,
                    workdir,
                    skill_loader=skill_loader,
                    workflow_runtime=workflow_runtime,
                    mcp_runtime=mcp_runtime,
                    max_turns=args.max_turns,
                    context_chars=args.context_chars,
                    stop_block_cap=args.stop_block_cap,
                    goal_evaluator_model=args.goal_evaluator_model
                    or os.getenv("GOAL_EVALUATOR_MODEL")
                    or effective_model,
                    goal_check_tokens=args.goal_check_tokens,
                    goal_input_chars=args.goal_input_chars,
                    mode="one_shot" if prompt is not None else "interactive",
                    shutdown_timeout=args.shutdown_timeout,
                    gateway_limits=gateway_limits_from_args(args),
                    test_runner=test_runner,
                    resume_session=args.resume_session,
                    turn_timeout=args.turn_timeout,
                    model_profile=args.model_profile,
                    worker_output_tokens=args.worker_output_tokens,
                    capabilities=selected,
                    bash_policy=bash_policy,
                    model_registry=models,
                    lead_model_profile=lead_alias,
                ) as session:
                    if prompt is not None:
                        print(
                            session.run_user(prompt)
                            if goal_spec is None
                            else session.run_user(prompt, goal_spec=goal_spec)
                        )
                        status = session.drain(timeout=args.wait_timeout)
                        if status:
                            print(
                                "[runtime] "
                                + json.dumps(
                                    session.exit_report(status), ensure_ascii=False
                                ),
                                file=sys.stderr,
                            )
                            print(
                                "Accepted work failed or remains pending; running commands are cancelled during shutdown.",
                                file=sys.stderr,
                            )
                        return host_exit_status(session, status)
                    print(
                        "homebrew-harness — enter a task or /goal condition; q, quit, or exit to leave."
                    )
                    from homebrew_harness.runtime.input_router import (
                        ApprovalBroker,
                        approval_scope,
                    )

                    broker = ApprovalBroker(trusted=sys.stdin.isatty(), owner=session)
                    with approval_scope(broker):
                        session.start()
                        try:
                            while True:
                                try:
                                    query = input("agent> ").strip()
                                except EOFError:
                                    return host_exit_status(session)
                                if query.lower() in {"q", "quit", "exit"}:
                                    return host_exit_status(session)
                                if not query:
                                    continue
                                try:
                                    if query.startswith("/approve "):
                                        parts = query.split()
                                        if len(parts) != 3 or parts[2] not in {
                                            "yes",
                                            "no",
                                        }:
                                            raise ValueError(
                                                "Use /approve ID yes or /approve ID no."
                                            )
                                        broker.respond(parts[1], parts[2] == "yes")
                                    elif query == "/inputs" or query.startswith(
                                        "/inputs "
                                    ):
                                        print(session.run_user(query))
                                    else:
                                        mode = (
                                            "queued"
                                            if query.startswith("/")
                                            else "steer"
                                        )
                                        target = "lead"
                                        if query.startswith("/interrupt "):
                                            mode, query = (
                                                "interrupt",
                                                query[len("/interrupt ") :],
                                            )
                                        elif query.startswith("/queue "):
                                            mode, query = (
                                                "queued",
                                                query[len("/queue ") :],
                                            )
                                        elif query.startswith("/steer "):
                                            mode, query = (
                                                "steer",
                                                query[len("/steer ") :],
                                            )
                                        elif query.startswith("/steer-to "):
                                            _, target, query = query.split(None, 2)
                                            mode = "steer"
                                        print(
                                            json.dumps(
                                                session.submit_user_input(
                                                    query, mode=mode, target=target
                                                ),
                                                ensure_ascii=False,
                                            )
                                        )
                                except (ValueError, OSError) as error:
                                    print(redact(str(error)), file=sys.stderr)
                        finally:
                            broker.close()
    except (
        homebrew_harness.runtime.errors.AgentError,
        HookError,
        CompactionError,
        CronError,
        MCPError,
        GoalError,
        MemoryError,
        ModelGatewayError,
        ValueError,
    ) as error:
        print(redact(str(error)), file=sys.stderr)
        return 1
    except Exception as error:
        print(
            "Session initialization/request failed (" + type(error).__name__ + ").",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130
    finally:
        if "session" not in locals():
            models.close()
        if "session" in locals():
            print(
                "[model usage] "
                + json.dumps(session.gateway.summary(), ensure_ascii=False),
                file=sys.stderr,
            )
            if session._closed and session.shutdown_status()["state"] in {
                "pending",
                "error",
            }:
                print(
                    "[shutdown] " + json.dumps(session.shutdown_status()),
                    file=sys.stderr,
                )
