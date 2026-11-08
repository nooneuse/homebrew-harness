"""Single public command with independently executed feature groups."""

import argparse
import importlib

from .cli_arguments import add_location_arguments, add_session_arguments


def parser():
    root = argparse.ArgumentParser(
        prog="homebrew-harness",
        description="Agent sessions, managed runs, workflows and host administration.",
        epilog="Without a subcommand, start an interactive session. Shared --cwd and --env-file options may appear before or after subcommands.",
    )
    add_location_arguments(root, defaults=True)
    commands = root.add_subparsers(dest="entrypoint", title="commands")
    session = commands.add_parser(
        "session", help="Run one task or start an interactive agent session."
    )
    add_location_arguments(session)
    add_session_arguments(session)
    session.set_defaults(
        _handler="homebrew_harness.session_cli", _command_parser=session
    )
    root._default_session = session
    root.set_defaults(_handler="homebrew_harness.session_cli", _command_parser=session)
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
        args.entrypoint = "session"
        for action in root._default_session._actions:
            if action.default != argparse.SUPPRESS and not hasattr(args, action.dest):
                setattr(args, action.dest, action.default)
    module = importlib.import_module(args._handler)
    return module.execute(args, args._command_parser)
