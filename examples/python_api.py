"""Application SDK examples with optional deterministic, offline model replies."""

import argparse
import asyncio
import json
from pathlib import Path

from homebrew_harness import AsyncHarness, Harness, HostConfig
from homebrew_harness.orchestration.workflow_runtime import (
    WorkflowRegistry,
    WorkflowSpec,
)
from homebrew_harness.runs import RunSpec


class DemoClient:
    """Small deterministic client for the Session's normal tool dispatch path."""

    _harness_model_identity = "sdk-example"

    def __init__(self):
        from types import SimpleNamespace

        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **_kwargs):
        from openai.types.chat import ChatCompletion

        self.calls += 1
        if self.calls == 1:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "demo-write",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps(
                                {"path": "sdk-demo.txt", "content": "hello\n"}
                            ),
                        },
                    }
                ],
            }
        else:
            message = {"role": "assistant", "content": "Created sdk-demo.txt."}
        return ChatCompletion.model_validate(
            {
                "id": "sdk-demo",
                "object": "chat.completion",
                "created": 0,
                "model": "demo",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls" if self.calls == 1 else "stop",
                        "message": message,
                    }
                ],
            }
        )


async def echo(ctx, args):
    ctx.check_stop()
    return {"echo": args["text"]}


def configuration(args):
    settings = {}
    for name in ("env_file", "models_config"):
        if getattr(args, name) is not None:
            settings[name] = getattr(args, name)
    registry = None
    if args.mode == "workflow":
        schema = {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        }
        registry = WorkflowRegistry(
            [WorkflowSpec("echo", "Echo supplied text", echo, schema)]
        )
    if args.demo:
        if args.mode == "run":
            raise ValueError(
                "The Run example requires reconstructable external host configuration."
            )
        return HostConfig.from_components(
            client=DemoClient(), model="demo", workflow_registry=registry, **settings
        )
    return HostConfig.from_components(workflow_registry=registry, **settings)


async def async_session(workspace, config, prompt):
    async with AsyncHarness(workdir=workspace, config=config) as host:
        session = await host.sessions.open()
        turn = await session.submit(prompt, request_id="example-query")
        report = await turn.wait(120)
        print(json.dumps(report.data(), ensure_ascii=False))
        if report.snapshot.output_available:
            print((await turn.result()).text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument(
        "--mode", choices=("session", "async", "run", "workflow"), default="session"
    )
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--models-config", type=Path)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--prompt", default="Write sdk-demo.txt containing hello.")
    args = parser.parse_args()
    args.workspace.mkdir(parents=True, exist_ok=True)
    config = configuration(args)
    if args.mode == "async":
        asyncio.run(async_session(args.workspace, config, args.prompt))
        return
    with Harness(workdir=args.workspace, config=config) as host:
        if args.mode == "session":
            job = host.sessions.open().submit(args.prompt, request_id="example-query")
        elif args.mode == "run":
            job = host.runs.start(RunSpec(args.prompt), request_id="example-run")
        else:
            job = host.workflows.start(
                "echo", {"text": args.prompt}, request_id="example-workflow"
            )
        report = job.wait(120)
        print(json.dumps(report.data(), ensure_ascii=False))
        if report.snapshot.output_available:
            print(json.dumps(job.result().data(), ensure_ascii=False))


if __name__ == "__main__":
    main()
