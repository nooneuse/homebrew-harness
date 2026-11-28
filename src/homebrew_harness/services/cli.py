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
        validate = actions.add_parser(
            "validate", help="Check external service configuration without starting it"
        )
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
    base_updates = delivery_ops.add_parser("base-updates", help="Choose manual, automatic or periodic target integration.")
    base_updates.add_argument("mode", choices=("manual", "automatic", "periodic"))
    base_updates.add_argument("--request-id")
    assess = delivery_ops.add_parser("assess-integration", help="Request an early periodic integration assessment.")
    assess.add_argument("identity")
    assess.add_argument("--request-id")
    limits = delivery_ops.add_parser("ci-limits")
    limits.add_argument("--limit", type=json.loads, default=None, help="null for advisory counts; an integer for a hard budget")
    limits.add_argument("--warning", type=json.loads, default=2, help="Progress threshold, or null to disable")
    limits.add_argument("--request-id")
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
    _decision_arguments(decide, "REQUEST_ID")
    decide.add_argument("--revision", type=int, default=0)
    inbox = actions.add_parser("inbox", help="List, decide, or refresh human requests.")
    if attach:
        inbox.add_argument("service_id")
    _inbox_list_arguments(inbox)
    inbox_ops = inbox.add_subparsers(dest="inbox_command")
    _inbox_list_arguments(inbox_ops.add_parser("list"))
    inbox_decide = inbox_ops.add_parser("decide")
    _decision_arguments(inbox_decide, "TOKEN")
    inbox_decide.add_argument("--work-id", default=argparse.SUPPRESS)
    inbox_decide.add_argument("--snapshot", metavar="DIGEST", help="Pending inbox snapshot for indices and bare IDs.")
    inbox_decide.add_argument("--limit", metavar="N", type=int, default=argparse.SUPPRESS,
                              help="Limit used to list the snapshot (default: 50).")
    refresh = inbox_ops.add_parser("refresh-expired")
    refresh.add_argument("--ids", nargs="+", metavar="REQUEST_ID")
    refresh.add_argument("--request-id")
    inbox_ops.add_parser("notify-status", help="Show webhook configuration and delivery status.")
    notify = inbox_ops.add_parser("notify", help="Send the current inbox digest to the configured webhook.")
    notify.add_argument("--force", action="store_true", help="Send even when no items are actionable.")
    notify.add_argument("--request-id")
    tasks = work.add_subparsers(dest="work_command", required=True)
    submit = tasks.add_parser("submit")
    submit.add_argument("text")
    submit.add_argument("--priority", type=int, default=0)
    submit.add_argument("--request-id")
    listing = tasks.add_parser("list")
    listing.add_argument("--state")
    listing.add_argument("--cursor", type=int, default=0)
    listing.add_argument("--limit", type=int, default=20)
    retry = tasks.add_parser(
        "retry", help="Retry the current Run with renewed work authority."
    )
    retry.add_argument("work_id")
    retry.add_argument("--seconds", type=float)
    retry.add_argument("--request-id")
    planning = tasks.add_parser("plan-recovery", help="Request a read-only Lead recovery proposal for a paused Work.")
    planning.add_argument("work_id")
    planning.add_argument("--instruction", default="")
    planning.add_argument("--request-id")
    planning.add_argument('--execute',action='store_true',help='Apply the Lead selected action after planning under current Work authority.')
    sources=tasks.add_parser('sources');sources.add_argument('work_id')
    read=tasks.add_parser('read-source');read.add_argument('work_id');read.add_argument('source_ref');read.add_argument('path')
    read.add_argument('--offset',type=int,default=0);read.add_argument('--limit',type=int,default=10000);read.add_argument('--expected-version')
    compare=tasks.add_parser('compare-sources');compare.add_argument('work_id');compare.add_argument('before');compare.add_argument('after')
    compare.add_argument('--offset',type=int,default=0);compare.add_argument('--limit',type=int,default=100);compare.add_argument('--expected-version')
    target=tasks.add_parser('observe-target');target.add_argument('work_id');target.add_argument('--request-id')
    apply=tasks.add_parser('apply-recovery');apply.add_argument('work_id');apply.add_argument('--job-id');apply.add_argument('--start',action='store_true');apply.add_argument('--request-id')
    cont=tasks.add_parser('continue-from');cont.add_argument('work_id');cont.add_argument('source_ref');cont.add_argument('--reason',required=True)
    cont.add_argument('--mode',choices=('fork','adopt_candidate'),default='fork');cont.add_argument('--start',action='store_true');cont.add_argument('--target-ref')
    cont.add_argument('--method',choices=('merge','rebase'));cont.add_argument('--request-id')
    integration=tasks.add_parser('integration');integration.add_argument('work_id');integration.add_argument('operation',choices=('inspect','continue','abort'));integration.add_argument('--request-id')
    for name in (
        "show",
        "progress",
        "pause",
        "resume",
        "cancel",
        "revoke",
        "request-authority",
    ):
        tasks.add_parser(name).add_argument("work_id")
    for name in ("steer", "interrupt", "revise", "context"):
        item = tasks.add_parser(name)
        item.add_argument("work_id")
        item.add_argument("text")
        item.add_argument("--request-id")
    for name in ("approve", "deny", "answer", "renew-tests", "review-retry"):
        item = actions.add_parser(name)
        if attach:
            item.add_argument("service_id")
        item.add_argument("request_id")
        item.add_argument("revision", type=int)
        item.add_argument("--work-id")
        if name == "review-retry":
            item.add_argument("--retry-id")
        else:
            item.add_argument("--decision-id")
        if name == "answer":
            item.add_argument("text")
        if name == "approve":
            item.add_argument("--allow-extra-attempt", action="store_true",
                              help="Grant one next attempt for this CI repair beyond its default ceiling")
        if name == "renew-tests":
            item.add_argument("--runs", type=int, required=True)
            item.add_argument("--seconds", type=float, required=True)
    return actions


def _decision_arguments(parser, token):
    parser.add_argument("--approve", nargs="*", action="extend", default=[], metavar=token)
    parser.add_argument("--deny", nargs="*", action="extend", default=[], metavar=token)
    parser.add_argument("--answer", action="append", default=[], metavar=token + "=TEXT")
    parser.add_argument("--allow-extra-attempt", nargs="*", action="extend", default=[], metavar=token)
    parser.add_argument("--request-id")


def _inbox_list_arguments(parser):
    # Shared by the implicit and explicit list forms; do not overwrite parent flags.
    parser.add_argument("--work-id", default=argparse.SUPPRESS)
    parser.add_argument("--include-expired", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--limit", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)


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
        if getattr(error, "code", None) or getattr(error, "reason_code", None):
            from homebrew_harness.runtime.errors import error_info

            render(error_info(error), file=file)
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


def _batch_decisions(args):
    approved = args.approve
    extra = set(args.allow_extra_attempt)
    if extra - set(approved):
        raise ValueError("Extra attempts must name approved CI repair requests in this batch.")
    decisions = [
        {"id": identity, "decision": "approve", **({"allow_extra_attempt": True} if identity in extra else {})}
        for identity in approved
    ]
    decisions.extend({"id": token, "decision": "deny"} for token in args.deny)
    decisions.extend(_answer_action(answer, "id") for answer in args.answer)
    return decisions


def _answer_action(answer, key):
    token, separator, text = answer.partition("=")
    if not separator or not token or not text.strip():
        raise ValueError("Use --answer TOKEN=TEXT with a nonempty answer.")
    return {key: token, "decision": "answer", "text": text}


def _operate_inbox(service, args, *, inbox_snapshot=None):
    work_id = getattr(args, "work_id", None)
    if args.inbox_command == "refresh-expired":
        return service.refresh_expired(ids=args.ids, request_id=args.request_id)
    if args.inbox_command == "notify-status":
        return service.notification_status()
    if args.inbox_command == "notify":
        return service.send_notification(force=args.force, request_id=args.request_id)
    if args.inbox_command != "decide":
        return service.inbox(
            work_id=work_id,
            include_expired=getattr(args, "include_expired", False),
            limit=getattr(args, "limit", 50),
        )

    snapshot = args.snapshot
    limit = getattr(args, "limit", 50)
    if snapshot is None and inbox_snapshot is not None:
        # The digest covers only the displayed pending items, even when the
        # listing included expired requests. Reuse the same scope by default.
        snapshot = inbox_snapshot["digest"]
        work_id = getattr(args, "work_id", inbox_snapshot.get("work_id"))
        limit = getattr(args, "limit", inbox_snapshot.get("limit", 50))
    actions = [
        {"token": token, "decision": decision}
        for decision in ("approve", "deny", "allow_extra_attempt")
        for token in getattr(args, decision)
    ]
    actions.extend(_answer_action(answer, "token") for answer in args.answer)
    return service.decide_inbox(
        actions, work_id=work_id, snapshot=snapshot, limit=limit, request_id=args.request_id
    )


def _render_command(value, args):
    if (
        args.command == "inbox"
        and args.inbox_command in {None, "list"}
        and not getattr(args, "json", False)
    ):
        print(value["summary"])
    else:
        render(value)


def operate(service, args, *, inbox_snapshot=None):
    if args.command == "inbox":
        return _operate_inbox(service, args, inbox_snapshot=inbox_snapshot)
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
            else (args.identity,) if hasattr(args, "identity") else ()
        )
        return getattr(service, methods[op])(*arguments, **values)
    if args.command == "delivery":
        op = args.delivery_command
        if op == "base-updates":
            return service.configure_base_updates(args.mode, request_id=args.request_id)
        if op == "assess-integration":
            return service.assess_integration(args.identity, request_id=args.request_id)
        if op == "ci-limits":
            return service.configure_ci_limits(ci_fix_limit=args.limit,
                ci_fix_warning=args.warning, request_id=args.request_id)
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
                subject=(
                    json.loads(args.subject_file.read_text())
                    if args.subject_file
                    else None
                ),
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
            args.identity, _batch_decisions(args),
            revision=args.revision, request_id=args.request_id,
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
        if args.work_command == "plan-recovery":
            return work.plan_recovery(args.instruction,execute=args.execute,request_id=args.request_id)
        if args.work_command=='sources':return work.sources()
        if args.work_command=='read-source':return work.read_source(args.source_ref,args.path,offset=args.offset,limit=args.limit,expected_version=args.expected_version)
        if args.work_command=='compare-sources':return work.compare_sources(args.before,args.after,offset=args.offset,limit=args.limit,expected_version=args.expected_version)
        if args.work_command=='observe-target':return work.observe_target(request_id=args.request_id)
        if args.work_command=='apply-recovery':return work.apply_recovery(job_id=args.job_id,start=args.start,request_id=args.request_id)
        if args.work_command=='continue-from':
            action={'mode':args.mode,'source_ref':args.source_ref,'reason':args.reason,'start':args.start}
            if args.target_ref or args.method:
                if not args.target_ref or not args.method:raise ValueError('Integration requires --target-ref and --method together.')
                action['integration']={'target_ref':args.target_ref,'method':args.method}
            return work.continue_from(action,request_id=args.request_id)
        if args.work_command=='integration':return work.control_integration(args.operation,request_id=args.request_id)
        if args.work_command == "show":
            return work.snapshot()
        if args.work_command == "retry":
            return work.retry(seconds=args.seconds, request_id=args.request_id)
        if args.work_command in {"steer", "interrupt", "revise", "context"}:
            return getattr(work, args.work_command)(
                args.text, request_id=args.request_id
            )
        return getattr(work, args.work_command.replace("-", "_"))()
    if args.command in {"approve", "deny", "answer", "renew-tests", "review-retry"}:
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
        if args.command == "review-retry":
            return service.retry_approval_review(request, request_id=args.retry_id)
        values = {}
        if args.command == "answer":
            values["text"] = args.text
        if args.command == "approve" and getattr(args, "allow_extra_attempt", False):
            values["allow_extra_attempt"] = True
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
    last_inbox = None
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
                    value = operate(service, args, inbox_snapshot=last_inbox)
                    if args.command == "inbox" and args.inbox_command in {None, "list"}:
                        last_inbox = {
                            "digest": value["digest"],
                            "work_id": getattr(args, "work_id", None),
                            "limit": getattr(args, "limit", 50),
                        }
                    # Keep the listed digest for exact retries with --request-id;
                    # a new decision against a stale digest is rejected server-side.
                    _render_command(value, args)
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
                    else (
                        HostConfig.from_file(args.host_config, workdir=control)
                        if args.host_config
                        else HostConfig.from_files(env_file=args.env_file)
                    )
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
            _render_command(operate(remote, args), args)
        return 0
    except Exception as error:
        render_error(error, file=sys.stderr)
        return 1
    finally:
        if remote is not None:
            remote.close()
        if core is not None:
            core.close()
