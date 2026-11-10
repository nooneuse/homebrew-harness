"""Unified service commands and a dependency-free line interface."""

import argparse
import json
import shlex
import sys
import uuid
from pathlib import Path

from homebrew_harness.cli_arguments import add_location_arguments


def commands(parser, *, attach=True):
    actions = parser.add_subparsers(dest="command", required=True)
    if attach:
        start = actions.add_parser("start")
        start.add_argument("service_id", nargs="?")
        start.add_argument("--spec", type=Path)
        start.add_argument("--detach", action="store_true")
        start.add_argument("--request-id")
        start.add_argument("--host-config", type=Path)
        for name in (
            "status",
            "stop",
            "resume",
            "attach",
            "schedules",
            "tick",
            "requests",
            "inspect",
        ):
            command = actions.add_parser(name)
            command.add_argument("service_id")
            if name == "attach":
                command.add_argument("--context", default="default")
        pause = actions.add_parser("pause")
        pause.add_argument("service_id")
        pause.add_argument("--scope", choices=("all", "execution"), default="execution")
        recover = actions.add_parser("recover-launch")
        recover.add_argument("service_id")
        recover.add_argument("--token", required=True)
        receipt = actions.add_parser("receipt")
        receipt.add_argument("service_id")
        receipt.add_argument("request_id")
        work = actions.add_parser("work")
        work.add_argument("service_id")
    else:
        for name in (
            "status",
            "stop",
            "resume",
            "schedules",
            "tick",
            "requests",
            "exit",
        ):
            actions.add_parser(name)
        pause = actions.add_parser("pause")
        pause.add_argument(
            "scope", choices=("all", "execution"), nargs="?", default="execution"
        )
        receipt = actions.add_parser("receipt")
        receipt.add_argument("request_id")
        work = actions.add_parser("work")
        focus = actions.add_parser("focus")
        focus.add_argument("work_id", nargs="?")
    tasks = work.add_subparsers(dest="work_command", required=True)
    submit = tasks.add_parser("submit")
    submit.add_argument("text")
    submit.add_argument("--priority", type=int, default=0)
    submit.add_argument("--request-id")
    listing = tasks.add_parser("list")
    listing.add_argument("--state")
    listing.add_argument("--cursor", type=int, default=0)
    listing.add_argument("--limit", type=int, default=20)
    for name in ("show", "pause", "resume", "cancel"):
        tasks.add_parser(name).add_argument("work_id")
    for name in ("steer", "interrupt"):
        item = tasks.add_parser(name)
        item.add_argument("work_id")
        item.add_argument("text")
        item.add_argument("--request-id")
    for name in ("approve", "deny", "answer", "renew-tests"):
        item = actions.add_parser(name)
        if attach:
            item.add_argument("service_id")
        item.add_argument("request_id")
        item.add_argument("revision", type=int)
        item.add_argument("--work-id")
        item.add_argument("--decision-id")
        if name == "answer":
            item.add_argument("text")
        if name == "renew-tests":
            item.add_argument("--runs", type=int, required=True)
            item.add_argument("--seconds", type=float, required=True)
    return actions


def register(groups):
    parser = groups.add_parser(
        "service", help="Manage persistent services and attach a local text interface."
    )
    add_location_arguments(parser)
    parser.add_argument(
        "--connection",
        type=Path,
        help="External HTTP control connection configuration.",
    )
    actions = commands(parser)
    for action in actions.choices.values():
        add_location_arguments(action)
        action.add_argument("--connection", type=Path, default=argparse.SUPPRESS)
    parser.set_defaults(_handler=__name__, _command_parser=parser)


def render(value, *, file=None):
    if hasattr(value, "data"):
        value = value.data()
    text = json.dumps(value, ensure_ascii=False, indent=2)
    # JSON escapes control bytes from external text; do not interpret terminal markup.
    print(text, file=file)


def render_error(error, *, file=None):
    from homebrew_harness.contracts import SDKError
    from homebrew_harness.security.data_boundary import redact

    if isinstance(error, SDKError):
        render(
            redact(
                {"code": error.code, "message": str(error), "details": error.details}
            ),
            file=file,
        )
    else:
        print(redact(str(error)), file=file)


def submit(service, text, *, priority=0, request_id=None):
    request_id = request_id or "control_" + uuid.uuid4().hex
    work = service.work.submit(text, priority=priority, request_id=request_id)
    return {
        "id": work.id,
        "service_id": service.id,
        "accepted": True,
        "request_id": request_id,
    }


def operate(service, args):
    if args.command == "status":
        return service.snapshot()
    if args.command == "schedules":
        return service.schedules()
    if args.command == "tick":
        return service.tick()
    if args.command == "requests":
        return [r.data() for r in service.requests()]
    if args.command == "receipt":
        return service.receipt(args.request_id)
    if args.command in {"stop", "resume"}:
        return getattr(service, args.command)()
    if args.command == "pause":
        return service.pause(args.scope)
    if args.command == "work":
        if args.work_command == "submit":
            return submit(
                service, args.text, priority=args.priority, request_id=args.request_id
            )
        if args.work_command == "list":
            return service.work.list(
                state=args.state, cursor=args.cursor, limit=args.limit
            )
        work = service.work.get(args.work_id)
        if args.work_command == "show":
            return work.snapshot()
        if args.work_command in {"steer", "interrupt"}:
            return getattr(work, args.work_command)(
                args.text, request_id=args.request_id
            )
        return getattr(work, args.work_command)()
    if args.command in {"approve", "deny", "answer", "renew-tests"}:
        request = next(
            (
                r
                for r in service.requests(args.work_id)
                if r.ref.id == args.request_id and r.ref.revision == args.revision
            ),
            None,
        )
        if request is None:
            raise ValueError("Request revision is stale; refresh requests.")
        values = {}
        if args.command == "answer":
            values["text"] = args.text
        if args.command == "renew-tests":
            values.update(runs=args.runs, seconds=args.seconds)
        return service.decide(
            request.ref,
            args.command.replace("-", "_"),
            decision_id=args.decision_id,
            **values,
        )
    raise ValueError("Unsupported service command.")


class LineParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)

    def exit(self, status=0, message=None):
        if status:
            raise ValueError(message or "Invalid command.")
        raise StopIteration


def attach(service, *, input_fn=input, context="default"):
    parser = LineParser(
        prog="service>",
        description="Local service controls; exit disconnects without stopping work.",
    )
    actions = commands(parser, attach=False)
    print(
        "Attached to "
        + service.id
        + ". Use status, work, requests or help. exit disconnects; stop closes the service."
    )
    render(service.snapshot())
    focus = service.current_focus(context=context)
    if focus and focus["work_id"]:
        render({"focus": focus})
    while True:
        try:
            line = input_fn("service> ").strip()
        except EOFError:
            break
        except KeyboardInterrupt:
            print("\nInput cancelled.")
            continue
        if not line:
            continue
        if line in {"help", "--help"}:
            parser.print_help()
            continue
        if line == "exit":
            break
        try:
            tokens = shlex.split(line)
            if not tokens:
                continue
            if tokens[0] not in actions.choices:
                if focus and focus["work_id"]:
                    render(
                        service.work.get(focus["work_id"]).steer(
                            line, revision=focus["revision"]
                        )
                    )
                    focus = service.focus(focus["work_id"], context=context)
                else:
                    render(submit(service, line))
            else:
                args = parser.parse_args(tokens)
                if args.command == "focus":
                    focus = service.focus(args.work_id, context=context)
                    render({"focus": focus})
                elif args.command == "requests" and focus and focus["work_id"]:
                    render([r.data() for r in service.requests(focus["work_id"])])
                else:
                    render(operate(service, args))
        except StopIteration:
            continue
        except KeyboardInterrupt:
            print(
                "\nObservation cancelled; inspect the accepted request before repeating it."
            )
        except Exception as error:
            render_error(error)


def execute(args, parser):
    core = None
    remote = None
    try:
        connection = getattr(args, "connection", None)
        if args.command in {"start", "recover-launch", "inspect"}:
            if connection is not None:
                raise ValueError("This operation requires the local service host.")
            from homebrew_harness.api import Harness, HostConfig
            from .spec import ServiceSpec
            from .store import ServiceStore
            from .runtime import saved_config

            control = args.cwd.expanduser()
            if args.command == "start":
                control.mkdir(parents=True, exist_ok=True, mode=0o700)
                if bool(args.spec) == bool(args.service_id):
                    raise ValueError("Use a service reference or --spec, exclusively.")
                config = (
                    saved_config(ServiceStore(control).get(args.service_id)["config"])
                    if args.service_id
                    else HostConfig.from_file(args.host_config, workdir=control)
                    if args.host_config
                    else HostConfig.from_files(env_file=args.env_file)
                )
                core = Harness(workdir=control, config=config)
                spec = ServiceSpec.load(args.spec, control) if args.spec else None
                service = core.services.start(
                    spec,
                    identity=args.service_id,
                    lifetime="detached" if args.detach else "attached",
                    request_id=args.request_id,
                )
                render(service.snapshot())
                if not args.detach:
                    try:
                        while not service.wait_closed(0.25):
                            pass
                    except KeyboardInterrupt:
                        service.stop()
                return 0
            core = Harness(workdir=control)
            if args.command == "recover-launch":
                render(core.services.recover_launch(args.service_id, args.token))
            else:
                render(core.services.get(args.service_id).snapshot())
            return 0
        from .remote import RemoteService

        remote = (
            RemoteService.from_file(connection, control=True)
            if connection is not None
            else RemoteService.from_root(args.cwd.expanduser(), control=True)
        )
        if remote.id != args.service_id:
            raise ValueError("Connection configuration targets another service.")
        if args.command == "attach":
            if not sys.stdin.isatty():
                raise ValueError(
                    "attach requires a terminal; use individual commands for JSON output."
                )
            attach(remote, context=args.context)
        else:
            render(operate(remote, args))
        return 0
    except Exception as error:
        render_error(error, file=sys.stderr)
        return 1
    finally:
        if remote is not None:
            remote.close()
        if core is not None:
            core.close()
