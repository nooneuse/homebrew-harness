"""Single public command with independently executed feature groups."""

import argparse
import importlib
from pathlib import Path

from .cli_arguments import add_location_arguments, add_session_arguments


def parser():
    root = argparse.ArgumentParser(
        prog="homebrew-harness",
        description="Agent sessions, managed runs, workflows and host administration.",
        epilog="Without a subcommand, route new tasks automatically. Shared --cwd and --env-file options may appear before or after subcommands.",
    )
    add_location_arguments(root, defaults=True)
    root.add_argument(
        "--host-config",
        type=Path,
        help="External automatic interaction settings; defaults to HARNESS_INTERACTION_CONFIG.",
    )
    root.add_argument(
        "--models-config",
        type=Path,
        help="External host model catalog for automatic interaction.",
    )
    commands = root.add_subparsers(dest="entrypoint", title="commands")
    session = commands.add_parser(
        "session", help="Run one task or start an interactive agent session."
    )
    add_location_arguments(session)
    add_session_arguments(session)
    session.set_defaults(
        _handler="homebrew_harness.session_cli", _command_parser=session
    )
    root.set_defaults(_handler="homebrew_harness.interaction_cli", _command_parser=root)
    from .admin import register as register_admin
    from .runs.cli import register as register_run
    from .workflow_cli import register as register_workflow

    register_run(commands)
    register_workflow(commands)
    register_admin(commands)
    return root


def main(argv=None):
    root = parser()
    args = root.parse_args(argv)
    if args.entrypoint is None:
        args.entrypoint = "auto"
    module = importlib.import_module(args._handler)
    return module.execute(args, args._command_parser)
