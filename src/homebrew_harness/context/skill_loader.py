"""s07: scan a skill catalog once, then expose full instructions by name."""

from __future__ import annotations

from homebrew_harness.runtime.output import print

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import yaml

from homebrew_harness.execution.execution_control import check as execution_check
from homebrew_harness.security.safe_files import SafeFile

NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
DESCRIPTION_LIMIT = 300


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    content: str


class SkillLoader:
    def __init__(self, skills_dir: Path):
        if skills_dir.is_symlink():
            raise ValueError("Skills directory must not be a symlink.")
        self.skills_dir = skills_dir.resolve()
        self._skills: dict[str, Skill] = {}
        self.scan()

    @property
    def skills(self) -> Mapping[str, Skill]:
        return MappingProxyType(self._skills)

    @staticmethod
    def parse_frontmatter(content: str) -> tuple[dict, str]:
        lines = content.splitlines(keepends=True)
        if not lines or lines[0].rstrip("\r\n") != "---":
            return {}, content
        end = next(
            (
                index
                for index, line in enumerate(lines[1:], 1)
                if line.rstrip("\r\n") == "---"
            ),
            None,
        )
        if end is None:
            raise ValueError("Unclosed YAML frontmatter.")
        try:
            metadata = yaml.safe_load("".join(lines[1:end]))
        except yaml.YAMLError as error:
            raise ValueError("Invalid YAML frontmatter.") from error
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            raise ValueError("YAML frontmatter must be a mapping.")
        return metadata, "".join(lines[end + 1 :])

    def _warn(self, manifest: Path, reason: str) -> None:
        details = {
            "file": manifest.relative_to(self.skills_dir).as_posix(),
            "reason": reason,
        }
        print(
            "[skills] Skipped: " + json.dumps(details, ensure_ascii=False),
            file=sys.stderr,
        )

    def scan(self) -> None:
        root = self.skills_dir
        if not root.exists():
            self._skills = {}
            return
        if not root.is_dir():
            raise ValueError("The skills path must be a directory.")

        found: dict[str, Skill] = {}
        seen_paths, conflicts = set(), set()
        for index, manifest in enumerate(sorted(root.glob("*/SKILL.md"))):
            if index >= 1000:
                raise ValueError("Skill catalog exceeds the host entry limit.")
            try:
                execution_check()
                resolved = manifest.resolve()
                if not resolved.is_relative_to(root):
                    self._warn(
                        manifest,
                        "Skill file resolves outside the configured directory.",
                    )
                    continue
                if not resolved.is_file() or resolved in seen_paths:
                    continue
                with (
                    SafeFile(root, str(manifest)) as target,
                    target.open_read() as stream,
                ):
                    raw = stream.read(1_000_001)
                    if len(raw) > 1_000_000:
                        raise ValueError("Skill body exceeds the host byte limit.")
                    target.verify_binding()
                    content = raw.decode("utf-8")
                seen_paths.add(resolved)
                metadata, body = self.parse_frontmatter(content)
                name = metadata.get("name", manifest.parent.name)
                if not isinstance(name, str) or not NAME_PATTERN.fullmatch(
                    name.strip()
                ):
                    raise ValueError(
                        "Skill name must be 1-64 lowercase letters, digits, underscores, or hyphens."
                    )
                name = name.strip()
                description = metadata.get("description", "")
                if not isinstance(description, str):
                    raise ValueError("Skill description must be a string.")
                if not description.strip():
                    description = next(
                        (line for line in body.splitlines() if line.strip()), ""
                    )
                description = " ".join(description.strip().lstrip("#").split())
                if not description:
                    raise ValueError("Skill needs a description or a body heading.")
                if len(description) > DESCRIPTION_LIMIT:
                    description = description[: DESCRIPTION_LIMIT - 3] + "..."
                if name in found or name in conflicts:
                    found.pop(name, None)
                    conflicts.add(name)
                    self._warn(
                        manifest,
                        f"Duplicate skill name: {name}; all conflicting entries are excluded.",
                    )
                    continue
                found[name] = Skill(name, description, content)
            except UnicodeError:
                self._warn(manifest, "Skill file must be valid UTF-8.")
            except ValueError as error:
                self._warn(manifest, str(error))
            except (OSError, RuntimeError) as error:
                self._warn(
                    manifest, f"Cannot read skill file ({type(error).__name__})."
                )
        self._skills = found

    def catalog(self) -> str:
        if not self._skills:
            return "(no skills found)"
        return "\n".join(
            f"- {name}: {self._skills[name].description}"
            for name in sorted(self._skills)
        )

    def load(self, name: str) -> str:
        skill = self._skills.get(name)
        if skill is None:
            available = ", ".join(sorted(self._skills)) or "none"
            raise ValueError(
                f"Unknown skill {json.dumps(name, ensure_ascii=False)}. Available: {available}."
            )
        return skill.content

    def run_tool(self, name: str, workdir: Path) -> str:
        """Read the registered snapshot, never interpret name as a file path."""
        return self.load(name)
