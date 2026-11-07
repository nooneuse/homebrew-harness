"""Explicit fixture assembly for isolated tool tests; production dispatch is pure."""

import homebrew_harness.context.skill_loader
import homebrew_harness.context.todo
import homebrew_harness.runtime.hooks
import homebrew_harness.tools.dispatch
import homebrew_harness.tools.registry


def fixture_invocation(call):
    from homebrew_harness.execution.model_protocol import ToolInvocation

    if isinstance(call, ToolInvocation):
        return call
    value = (
        call.model_dump(exclude_none=True)
        if hasattr(call, "model_dump")
        else {
            "id": getattr(call, "id", "fixture"),
            "type": call.type,
            "function": {
                "name": call.function.name,
                "arguments": call.function.arguments,
            },
        }
    )
    return ToolInvocation.from_history(value)


def execute_fixture_tool(call, workdir, **kwargs):
    call = fixture_invocation(call)
    hooks = kwargs.pop("hooks", None)
    if hooks is None:
        hooks = homebrew_harness.runtime.hooks.create_hooks()
    handlers = kwargs.pop("handlers", None)
    if handlers is None:
        host = kwargs.get("team_manager")
        member = kwargs.get("team_member")
        if member is not None:
            handlers = {
                **homebrew_harness.tools.registry.TOOL_HANDLERS,
                **member.handlers(),
            }
        else:
            handlers = homebrew_harness.tools.registry.create_tool_handlers(
                homebrew_harness.context.todo.TodoManager(),
                skill_loader=homebrew_harness.context.skill_loader.SkillLoader(
                    workdir / "skills"
                ),
                task_store=getattr(host, "store", None),
                team_manager=host,
                background_manager=kwargs.get("background_manager"),
                mcp_runtime=kwargs.get("mcp_runtime"),
                workflow_runtime=kwargs.get("workflow_runtime"),
                test_runner=kwargs.get("test_runner"),
            )
    return homebrew_harness.tools.dispatch.execute_tool(
        call, workdir, hooks=hooks, handlers=handlers, **kwargs
    )


def queued_cli_input(answers, finished):
    import threading
    import time

    values = iter(answers)

    def read(_prompt):
        value = next(values)
        if value in {"q", "quit", "exit"}:
            end = time.monotonic() + 4
            while not finished() and time.monotonic() < end:
                threading.Event().wait(0.01)
            if not finished():
                raise AssertionError(
                    "Queued CLI work did not reach its expected boundary."
                )
        return value

    return read
