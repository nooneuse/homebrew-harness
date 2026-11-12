"""Argument definitions shared by the public CLI and its private worker."""

import argparse
from pathlib import Path

from homebrew_harness.runtime.capabilities import (
    add_arguments as add_capability_arguments,
)


def add_location_arguments(parser, *, defaults=False):
    parser.add_argument(
        "--cwd",
        type=Path,
        default=Path.cwd() if defaults else argparse.SUPPRESS,
        help="Working directory for tools and persisted state (default: current directory).",
    )
    add_environment_argument(parser, defaults=defaults)


def add_environment_argument(parser, *, defaults=False):
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path.home() / ".config/homebrew-harness/.env"
        if defaults
        else argparse.SUPPRESS,
        help="External dotenv configuration; exported variables take precedence.",
    )


def add_host_config_arguments(parser):
    parser.add_argument("--model-selection", type=__import__("json").loads, help=argparse.SUPPRESS)
    parser.add_argument(
        "--models-config",
        type=Path,
        help="External host model registry JSON; aliases select models for Lead, teammates and subagents.",
    )
    parser.add_argument(
        "--test-config",
        type=Path,
        default=Path.home() / ".config/homebrew-harness/tests.json",
        help="Host test registry outside the workspace; absent default registers unittest when tests/ exists.",
    )
    parser.add_argument(
        "--bash-config",
        type=Path,
        help="Bash policy JSON outside the workspace; container mode requires a host-approved image.",
    )

    parser.add_argument(
        "--mcp-config",
        type=Path,
        default=Path.home() / ".config" / "homebrew-harness" / "mcp.json",
        help="External MCP server/permission configuration; absent default means no registered servers.",
    )


def add_lead_model_argument(parser):
    parser.add_argument(
        "--lead-model",
        help="Lead model alias from --models-config; otherwise its configured default or OPENAI_MODEL.",
    )


def add_gateway_arguments(parser):
    descriptions = {
        "model_concurrency": "Maximum in-flight model requests (default: 4).",
        "model_request_timeout": "Absolute model queue/request deadline in seconds (default: 60).",
        "model_session_timeout": "Optional absolute session duration, including idle time, in seconds.",
    }
    for name, converter in (
        ("model_concurrency", int),
        ("model_request_timeout", float),
        ("model_session_timeout", float),
    ):
        parser.add_argument(
            "--" + name.replace("_", "-"),
            type=converter,
            default=None,
            help=descriptions[name],
        )


def add_session_arguments(parser):
    add_gateway_arguments(parser)
    add_host_config_arguments(parser)
    add_lead_model_argument(parser)
    add_capability_arguments(parser)
    parser.add_argument(
        "--bash-mode",
        choices=("host", "container"),
        help="Host-selected Bash target; defaults to host when no Bash configuration is supplied.",
    )
    parser.add_argument(
        "--demo-workflows",
        action="store_true",
        help="Explicitly register the sample review-changes workflow.",
    )
    parser.add_argument(
        "--worker-output-tokens",
        type=int,
        help="Host worker output/reasoning allowance; default HARNESS_WORKER_OUTPUT_TOKENS or 4096.",
    )
    parser.add_argument(
        "--model-profile",
        help="Host capability revision for cache identity; external HARNESS_MODEL_PROFILE is the default.",
    )
    parser.add_argument(
        "prompt", nargs="?", help="Run one task; omit for an interactive session."
    )
    parser.add_argument(
        "--skills-dir",
        type=Path,
        help="Skill source directory (default: skills/ inside --cwd).",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=20,
        help="Maximum parent model calls per request (default: 20); each subagent allows up to 30.",
    )
    parser.add_argument(
        "--context-chars",
        type=int,
        default=50_000,
        help="Approximate message and tool-schema character budget (default: 50000, minimum: 10000).",
    )
    parser.add_argument(
        "--turn-timeout",
        type=float,
        default=300,
        help="Absolute deadline for a user/event turn including child tools (default: 300 seconds).",
    )
    parser.add_argument(
        "--resume-session",
        nargs="?",
        const="latest",
        help="Restore an explicit saved Lead session; defaults to the latest checkpoint. Uncertain actions require host inspection.",
    )
    parser.add_argument(
        "--wait-timeout",
        type=float,
        default=120,
        help="One-shot async drain deadline in seconds (default: 120).",
    )
    parser.add_argument(
        "--shutdown-timeout",
        type=float,
        default=5,
        help="Bounded local shutdown wait in seconds (default: 5).",
    )
    parser.add_argument(
        "--allow-tests",
        action="append",
        default=[],
        metavar="NAME",
        help="Host preauthorization for one registered recipe, bounded by its roles, inputs and limits.",
    )
    parser.add_argument(
        "--goal-spec",
        type=Path,
        help="Explicit structured goal JSON; ordinary text /goal remains advisory.",
    )
    parser.add_argument(
        "--goal-evaluator-model",
        help="Optional independent checker model; otherwise external GOAL_EVALUATOR_MODEL or worker model.",
    )
    parser.add_argument(
        "--goal-check-tokens",
        type=int,
        default=2048,
        help="Goal checker output limit (default: 2048).",
    )
    parser.add_argument(
        "--goal-input-chars",
        type=int,
        default=12000,
        help="Maximum Goal checker data characters (default: 12000).",
    )
    parser.add_argument(
        "--stop-block-cap",
        type=int,
        default=8,
        help="Maximum consecutive Stop continuations (default: 8).",
    )


def add_direct_model_arguments(parser):
    parser.add_argument(
        "--models-config",
        type=Path,
        help="External model registry; connection values remain in the host environment.",
    )
    parser.add_argument(
        "--model",
        help="Host model profile alias; otherwise the catalog default or single-model environment.",
    )
