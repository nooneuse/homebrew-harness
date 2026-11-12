"""Parse host command syntax once; authorization and state checks stay in execution."""

import json
import re
import shlex
from dataclasses import dataclass

from homebrew_harness.context.memory import NAME_PATTERN
from homebrew_harness.execution.test_runner import TestRunnerError
from homebrew_harness.orchestration.hard_conditions import validate_spec


@dataclass(frozen=True)
class HostCommand:
    query: str
    name: str = ""
    action: str = ""
    argument: str = ""
    mode: str = "execute"
    values: tuple = ()

    @property
    def local(self):
        return self.mode != "execute"

    @property
    def extended(self):
        return self.name in {"model", "reasoning", "compact", "new", "diff", "review", "skills", "help"}

    @property
    def allowed_during_recovery(self):
        return (
            self.name in {"usage", "storage", "recovery", "mcp", "inputs", "help", "diff"}
            or self.name in {"model", "reasoning", "skills"} and self.mode == "read"
            or self.name == "goal"
            and self.local
        )

    @classmethod
    def parse(cls, query):
        if not isinstance(query, str):
            raise ValueError("User query must be text.")
        matched = re.fullmatch(r"/([a-z]+)(?:\s+(.*))?", query.lstrip(), re.DOTALL)
        if matched is None or matched[1] not in {
            "inputs",
            "usage",
            "storage",
            "recovery",
            "tests",
            "memory",
            "goal",
            "task",
            "mcp",
            "model", "reasoning", "compact", "new", "diff", "review", "skills", "help",
        }:
            if query.lstrip().startswith("/"):
                raise ValueError("Unknown host command; use /help.")
            return cls(query)
        name, raw = matched[1], matched[2] or ""
        argument = raw.strip()
        if name in {"help", "new", "compact"}:
            if argument:
                raise ValueError("/" + name + " accepts no arguments.")
            return cls(query, name, "run", mode="context" if name == "compact" else "manage" if name == "new" else "read")
        if name in {"model", "reasoning"}:
            if not argument:
                return cls(query, name, "status", mode="read")
            parts = shlex.split(argument)
            if len(parts) != 1:
                raise ValueError("Use /" + name + " VALUE.")
            return cls(query, name, "select", parts[0], mode="manage")
        if name in {"diff", "review"}:
            parts = shlex.split(argument)
            base, paths = None, []
            if parts[:1] == ["--base"]:
                if len(parts) < 2:
                    raise ValueError("--base requires a Git revision.")
                base, parts = parts[1], parts[2:]
            if parts[:1] == ["--"]:
                parts = parts[1:]
            if any(path.startswith("--") for path in parts):
                raise ValueError("Use /" + name + " [--base REF] [PATH ...].")
            paths = tuple(parts)
            return cls(query, name, "run", mode="read" if name == "diff" else "context", values=(base, paths))
        if name == "skills":
            parts = shlex.split(argument)
            if not parts:
                return cls(query, name, "list", mode="read")
            if len(parts) == 2 and parts[0] == "show":
                return cls(query, name, "show", parts[1], mode="read")
            match = re.fullmatch(r"use\s+(\S+)\s+(.+)", argument, re.DOTALL)
            if match:
                return cls(query, name, "use", match[1], mode="context", values=(match[2],))
            raise ValueError("Use /skills, /skills show NAME or /skills use NAME TASK.")
        if name == "inputs":
            if not argument:
                return cls(query, name, "status", mode="read")
            parts = argument.split()
            if (
                len(parts) != 2
                or parts[0] not in {"cancel", "retry"}
                or not re.fullmatch(r"usr_[0-9a-f]{32}", parts[1])
            ):
                raise ValueError("Use /inputs, /inputs cancel ID or /inputs retry ID.")
            return cls(query, name, parts[0], parts[1], mode="manage")
        if name in {"usage", "storage"}:
            if argument:
                raise ValueError("/" + name + " accepts no arguments.")
            return cls(query, name, "status", mode="read")
        if name == "mcp":
            if not argument:
                return cls(query, name, "status", mode="read")
            close = re.fullmatch(r"close\s+(mcp_[0-9a-f]{32})", argument)
            if close:
                return cls(query, name, "close", close[1], mode="manage")
            raise ValueError("Use /mcp or /mcp close CONNECTION_ID.")
        if name == "recovery":
            if not argument:
                return cls(query, name, "status", mode="read")
            if argument == "continue":
                return cls(query, name, "continue", mode="manage")
            parts = argument.split(None, 1)
            inspected = (
                parts[1].rsplit(None, 1)
                if len(parts) == 2 and parts[0] == "inspect"
                else []
            )
            if (
                len(inspected) == 2
                and inspected[0].strip()
                and re.fullmatch(r"[0-9]+", inspected[1])
            ):
                return cls(
                    query,
                    name,
                    "inspect",
                    mode="manage",
                    values=(inspected[0].strip(), int(inspected[1])),
                )
            raise ValueError(
                "Use /recovery, /recovery inspect ID REVISION or /recovery continue."
            )
        if name == "tests":
            if not argument:
                return cls(query, name, "status", mode="read")
            parts = argument.split()
            if len(parts) != 2 or parts[0] not in {"approve", "revoke"}:
                raise TestRunnerError(
                    "Use /tests, /tests approve NAME or /tests revoke NAME."
                )
            return cls(query, name, parts[0], parts[1], mode="manage")
        if name == "memory":
            if not argument:
                return cls(query, name, "list", mode="read")
            parts = raw.lstrip().split(None, 1)
            action = parts[0]
            rest = parts[1] if len(parts) == 2 else ""
            if action == "list" and not rest.strip():
                return cls(query, name, action, mode="read")
            if action in {"show", "delete"} and NAME_PATTERN.fullmatch(rest.strip()):
                return cls(
                    query,
                    name,
                    action,
                    rest.strip(),
                    mode="read" if action == "show" else "manage",
                )
            correction = (
                re.fullmatch(r"(\S+)\s(.*)", rest, re.DOTALL)
                if action == "correct"
                else None
            )
            if (
                correction
                and NAME_PATTERN.fullmatch(correction[1])
                and correction[2].strip()
            ):
                return cls(
                    query,
                    name,
                    action,
                    correction[1],
                    mode="manage",
                    values=(correction[2],),
                )
            raise ValueError(
                "Use /memory list, show NAME, delete NAME, or correct NAME STATEMENT."
            )
        if name == "goal":
            if not argument:
                return cls(query, name, "status", mode="read")
            if argument.lower() in {"clear", "stop", "off", "reset", "none", "cancel"}:
                return cls(query, name, "cancel", mode="manage")
            parts = argument.split(None, 1)
            if parts[0] == "resume":
                if len(parts) == 2 and len(parts[1].split()) != 1:
                    raise ValueError("Use /goal resume or /goal resume ID.")
                return cls(query, name, "resume", parts[1] if len(parts) == 2 else "")
            if parts[0] == "spec":
                if len(parts) != 2:
                    raise ValueError("Use /goal spec JSON.")
                definition = validate_spec(json.loads(parts[1]))
                return cls(query, name, "spec", values=(definition,))
            return cls(query, name, "set", raw)
        if not argument:
            raise ValueError("Use /task TASK DESCRIPTION.")
        return cls(query, name, "new", raw)
