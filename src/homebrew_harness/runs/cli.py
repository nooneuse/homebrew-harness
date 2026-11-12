"""Local host commands for persistent managed runs."""

import json
import sys
from pathlib import Path

from homebrew_harness.cli_arguments import (
    add_host_config_arguments,
    add_location_arguments,
)


def register(groups):
    parser = groups.add_parser(
        "run", help="Supervise and control persistent managed runs."
    )
    add_location_arguments(parser)
    add_host_config_arguments(parser)
    parser.add_argument("--control-config", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    start.add_argument("--spec", type=Path, required=True)
    resource = commands.add_parser("mcp-resource-inspect")
    resource.add_argument("run_id")
    resource.add_argument("resource_id")
    resource.add_argument("revision", type=int)
    resource.add_argument("--token", required=True)
    resource.add_argument("--confirmed-stopped", action="store_true", required=True)
    for name in ("supervise", "status", "pause", "resume", "cancel", "recovery"):
        commands.add_parser(name).add_argument("run_id")
    for name in (
        "requests",
        "progress",
        "events",
        "effects",
        "poll",
        "stages",
        "handoffs",
    ):
        commands.add_parser(name).add_argument("run_id")
    for name in ("export", "backup"):
        command = commands.add_parser(name)
        command.add_argument("run_id")
        command.add_argument("--output", type=Path, required=True)
    restore = commands.add_parser("restore-backup")
    restore.add_argument("directory", type=Path)
    restore.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify-backup")
    verify.add_argument("directory", type=Path)
    transfers = commands.add_parser("transfers")
    transfers.add_argument("run_id")
    resolve_transfer = commands.add_parser("transfers-resolve")
    resolve_transfer.add_argument("run_id")
    resolve_transfer.add_argument("transfer_id")
    resolve_transfer.add_argument("revision", type=int)
    resolve_transfer.add_argument("decision", choices=("applied", "aborted"))
    handoff = commands.add_parser("handoff")
    handoff.add_argument("run_id")
    handoff.add_argument("model_profile")
    handoff.add_argument("revision", type=int)
    handoff.add_argument("--reason", required=True)
    approve = commands.add_parser("approve")
    approve.add_argument("run_id")
    approve.add_argument("request_id")
    approve.add_argument("revision", type=int)
    approve.add_argument("decision", choices=("yes", "no"))
    approve.add_argument("--runs", type=int)
    approve.add_argument("--seconds", type=float)
    answer = commands.add_parser("answer")
    answer.add_argument("run_id")
    answer.add_argument("request_id")
    answer.add_argument("revision", type=int)
    answer.add_argument("text")
    progress_reset = commands.add_parser("progress-reset")
    progress_reset.add_argument("run_id")
    progress_reset.add_argument("revision", type=int)
    progress_reset.add_argument("--reason", required=True)
    resolve = commands.add_parser("effects-resolve")
    resolve.add_argument("run_id")
    resolve.add_argument("effect_id")
    resolve.add_argument("revision", type=int)
    resolve.add_argument("outcome", choices=("delivered", "not_sent"))
    resolve.add_argument("--reason", required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("run_id")
    inspect.add_argument("operation_id")
    inspect.add_argument("revision", type=int)
    steer = commands.add_parser("steer")
    steer.add_argument("run_id")
    steer.add_argument("text")
    steer.add_argument(
        "--mode", choices=("steer", "interrupt", "queued"), default="steer"
    )
    steer.add_argument("--expected-turn-id")
    steer.add_argument("--expected-generation", type=int)
    for command in commands.choices.values():
        add_location_arguments(command)
    parser.set_defaults(_handler=__name__, _command_parser=parser)


def execute(args, parser):
    from homebrew_harness.security.data_boundary import redact

    from .spec import RunSpec
    from .store import RunStore
    from .supervisor import RunSupervisor
    from .worker import exit_status

    supervisor = None
    services = None
    try:
        if args.command == "restore-backup":
            from .maintenance import restore_backup

            print(json.dumps(restore_backup(args.directory, args.output), indent=2))
            return 0
        if args.command == "verify-backup":
            from .maintenance import verify_backup

            print(json.dumps(verify_backup(args.directory), indent=2))
            return 0
        store = RunStore(args.cwd.expanduser())
        if args.command == "start":
            spec_path = args.spec.expanduser().resolve()
            with spec_path.open("rb") as f:
                raw = f.read(100001)
            if len(raw) > 100000:
                raise ValueError("RunSpec exceeds its size limit.")
            spec = RunSpec.from_dict(json.loads(raw))
            declared = None
            if spec.mcp:
                from homebrew_harness.execution.model_connections import (
                    read_environment,
                )
                from homebrew_harness.tools.mcp.runtime import load_specs
                from homebrew_harness.tools.mcp.selection import declaration

                environment = read_environment(args.env_file, store.root)
                declared = declaration(
                    load_specs(args.mcp_config), spec.mcp, environment
                )

            def accept(_db, _conn, value):
                if declared is not None:
                    value["expected_mcp"] = declared

            value = store.create(spec, _accept=accept)
            identity = value["id"]
            print(json.dumps({"run_id": identity, "status": "queued"}), flush=True)
        else:
            identity = args.run_id

        if args.command == "mcp-resource-inspect":
            from homebrew_harness.execution.mcp_process import inspect_resource

            print(
                json.dumps(
                    inspect_resource(
                        store.runtime,
                        identity,
                        args.resource_id,
                        args.revision,
                        args.token,
                    ),
                    ensure_ascii=False,
                )
            )
            return 0

        def handoff_model(value, alias, reason, owner=None):
            from dotenv import load_dotenv

            from homebrew_harness.execution.model_registry import load_model_registry

            from .handoff import switch_model

            if (
                args.models_config is None
                or args.models_config.expanduser().resolve().is_relative_to(store.root)
            ):
                raise ValueError("Model handoff needs the external host model catalog.")
            if args.env_file.expanduser().resolve().is_relative_to(store.root):
                raise ValueError("Model environment must remain outside the workspace.")
            load_dotenv(args.env_file.expanduser(), override=False)
            registry = load_model_registry(args.models_config, store.root)
            try:
                return switch_model(
                    store,
                    identity,
                    alias,
                    value["revision"],
                    registry,
                    reason=reason,
                    supervisor=owner,
                )
            finally:
                registry.close()

        if args.command in {"transfers", "transfers-resolve"}:
            from .transfers import resolve_transfer

            store.get(identity)
            result = (
                {
                    key: value
                    for key, value in store.runtime.items("run_transfers").items()
                    if value["run_id"] == identity
                }
                if args.command == "transfers"
                else resolve_transfer(
                    store, identity, args.transfer_id, args.revision, args.decision
                )
            )
            print(json.dumps(redact(result), indent=2))
            return 0
        if args.command == "handoff":
            value = store.get(identity)
            if value["revision"] != args.revision:
                raise ValueError("Run revision changed before handoff.")
            print(
                json.dumps(
                    handoff_model(value, args.model_profile, args.reason), indent=2
                )
            )
            return 0
        if args.command in {"export", "backup"}:
            from .maintenance import backup_run, write_export

            result = (write_export if args.command == "export" else backup_run)(
                store, identity, args.output
            )
            print(json.dumps(result, indent=2))
            return 0
        if args.command == "status":
            print(json.dumps(store.status(identity), ensure_ascii=False, indent=2))
            return 0
        if args.command in {"stages", "handoffs"}:
            from .stages import stage_records

            result = (
                stage_records(store, identity)
                if args.command == "stages"
                else {
                    key: value
                    for key, value in store.runtime.items("stage_handoffs").items()
                    if value["run_id"] == store.get(identity)["id"]
                }
            )
            print(json.dumps(redact(result), ensure_ascii=False, indent=2))
            return 0
        if args.command in {"requests", "approve", "answer"}:
            from .requests import HumanRequests

            requests = HumanRequests(store)
            result = (
                requests.records(identity)
                if args.command == "requests"
                else requests.decide(
                    identity,
                    args.request_id,
                    args.revision,
                    "answer"
                    if args.command == "answer"
                    else "approve"
                    if args.decision == "yes"
                    else "deny",
                    args.text if args.command == "answer" else None,
                    runs=getattr(args, "runs", None),
                    seconds=getattr(args, "seconds", None),
                )
            )
            print(json.dumps(redact(result), ensure_ascii=False, indent=2))
            return 0
        if args.command in {"progress", "progress-reset"}:
            from homebrew_harness.execution.execution_leases import ExecutionLeases

            from .progress import ProgressTracker

            progress = ProgressTracker(store)
            if args.command == "progress":
                result = progress.get(identity)
            else:
                leases = ExecutionLeases(store.root)
                try:
                    with leases.use("session"):
                        result = progress.reset(identity, args.revision, args.reason)
                finally:
                    leases.close()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command in {"events", "effects"}:
            run = store.get(identity)
            values = store.runtime.items(
                "run_events" if args.command == "events" else "run_outbox",
                owner="lead:" + run["session_id"],
            )
            print(json.dumps(redact(values), ensure_ascii=False, indent=2))
            return 0
        if args.command == "recovery":
            value = store.get(identity)
            print(
                json.dumps(
                    {
                        "run_id": identity,
                        "operations": store.runtime.items(
                            "operations", owner="lead:" + value["session_id"]
                        ),
                        "last_outcome": value["last_outcome"],
                        "background": store.runtime.items("background"),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.command == "inspect":
            from homebrew_harness.execution.execution_leases import ExecutionLeases

            from .controller import RunController
            from .worker import make_session

            leases = ExecutionLeases(store.root)
            try:
                with leases.use("managed-supervisor"):
                    check = RunSupervisor(store)
                    value = store.get(identity)
                    if not check.execution_free() or check.worker_alive(value):
                        raise ValueError(
                            "Stop the existing execution before inspecting its operation."
                        )
                    value = store.claim(identity, manage=True)
                    control = RunController(
                        store, identity, value["owner"], value["generation"]
                    )
                    control.inspection_mode = True
                    session, raw = make_session(store, control, args)
                    try:
                        print(
                            session.run_user(
                                "/recovery inspect "
                                + args.operation_id
                                + " "
                                + str(args.revision)
                            )
                        )
                    finally:
                        session.close()
                        closed = session.wait_closed(control.spec.shutdown_seconds)
                        control.close_heartbeat()
                        store.ended(
                            control.header,
                            cleanup=closed
                            and session.shutdown_status()["persistence_ok"],
                        )
                        if raw and closed:
                            raw.close()
                    return 0
            finally:
                leases.close()
        if args.command == "steer":
            print(
                json.dumps(
                    store.submit(
                        identity,
                        args.text,
                        mode=args.mode,
                        expected_turn_id=args.expected_turn_id,
                        expected_generation=args.expected_generation,
                    )
                )
            )
            return 0
        if args.command in {"pause", "cancel"}:
            print(json.dumps(store.control(identity, args.command)))
            return 0
        if args.command == "resume":
            store.control(identity, "run")
        from .services import RunServices

        if args.control_config:
            from dotenv import load_dotenv

            if args.env_file.expanduser().resolve().is_relative_to(store.root):
                raise ValueError(
                    "Managed host configuration must be outside the workspace."
                )
            load_dotenv(args.env_file.expanduser(), override=False)
        services = RunServices.load(store, args.control_config)
        if args.command in {"poll", "effects-resolve"}:
            from homebrew_harness.execution.execution_leases import ExecutionLeases

            leases = ExecutionLeases(store.root)
            try:
                with leases.use("managed-supervisor"):
                    services.bind(identity)
                    result = (
                        services.tick(identity)
                        if args.command == "poll"
                        else services.resolve(
                            identity,
                            args.effect_id,
                            args.revision,
                            args.outcome,
                            args.reason,
                        )
                    )
                print(json.dumps(redact(result), ensure_ascii=False, indent=2))
            finally:
                leases.close()
            return 0
        settings = []
        for name in (
            "env_file",
            "models_config",
            "test_config",
            "bash_config",
            "mcp_config",
        ):
            path = getattr(args, name)
            if path is not None:
                settings.extend(
                    ("--" + name.replace("_", "-"), str(path.expanduser().resolve()))
                )
        if args.approval_review is not None:
            from homebrew_harness.security.approval_review import ReviewConfig
            settings.extend(("--approval-review", json.dumps(ReviewConfig.parse(args.approval_review).data())))
        supervisor = RunSupervisor(
            store,
            host_arguments=settings,
            services=services,
            model_handoff=lambda owner, value, alias: handoff_model(
                value,
                alias,
                "Host-declared fallback after " + value["last_outcome"]["reason_code"],
                owner,
            ),
        )
        result = supervisor.supervise(identity)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return exit_status(result)
    except Exception as error:
        if (
            "store" in locals()
            and "identity" in locals()
            and args.command in {"start", "supervise", "resume"}
        ):
            try:
                value = store.get(identity)
                if (
                    value["status"] in {"queued", "waiting"}
                    and value["cleanup_complete"]
                ):
                    from .spec import RunOutcome

                    store.update(
                        {
                            "run_id": identity,
                            "owner": value["owner"],
                            "generation": value["generation"],
                        },
                        {
                            "status": "blocked",
                            "last_outcome": RunOutcome(
                                identity,
                                value["episode"],
                                "blocked",
                                "service_configuration_failed",
                                value.get("checkpoint")
                                or "lead:" + value["session_id"],
                                required_host_actions=(
                                    "Restore the configured host services and explicitly resume",
                                ),
                                cleanup_complete=True,
                                details={"error_type": type(error).__name__},
                            ).data(),
                        },
                    )
            except Exception:
                pass
        print(redact(str(error)), file=sys.stderr)
        return 1
    finally:
        if supervisor:
            supervisor.close()
        elif "store" in locals():
            try:
                if services:
                    services.close()
            finally:
                store.close()
