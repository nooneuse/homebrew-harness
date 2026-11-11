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
        validate = actions.add_parser("validate", help="Check external service configuration without starting it")
        validate.add_argument("--spec", type=Path, required=True)
        validate.add_argument("--host-config", type=Path, required=True)
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
    requests = actions.choices["requests"]
    requests.add_argument("--cursor", type=int, default=0)
    requests.add_argument("--limit", type=int)
    requests.add_argument("--expired", action="store_true")
    if not attach:
        pause = actions.add_parser("pause")
        pause.add_argument(
            "scope", choices=("all", "execution"), nargs="?", default="execution"
        )
        receipt = actions.add_parser("receipt")
        receipt.add_argument("request_id")
        work = actions.add_parser("work")
        focus = actions.add_parser("focus")
        focus.add_argument("work_id", nargs="?")
    for name in (
        "records",
        "record",
        "chat",
        "message",
        "report",
        "report-ack",
        "job",
        "job-retry",
        "reports",
    ):
        item = actions.add_parser(name)
        if attach:
            item.add_argument("service_id")
        if name == "records":
            item.add_argument(
                "kind",
                choices=(
                    "events",
                    "evidence",
                    "sources",
                    "jobs",
                    "decisions",
                    "messages",
                    "reports",
                    "changesets",
                    "actions",
                    "batches",
                    "audits",
                    "incidents",
                    "operations",
                    "upgrade_plans",
                ),
            )
            item.add_argument("--cursor", type=int, default=0)
            item.add_argument("--limit", type=int, default=20)
            item.add_argument("--state")
        elif name == "record":
            item.add_argument(
                "kind",
                choices=(
                    "work",
                    "events",
                    "evidence",
                    "sources",
                    "jobs",
                    "decisions",
                    "messages",
                    "reports",
                    "changesets",
                    "actions",
                    "batches",
                    "audits",
                    "incidents",
                    "operations",
                    "upgrade_plans",
                ),
            )
            item.add_argument("identity")
            item.add_argument("--offset", type=int, default=0)
            item.add_argument("--limit", type=int, default=4000)
        elif name == "chat":
            item.add_argument("text")
            item.add_argument("--context", default="default")
            item.add_argument("--work-id")
            item.add_argument("--revision", type=int)
            item.add_argument("--request-id")
        elif name in {"message", "job", "job-retry", "report-ack"}:
            item.add_argument("identity")
            if name == "job-retry":
                item.add_argument("--request-id")
            if name == "report-ack":
                item.add_argument("--context", default="default")
        elif name == "reports":
            item.add_argument("--context", default="default")
            item.add_argument("--cursor", type=int, default=0)
            item.add_argument("--limit", type=int, default=20)
        elif name == "report":
            item.add_argument("identity", nargs="?")
    ops = actions.add_parser("ops")
    if attach:
        ops.add_argument("service_id")
    op_actions = ops.add_subparsers(dest="ops_command", required=True)
    for name in (
        "health",
        "list",
        "show",
        "read",
        "park",
        "resume",
        "backup",
        "verify-backup",
        "restore-backup",
        "retention-preview",
        "retention-apply",
        "audit",
        "diagnose",
        "retry",
        "upgrade-prepare",
        "upgrade-arm",
        "upgrade-confirm",
    ):
        item = op_actions.add_parser(name)
        if name in {
            "show",
            "read",
            "verify-backup",
            "restore-backup",
            "retention-apply",
            "diagnose",
            "retry",
            "upgrade-arm",
            "upgrade-confirm",
        }:
            item.add_argument("identity")
        if name == "upgrade-prepare":
            item.add_argument("alias")
        if name in {"read", "list"}:
            item.add_argument(
                "--offset" if name == "read" else "--cursor", type=int, default=0
            )
            item.add_argument(
                "--limit", type=int, default=4000 if name == "read" else 20
            )
        if name == "audit":
            item.add_argument("--force", action="store_true")
        if name not in {"health", "list", "show", "read"}:
            item.add_argument("--request-id")
    if attach:
        item = op_actions.add_parser("upgrade-rollback")
        item.add_argument("identity")
        item.add_argument("--request-id", required=True)
    models = actions.add_parser("models")
    if attach:
        models.add_argument("service_id")
    model_ops = models.add_subparsers(dest="model_command", required=True)
    model_ops.add_parser("list")
    recovering = model_ops.add_parser("recover")
    recovering.add_argument("profile")
    recovering.add_argument("--revision", required=True)
    recovering.add_argument("--request-id")
    discussion = actions.add_parser("discussion")
    if attach:
        discussion.add_argument("service_id")
    discussion_ops = discussion.add_subparsers(dest="discussion_command", required=True)
    listing = discussion_ops.add_parser("list")
    listing.add_argument("--cursor", type=int, default=0)
    listing.add_argument("--limit", type=int, default=20)
    listing.add_argument("--state")
    for name in ("show", "events", "assess", "pause", "reopen", "prepare-review"):
        item = discussion_ops.add_parser(name)
        item.add_argument("identity")
        item.add_argument("--request-id")
    publication = actions.add_parser("communication")
    if attach:
        publication.add_argument("service_id")
    publication_ops = publication.add_subparsers(
        dest="communication_command", required=True
    )
    listing = publication_ops.add_parser("list")
    listing.add_argument("--cursor", type=int, default=0)
    listing.add_argument("--limit", type=int, default=20)
    listing.add_argument("--state")
    for name in ("show", "inspect", "request-authority"):
        item = publication_ops.add_parser(name)
        item.add_argument("identity")
        item.add_argument("--request-id")
        if name == "inspect":
            item.add_argument(
                "--conclusion",
                choices=("reconcile", "retry_not_executed"),
                default="reconcile",
            )
            item.add_argument("--confirm-not-executed", action="store_true")
            item.add_argument("--revision", type=int)
    item = publication_ops.add_parser("plan")
    item.add_argument("identity")
    item.add_argument("--plan-file", type=Path, required=True)
    item.add_argument("--request-id")

    delivery = actions.add_parser("delivery")
    if attach:
        delivery.add_argument("service_id")
    delivery_ops = delivery.add_subparsers(dest="delivery_command", required=True)
    listing = delivery_ops.add_parser("list")
    listing.add_argument("--cursor", type=int, default=0)
    listing.add_argument("--limit", type=int, default=20)
    listing.add_argument("--state")
    for name in ("show", "action", "prepare", "request", "refresh"):
        item = delivery_ops.add_parser(name)
        item.add_argument("identity")
        item.add_argument("--request-id")
    plan = delivery_ops.add_parser("plan")
    plan.add_argument("identity")
    plan.add_argument(
        "kind",
        choices=(
            "publish_branch",
            "ensure_change_request",
            "merge_change_request",
            "post_comment",
        ),
    )
    plan.add_argument("--body")
    plan.add_argument("--subject-file", type=Path)
    plan.add_argument("--request-id")
    inspect = delivery_ops.add_parser("inspect")
    inspect.add_argument("identity")
    inspect.add_argument(
        "--conclusion", choices=("reconcile", "retry_not_executed"), default="reconcile"
    )
    inspect.add_argument("--confirmed", action="store_true")
    inspect.add_argument("--request-id")
    batch = actions.add_parser("batch")
    if attach:
        batch.add_argument("service_id")
    batch_ops = batch.add_subparsers(dest="batch_command", required=True)
    create = batch_ops.add_parser("create")
    create.add_argument("--ids", nargs="+")
    create.add_argument("--request-id")
    show = batch_ops.add_parser("show")
    show.add_argument("identity")
    decide = batch_ops.add_parser("decide")
    decide.add_argument("identity")
    decide.add_argument("--approve", nargs="*", default=[])
    decide.add_argument("--deny", nargs="*", default=[])
    decide.add_argument("--revision", type=int, default=0)
    decide.add_argument("--request-id")
    tasks = work.add_subparsers(dest="work_command", required=True)
    submit = tasks.add_parser("submit")
    submit.add_argument("text")
    submit.add_argument("--priority", type=int, default=0)
    submit.add_argument("--request-id")
    listing = tasks.add_parser("list")
    listing.add_argument("--state")
    listing.add_argument("--cursor", type=int, default=0)
    listing.add_argument("--limit", type=int, default=20)
    for name in ("show", "pause", "resume", "cancel", "revoke", "request-authority"):
        tasks.add_parser(name).add_argument("work_id")
    for name in ("steer", "interrupt", "revise"):
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
    if args.command == "ops":
        op = args.ops_command
        if op == "health":
            return service.health()
        if op == "list":
            return service.operations(cursor=args.cursor, limit=args.limit)
        if op == "show":
            return service.operation(args.identity)
        if op == "read":
            return service.operation_record(
                args.identity, offset=args.offset, limit=args.limit
            )
        methods = {
            "park": "quiesce",
            "resume": "resume_operations",
            "backup": "backup",
            "verify-backup": "verify_backup",
            "restore-backup": "restore_backup",
            "retention-preview": "retention_preview",
            "retention-apply": "retention_apply",
            "audit": "audit",
            "diagnose": "diagnose",
            "retry": "retry_operation",
            "upgrade-prepare": "prepare_upgrade",
            "upgrade-arm": "arm_upgrade",
            "upgrade-confirm": "confirm_upgrade",
        }
        values = {"request_id": args.request_id}
        if op == "audit":
            values["force"] = args.force
        arguments = (
            (args.alias,)
            if op == "upgrade-prepare"
            else (args.identity,)
            if hasattr(args, "identity")
            else ()
        )
        return getattr(service, methods[op])(*arguments, **values)
    if args.command == "delivery":
        op = args.delivery_command
        if op == "list":
            return service.records(
                "changesets", cursor=args.cursor, limit=args.limit, state=args.state
            )
        if op in {"show", "action"}:
            return service.delivery(
                args.identity, kind="changesets" if op == "show" else "actions"
            )
        if op == "prepare":
            return service.prepare_delivery(args.identity, request_id=args.request_id)
        if op == "plan":
            return service.plan_delivery(
                args.identity,
                args.kind,
                body=args.body,
                subject=json.loads(args.subject_file.read_text())
                if args.subject_file
                else None,
                request_id=args.request_id,
            )
        if op == "inspect":
            return service.inspect_delivery(
                args.identity,
                conclusion=args.conclusion,
                confirmed=args.confirmed,
                request_id=args.request_id,
            )
        return getattr(
            service, "request_delivery" if op == "request" else "refresh_delivery"
        )(args.identity, request_id=args.request_id)
    if args.command == "batch":
        if args.batch_command == "create":
            return service.batch(
                [r for r in service.requests() if not args.ids or r.ref.id in args.ids],
                request_id=args.request_id,
            )
        if args.batch_command == "show":
            return service.delivery(args.identity, kind="batches")
        return service.decide_batch(
            args.identity,
            [{"id": i, "decision": "approve"} for i in args.approve]
            + [{"id": i, "decision": "deny"} for i in args.deny],
            revision=args.revision,
            request_id=args.request_id,
        )
    if args.command == "reports":
        return service.reports(
            context=args.context, cursor=args.cursor, limit=args.limit
        )
    if args.command == "records":
        return service.records(
            args.kind, cursor=args.cursor, limit=args.limit, state=args.state
        )
    if args.command == "record":
        return service.record(
            args.kind, args.identity, offset=args.offset, limit=args.limit
        )
    if args.command == "discussion":
        if args.discussion_command == "list":
            return service.discussions(
                cursor=args.cursor, limit=args.limit, state=args.state
            )
        if args.discussion_command == "show":
            return service.discussion(args.identity)
        if args.discussion_command == "events":
            return service.discussion_events(args.identity)
        return service.control_discussion(
            args.identity,
            args.discussion_command.replace("-", "_"),
            request_id=args.request_id,
        )
    if args.command == "models":
        if args.model_command == "list":
            return service.models()
        return service.recover_model(
            args.profile, revision=args.revision, request_id=args.request_id
        )
    if args.command == "communication":
        if args.communication_command == "list":
            return service.communication_actions(
                cursor=args.cursor, limit=args.limit, state=args.state
            )
        if args.communication_command == "show":
            return service.communication(args.identity)
        if args.communication_command == "inspect":
            return service.inspect_communication(
                args.identity,
                request_id=args.request_id,
                conclusion=args.conclusion,
                confirmed=args.confirm_not_executed,
                revision=args.revision,
            )
        if args.communication_command == "request-authority":
            return service.request_communication_authority(
                args.identity, request_id=args.request_id
            )
        return service.plan_communication(
            args.identity,
            json.loads(args.plan_file.read_text()),
            request_id=args.request_id,
        )
    if args.command == "chat":
        return service.converse(
            args.text,
            context=args.context,
            work_id=args.work_id,
            revision=args.revision,
            request_id=args.request_id,
        )
    if args.command == "message":
        return service.conversation(args.identity)
    if args.command == "job-retry":
        return service.retry_job(args.identity, request_id=args.request_id)
    if args.command == "job":
        return service.job(args.identity)
    if args.command == "report":
        return service.report(args.identity)
    if args.command == "report-ack":
        return service.acknowledge_report(args.identity, context=args.context)
    if args.command == "status":
        return service.snapshot()
    if args.command == "schedules":
        return service.schedules()
    if args.command == "tick":
        return service.tick()
    if args.command == "requests":
        if args.limit is not None or args.cursor or args.expired:
            page = service.requests_page(
                cursor=args.cursor, limit=args.limit or 20, expired=args.expired
            )
            return {**page, "items": [r.data() for r in page["items"]]}
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
        if args.work_command in {"steer", "interrupt", "revise"}:
            return getattr(work, args.work_command)(
                args.text, request_id=args.request_id
            )
        return getattr(work, args.work_command.replace("-", "_"))()
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
            if service.snapshot().details.get("profile") == "repository_maintenance":
                cursor = 0
                while request is None:
                    page = service.requests_page(
                        work_id=args.work_id, cursor=cursor, limit=100
                    )
                    request = next(
                        (
                            r
                            for r in page["items"]
                            if r.ref.id == args.request_id
                            and r.ref.revision == args.revision
                        ),
                        None,
                    )
                    if page["next_cursor"] is None:
                        break
                    cursor = page["next_cursor"]
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
    snapshot = service.snapshot()
    maintenance = snapshot.details.get("profile") == "repository_maintenance"
    render(snapshot)
    focus = service.current_focus(context=context)
    if focus and focus["work_id"]:
        render({"focus": focus})
    while True:
        try:
            line = input_fn("service> ")
        except EOFError:
            break
        except KeyboardInterrupt:
            print("\nInput cancelled.")
            continue
        if not line.strip():
            continue
        if line.strip() in {"help", "--help"}:
            parser.print_help()
            continue
        if line.strip() == "exit":
            break
        try:
            command = line.split(maxsplit=1)[0]
            if command not in actions.choices:
                if maintenance:
                    render(
                        service.converse(
                            line,
                            context=context,
                            work_id=focus["work_id"] if focus else None,
                            revision=focus["requirements_revision"] if focus else None,
                        )
                    )
                elif focus and focus["work_id"]:
                    render(
                        service.work.get(focus["work_id"]).steer(
                            line, revision=focus["revision"]
                        )
                    )
                    focus = service.focus(focus["work_id"], context=context)
                else:
                    render(submit(service, line))
            else:
                args = parser.parse_args(shlex.split(line))
                if args.command == "focus":
                    focus = service.focus(args.work_id, context=context)
                    render({"focus": focus})
                elif args.command == "requests" and focus and focus["work_id"]:
                    if args.limit is not None or args.cursor or args.expired:
                        page = service.requests_page(
                            work_id=focus["work_id"],
                            cursor=args.cursor,
                            limit=args.limit or 20,
                            expired=args.expired,
                        )
                        render({**page, "items": [r.data() for r in page["items"]]})
                    else:
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
        if args.command == "validate":
            if connection is not None:
                raise ValueError("Validation requires the local service host.")
            from .setup_validation import validate_service

            render(validate_service(args.spec, args.host_config, args.cwd))
            return 0
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
        if args.command == "ops" and args.ops_command == "upgrade-rollback":
            if connection is not None:
                raise ValueError(
                    "Upgrade rollback is an explicit host-local maintenance operation."
                )
            from homebrew_harness import Harness
            from homebrew_harness.contracts import ExecutionRef

            with Harness(workdir=args.cwd.expanduser()) as host:
                render(
                    host.admin.service_upgrade_rollback(
                        ExecutionRef("service", args.service_id),
                        args.identity,
                        request_id=args.request_id,
                    )
                )
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
