"""Host CLI for storage/GC/benchmarks; deliberately absent from model tool schemas."""

import io
import json
import os
import stat
import sys
from pathlib import Path

from homebrew_harness.cli_arguments import add_location_arguments


def external_model_config(path, workspace):
    from dotenv import dotenv_values

    from homebrew_harness.security.data_boundary import register_private_values

    path = Path(path).expanduser()
    if path.resolve().is_relative_to(workspace.resolve()) or path.is_symlink():
        raise ValueError(
            "Benchmark connection settings must be a private host file outside the workspace."
        )
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise ValueError(
                "Benchmark connection file must be private and owner-only."
            )
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("Benchmark connection file exceeds the size limit.")
    finally:
        os.close(fd)
    values = dotenv_values(stream=io.StringIO(raw.decode()), interpolate=False)
    names = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL")
    config = {name: os.getenv(name) or values.get(name) for name in names}
    if any(not value for value in config.values()):
        raise ValueError(
            "Benchmark connection file requires OPENAI_API_KEY, OPENAI_BASE_URL and OPENAI_MODEL."
        )
    register_private_values(config["OPENAI_API_KEY"], config["OPENAI_BASE_URL"])
    return config


def register(groups):
    parser = groups.add_parser(
        "admin", help="Manage host storage, history, artifacts and evaluations."
    )
    add_location_arguments(parser)
    commands = parser.add_subparsers(dest="command", required=True)
    descriptions = {
        "storage-status": "Inspect storage without creating state.",
        "storage-backup": "Create a storage backup.",
        "inventory": "Inspect retained artifacts and history.",
        "artifact-list": "List output artifact bundles.",
        "artifact-discard-preview": "Preview an artifact discard plan.",
        "artifact-discard": "Apply a saved artifact discard plan.",
        "gc-preview": "Preview history collection.",
        "gc-apply": "Apply a saved history collection plan.",
        "gc-recover": "Recover an interrupted history collection.",
        "storage-baseline": "Measure offline storage behavior.",
        "benchmark": "Run an offline or explicit live model benchmark.",
        "checkpoint-list": "List checkpoint retirement candidates.",
        "checkpoint-pin": "Pin one retained session.",
        "checkpoint-unpin": "Unpin one retained session.",
        "checkpoint-retire-preview": "Preview checkpoint retirement.",
        "checkpoint-retire": "Apply a saved retirement plan.",
        "checkpoint-unretire": "Restore a retired checkpoint.",
        "checkpoint-purge-preview": "Preview retired checkpoint removal.",
        "checkpoint-purge": "Apply a saved checkpoint removal plan.",
        "checkpoint-reconcile": "Reconcile interrupted checkpoint operations.",
        "incremental-prune-preview": "Preview unused incremental revisions.",
        "incremental-prune": "Apply a saved incremental pruning plan.",
    }
    for name, description in descriptions.items():
        command = commands.add_parser(name, help=description, description=description)
        add_location_arguments(command)
        command.add_argument(
            "--output", help="Optional report path under .harness/ inside --cwd"
        )
        if name in {"inventory", "gc-preview"}:
            command.add_argument("--retention-days", type=float, default=30)
        if name in {
            "artifact-discard",
            "gc-apply",
            "checkpoint-retire",
            "checkpoint-purge",
            "incremental-prune",
        }:
            command.add_argument("--plan", help="Saved preview JSON path inside --cwd")
        if name == "gc-recover":
            command.add_argument("--run-id")
        if name == "artifact-discard-preview":
            command.add_argument("--bundle-id")
        if name == "artifact-list":
            command.add_argument("--offset", type=int, default=0)
            command.add_argument("--limit", type=int, default=20)
        if name in {
            "checkpoint-list",
            "checkpoint-retire-preview",
            "checkpoint-purge-preview",
            "checkpoint-pin",
            "checkpoint-unpin",
            "checkpoint-unretire",
        }:
            command.add_argument(
                "--session-id",
                action="append",
                help="Explicit session selection for checkpoint management",
            )
        if name in {"checkpoint-list", "checkpoint-retire-preview"}:
            command.add_argument("--checkpoint-days", type=int, default=15)
            command.add_argument("--keep-recent", type=int, default=10)
        if name == "benchmark":
            command.add_argument(
                "--live",
                action="store_true",
                help="Use the configured model; default is offline verification",
            )
            command.add_argument(
                "--max-turns",
                type=int,
                default=4,
                help="Benchmark turns per case, 1..100",
            )
            command.add_argument(
                "--timeout-seconds",
                type=float,
                default=120,
                help="Benchmark absolute batch deadline",
            )
    parser.set_defaults(_handler=__name__, _command_parser=parser)


def execute(args, parser):
    from homebrew_harness.security.safe_files import SafeFile
    from homebrew_harness.storage.storage_admin import backup, publish, storage_status

    root = args.cwd.expanduser()
    try:
        if args.output is not None and (
            Path(args.output).is_absolute()
            or ".." in Path(args.output).parts
            or not args.output.startswith(
                (".harness/reports/", ".harness/benchmarks/", ".harness/gc/previews/")
            )
        ):
            raise ValueError(
                "Report outputs must use a dedicated report, benchmark or GC preview directory."
            )
        if args.command.startswith("artifact-"):
            from homebrew_harness.storage.artifact_store import (
                discard,
                discard_preview,
                host_artifact_list,
            )

            if args.command == "artifact-list":
                result = host_artifact_list(root, offset=args.offset, limit=args.limit)
            elif args.command == "artifact-discard-preview":
                if not args.bundle_id:
                    raise ValueError("Artifact preview requires --bundle-id.")
                result = discard_preview(root, args.bundle_id)
            else:
                if not args.plan:
                    raise ValueError("Artifact discard requires a saved --plan.")
                with SafeFile(root, args.plan) as target:
                    raw, _, _ = target.snapshot(max_bytes=4 * 1024 * 1024)
                result = discard(root, json.loads(raw))
        elif args.command == "incremental-prune-preview":
            from homebrew_harness.storage.incremental_gc import preview

            result = preview(root)
        elif args.command == "incremental-prune":
            from homebrew_harness.storage.incremental_gc import prune

            if not args.plan:
                raise ValueError("Incremental prune requires a saved --plan.")
            with SafeFile(root, args.plan) as target:
                raw, _, _ = target.snapshot(max_bytes=4 * 1024 * 1024)
            result = prune(root, json.loads(raw))
        elif args.command.startswith("checkpoint-"):
            from homebrew_harness.storage.checkpoint_retirement import (
                pin,
                preview,
                purge,
                purge_preview,
                reconcile,
                retire,
                unretire,
            )

            command = args.command[11:]
            if command in {"list", "retire-preview"}:
                result = preview(
                    root,
                    days=args.checkpoint_days,
                    keep_recent=args.keep_recent,
                    selected=args.session_id,
                )
            elif command == "purge-preview":
                result = purge_preview(root, selected=args.session_id)
            elif command in {"pin", "unpin", "unretire"}:
                if not args.session_id or len(args.session_id) != 1:
                    raise ValueError("This operation requires one --session-id.")
                result = (
                    unretire(root, args.session_id[0])
                    if command == "unretire"
                    else pin(root, args.session_id[0], command == "pin")
                )
            elif command == "reconcile":
                result = reconcile(root)
            else:
                if not args.plan:
                    raise ValueError(
                        "Checkpoint mutation requires an explicit saved --plan."
                    )
                with SafeFile(root, args.plan) as target:
                    raw, _, _ = target.snapshot(max_bytes=4 * 1024 * 1024)
                result = (retire if command == "retire" else purge)(
                    root, json.loads(raw)
                )
        elif args.command == "storage-status":
            result = storage_status(root)
        elif args.command == "storage-backup":
            result = backup(root)
        elif args.command in {"inventory", "gc-preview"}:
            from homebrew_harness.storage.artifact_inventory import inspect_artifacts

            result = inspect_artifacts(root, retention_days=args.retention_days)
        elif args.command == "gc-apply":
            from homebrew_harness.storage.history_gc import apply

            if not args.plan:
                raise ValueError("GC apply requires an explicit saved --plan.")
            with SafeFile(root, args.plan) as target:
                raw, _, _ = target.snapshot(max_bytes=4 * 1024 * 1024)
            result = apply(root, json.loads(raw))
        elif args.command == "gc-recover":
            from homebrew_harness.storage.history_gc import recover

            result = recover(root, args.run_id)
        elif args.command == "storage-baseline":
            from homebrew_harness.storage.storage_baseline import run_baseline

            result = run_baseline()
        else:
            from homebrew_harness.benchmark import run_benchmark

            client = None
            model = "offline-fixture"
            if args.live:
                from openai import OpenAI

                config = external_model_config(args.env_file, root)
                client = OpenAI(
                    api_key=config["OPENAI_API_KEY"],
                    base_url=config["OPENAI_BASE_URL"],
                    max_retries=0,
                )
                model = config["OPENAI_MODEL"]
            try:
                result = run_benchmark(
                    client,
                    model,
                    max_turns=args.max_turns,
                    timeout_seconds=args.timeout_seconds,
                )
            finally:
                if client is not None:
                    client.close()
        if args.output:
            publish(root, args.output, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get("passed") is False else 0
    except (OSError, ValueError, TypeError) as error:
        print(
            json.dumps(
                {
                    "error": type(error).__name__,
                    "message": "Host operation failed; state was not silently switched or replayed.",
                }
            ),
            file=sys.stderr,
        )
        return 1
