from __future__ import annotations
from homebrew_harness.runtime.limits import current_limits

from homebrew_harness.runtime.output import print

import json

from homebrew_harness.security.permissions import (
    check_permission,
    approval_call_scope,
    record_artifact_approval,
    record_spawn_approval,
    record_test_approval,
    test_approval_needed,
)

"""s04: a small, ordered registry for four agent lifecycle events."""


import sys
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping

EVENTS = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")
CONTROL_EVENTS = {"PreToolUse", "Stop"}


class HookError(RuntimeError):
    """A control hook failed, so the runtime must not assume permission to proceed."""


def freeze_value(value):
    """Freeze nested JSON objects and arrays before sharing them with callbacks."""
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: freeze_value(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(freeze_value(item) for item in value)
    return value


def thaw_value(value):
    """Copy frozen Hook data back to JSON values for an exact permission request."""
    if isinstance(value, Mapping):
        return {key: thaw_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw_value(item) for item in value]
    return value


@dataclass(frozen=True)
class ToolEvent:
    name: str
    arguments: Mapping[str, object]
    workdir: Path
    call_id: str

    def __post_init__(self):
        # s05 adds nested todo arrays. Preserve the same read-only contract at
        # every level, not just the outer arguments dictionary.
        object.__setattr__(self, "arguments", freeze_value(self.arguments))


class HookRegistry:
    def __init__(self):
        self._failures = []
        self._callbacks: dict[str, list[Callable]] = {event: [] for event in EVENTS}

    def register_hook(self, event: str, callback: Callable, *, execution="inline") -> None:
        if event not in self._callbacks:
            raise ValueError(f"Unknown hook event: {event}")
        if not callable(callback):
            raise TypeError("A hook callback must be callable.")
        if execution not in {'inline', 'process'}:
            raise ValueError('Hook execution must be inline or process.')
        if execution == 'process':
            from homebrew_harness.execution.hook_process import ProcessHook
            callback = ProcessHook(callback)
        self._callbacks[event].append(callback)

    def snapshot(self, event=None):
        if event is not None and event not in EVENTS:
            raise ValueError("Unknown hook event.")
        return {"events": [{"event": name, "kind": "control" if name in CONTROL_EVENTS else "observer",
            "callbacks": [getattr(callback, "__name__", type(callback).__name__) for callback in tuple(self._callbacks[name])]}
            for name in EVENTS if event is None or name == event],
            "failures": [dict(row) for row in tuple(self._failures) if event is None or row["event"] == event]}

    def trigger_hooks(self, event: str, *args) -> str | None:
        if event not in self._callbacks:
            raise ValueError(f"Unknown hook event: {event}")
        # Registrations made during a callback apply to the next event emission.
        for index, callback in enumerate(tuple(self._callbacks[event])):
            try:
                result = callback(*args)
            except Exception as error:
                from homebrew_harness.runtime.errors import failure_record, error_message
                name = getattr(callback, "__name__", type(callback).__name__)
                previous = next((row for row in reversed(self._failures) if row["event"] == event and row.get("callback_index") == index), None)
                record = {"event": event, "callback": name, "callback_index": index, "error_type": type(error).__name__,
                          **failure_record(error, previous, component=name, phase=event)}
                if previous and record["occurrences"] > 1:
                    self._failures[self._failures.index(previous)] = record
                else:
                    self._failures.append(record)
                    self._failures[:] = self._failures[-20:]
                failure = HookError(f"{event} hook failed ({name}): {error_message(error)}")
                if event in CONTROL_EVENTS:
                    raise failure from error
                # Observer failures must not erase a real tool result or cause
                # the model to repeat an already completed side effect.
                if record["occurrences"] == 1:
                    print(f"[hook warning] {failure}", file=sys.stderr)
                continue
            for record in reversed(self._failures):
                if record["event"] == event and record.get("callback_index") == index and not record.get("resolved_at"):
                    import time
                    record["resolved_at"] = time.time()
                    break
            if event in CONTROL_EVENTS and result is not None:
                if not isinstance(result, str) or not result.strip():
                    raise HookError(
                        f"{event} hooks must return None or a non-empty string."
                    )
                return result
        return None


def permission_hook(event: ToolEvent) -> str | None:
    """Adapt the unchanged s03 policy to PreToolUse's blocking-result contract."""
    arguments = thaw_value(event.arguments)
    with approval_call_scope(event.call_id):
        decision = check_permission(event.name, arguments, event.workdir)
    if decision.action == "allow":
        if event.name == "run_tests" and test_approval_needed():
            record_test_approval(arguments, event.workdir)
        if event.name == "publish_artifacts":
            record_artifact_approval(arguments, event.workdir)
        if event.name == "spawn_teammate":
            record_spawn_approval(arguments, event.workdir)
        return None
    result = json.dumps(
        {
            "error": "Permission denied.",
            "permission": "deny",
            "reason": decision.reason,
            **(
                {"reason_code": decision.code}
                if getattr(decision, "code", None)
                else {}
            ),
        },
        ensure_ascii=False,
    )
    print(f"[hook] PreToolUse denied: {result}", file=sys.stderr)
    return result


def user_prompt_hook(query: str, workdir: Path) -> None:
    details = {"directory": str(workdir), "characters": len(query)}
    print(
        "[hook] UserPromptSubmit: " + json.dumps(details, ensure_ascii=False),
        file=sys.stderr,
    )


def tool_log_hook(event: ToolEvent) -> None:
    details = {"tool": event.name, "call_id": event.call_id}
    print(
        "[hook] PreToolUse: " + json.dumps(details, ensure_ascii=False), file=sys.stderr
    )


def tool_result_hook(event: ToolEvent, output: str) -> None:
    details = {
        "tool": event.name,
        "call_id": event.call_id,
        "result_characters": len(output),
    }
    print(
        "[hook] PostToolUse: " + json.dumps(details, ensure_ascii=False),
        file=sys.stderr,
    )


def large_output_hook(event: ToolEvent, output: str) -> None:
    if len(output) > current_limits().hook_response_chars:
        print(
            f"[hook] Large output: {event.name}, {len(output)} characters.",
            file=sys.stderr,
        )


def todo_display_hook(event: ToolEvent, output: str) -> None:
    if event.name == "todo_write":
        result = json.loads(output)
        if "error" not in result:
            print("\n[todo] Current plan\n" + result["plan"], file=sys.stderr)


def summary_hook(messages: list[dict]) -> None:
    count = sum(message["role"] == "tool" for message in messages)
    print(f"[hook] Stop: {count} tool results in the session.", file=sys.stderr)


def create_hooks() -> HookRegistry:
    """Each session gets its own registrations; permission always runs first."""
    hooks = HookRegistry()
    hooks.register_hook("UserPromptSubmit", user_prompt_hook)
    hooks.register_hook("PreToolUse", permission_hook)
    hooks.register_hook("PreToolUse", tool_log_hook)
    hooks.register_hook("PostToolUse", tool_result_hook)
    hooks.register_hook("PostToolUse", large_output_hook)
    hooks.register_hook("PostToolUse", todo_display_hook)
    hooks.register_hook("Stop", summary_hook)
    return hooks


def submit_prompt(
    messages: list[dict], query: str, workdir: Path, hooks: HookRegistry, *, parts=None, media_turn=False
) -> None:
    """Only real user input emits UserPromptSubmit, not tool results or continuations."""
    hooks.trigger_hooks("UserPromptSubmit", query, workdir)
    message = {"role": "user", "content": query if parts is None else parts}
    if media_turn:
        message["_media_turn_start"] = True
    messages.append(message)
