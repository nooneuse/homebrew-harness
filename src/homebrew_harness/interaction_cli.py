"""Default interactive terminal; explicit feature commands keep their own entry."""

import json
import os
import sys

from homebrew_harness.runtime.interaction_config import (
    InteractionAssembler,
    InteractionConfig,
)
from homebrew_harness.runtime.interaction_host import InteractionHost
from homebrew_harness.security.data_boundary import redact


def announce(value):
    value = redact(value)
    if isinstance(value, str):
        print("\n" + value, flush=True)
    elif value.get("requests"):
        for request in value["requests"]:
            label = request["id"] + " revision " + str(request["revision"])
            if request["kind"] == "attention":
                print(
                    "\nDecision needed (" + label + "): " + request["question"],
                    flush=True,
                )
                print("Answer here, or use /answer ID REVISION TEXT.", flush=True)
            else:
                print(
                    "\nApproval needed ("
                    + label
                    + "): "
                    + str(request.get("tool") or "fixed test grant"),
                    flush=True,
                )
                print(
                    json.dumps(
                        request.get("arguments") or request.get("grant_context"),
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                if request["kind"] == "tool":
                    print(
                        "Workspace: "
                        + str(request.get("context", {}).get("cwd", "."))
                        + ". Answer yes/no for a unique request, or use /approve ID REVISION yes/no.",
                        flush=True,
                    )
                else:
                    print("Use /grant ID REVISION RUNS SECONDS.", flush=True)
    elif value.get("execution_mode"):
        mode = (
            "Running interactively. "
            if value["execution_mode"] == "interactive"
            else "Continuing as a managed task. "
        )
        print("\n" + mode + value.get("reason", ""), flush=True)
    elif value.get("error") or value.get("missing"):
        print("\n" + str(value.get("error") or "; ".join(value["missing"])), flush=True)
    elif value.get("status"):
        print("\nTask " + value["status"] + ".", flush=True)
        if value["status"] == "completed" and value.get("result"):
            print(value["result"], flush=True)
    elif value.get("state") in {"accepted", "startup_pending", "handed_off"}:
        print(
            "\n"
            + {
                "accepted": "Background supervisor accepted the task.",
                "startup_pending": "Supervisor startup needs inspection; use /status.",
                "handed_off": "The task is continuing as a managed Run.",
            }[value["state"]],
            flush=True,
        )


def execute(args, parser):
    host = None
    try:
        workdir = args.cwd.expanduser().resolve()
        if not workdir.is_dir():
            parser.error("--cwd must be an existing directory")
        default = InteractionConfig()
        config = InteractionConfig.load(
            args.host_config or os.getenv("HARNESS_INTERACTION_CONFIG"),
            workdir,
            env_file=args.env_file if args.env_file != default.env_file else None,
            models_config=args.models_config,
        )
        assembler = InteractionAssembler(workdir, config)

        host = InteractionHost(assembler, trusted=sys.stdin.isatty(), announce=announce)
        print(
            "homebrew-harness — enter a task; active text steers it. /status, /queue, /pause, /resume, /cancel, /upgrade, /detach; q to leave."
        )
        initial = host.status()
        if initial["state"] != "idle":
            print("Attached to the existing task. Use /status for details.", flush=True)
        while True:
            try:
                text = input("agent> ")
            except EOFError:
                break
            from homebrew_harness.runtime.host_commands import exit_command
            if exit_command(text):
                break
            if not text.strip():
                continue
            try:
                result = redact(host.submit(text))
                if text.strip() in {"/status", "状态"}:
                    print(json.dumps(result, ensure_ascii=False), flush=True)
                elif result.get("result") is not None:
                    print(result["result"], flush=True)
                elif result.get("input_id"):
                    print(
                        "Queued a follow-up task."
                        if text.startswith("/queue ")
                        else "Input accepted.",
                        flush=True,
                    )
                else:
                    print("Request accepted.", flush=True)
            except (ValueError, OSError) as error:
                print(redact(str(error)), file=sys.stderr)
        result = host.close()
        return 0 if result["state"] == "closed" else 2
    except Exception as error:
        print(redact(str(error)), file=sys.stderr)
        return 1
    finally:
        if host and not host.closed:
            host.close()
