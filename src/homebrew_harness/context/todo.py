"""s05: a validated, session-local plan with no execution or persistence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

MAX_TODOS = 20
STATUSES = ("pending", "in_progress", "completed")
REMINDER_INTERVAL = 3
TODO_REMINDER = (
    "<reminder>Update your todos to reflect current progress. Use todo_write for "
    "multi-step work, and mark steps completed only after verifying their results.</reminder>"
)
TODO_INPUT_SCHEMA = {
    "type": "array",
    "description": "Use an array of items.",
    "maxItems": MAX_TODOS,
    "items": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "minLength": 1, "pattern": r"\S"},
            "status": {"type": "string", "enum": list(STATUSES)},
        },
        "required": ["content", "status"],
        "additionalProperties": False,
    },
}


@dataclass(frozen=True)
class TodoItem:
    content: str
    status: str


class TodoManager:
    def __init__(self):
        self._items: tuple[TodoItem, ...] = ()
        self._revision = 0

    @property
    def items(self) -> tuple[TodoItem, ...]:
        return self._items

    @property
    def revision(self) -> int:
        return self._revision

    def update(self, todos: list) -> str:
        """Validate everything before replacing the current list."""
        if not isinstance(todos, list):
            raise ValueError("todos must be an array.")
        if len(todos) > MAX_TODOS:
            raise ValueError(f"At most {MAX_TODOS} todos are allowed.")

        validated = []
        for index, item in enumerate(todos):
            if not isinstance(item, dict) or set(item) != {"content", "status"}:
                raise ValueError(
                    f"todos[{index}] must contain exactly content and status."
                )
            content, status = item["content"], item["status"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError(f"todos[{index}].content must be a non-empty string.")
            if not isinstance(status, str) or status not in STATUSES:
                raise ValueError(
                    f"todos[{index}].status must be pending, in_progress, or completed."
                )
            validated.append(TodoItem(content.strip(), status))
        if sum(item.status == "in_progress" for item in validated) > 1:
            raise ValueError("At most one todo may be in_progress.")

        self._items = tuple(validated)
        self._revision += 1
        return self.render()

    def render(self) -> str:
        if not self._items:
            return "No todos."
        markers = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}
        lines = []
        for item in self._items:
            # Keep each item on one terminal line and escape control characters.
            label = json.dumps(item.content, ensure_ascii=False)[1:-1]
            lines.append(f"{markers[item.status]} {label}")
        done = sum(item.status == "completed" for item in self._items)
        return "\n".join([*lines, f"({done}/{len(self._items)} completed)"])

    def run_tool(self, todos: list, workdir: Path) -> str:
        """Match the common handler signature; workdir does not affect this plan."""
        plan = self.update(todos)
        return json.dumps(
            {
                "todos": [
                    {"content": item.content, "status": item.status}
                    for item in self._items
                ],
                "completed": sum(item.status == "completed" for item in self._items),
                "total": len(self._items),
                "plan": plan,
            },
            ensure_ascii=False,
        )
