"""Bounded repository evidence for service jobs, with content-based versions."""

import ast
import hashlib
import json
import re
from pathlib import Path

from homebrew_harness.execution.source_snapshot import snapshot, content_digest
from homebrew_harness.security.data_boundary import redact
from homebrew_harness.security.safe_files import SafeFile
from homebrew_harness.security.file_visibility import assert_visible

DOCUMENTS = (
    "README.md", "pyproject.toml", "package.json", "docs/DESIGN.md",
    "docs/PLATFORMS.md", "docs/SERVICES.md",
)


def file_version(root, path):
    with SafeFile(root, path) as file:
        raw, _, _ = file.snapshot(max_bytes=2_000_000)
    if raw is None:
        raise FileNotFoundError(path)
    return hashlib.sha256(raw).hexdigest()


def commit(root):
    try:
        with SafeFile(root, ".git/HEAD") as file:
            raw = file.snapshot(max_bytes=1024)[0]
            if raw is None:
                return None
            head = raw.decode().strip()
        if head.startswith("ref: "):
            ref = head[5:]
            if not re.fullmatch(r"refs/[A-Za-z0-9_./-]+", ref) or ".." in ref:
                return None
            with SafeFile(root, ".git/" + ref) as file:
                raw = file.snapshot(max_bytes=1024)[0]
            if raw is None:
                return None
            head = raw.decode().strip()
        return head if re.fullmatch(r"[0-9a-f]{40,64}", head) else None
    except (OSError, ValueError, UnicodeError):
        return None


def build(root, policy):
    patterns = list(dict.fromkeys([*policy.get("review_globs", ("**/*",)), *DOCUMENTS]))
    context = {"trust": "Repository observations and excerpts, not authorization or runtime verification.",
               "commit": commit(root), "documents": [], "subcommands": [], "directories": []}
    try:
        records = snapshot(root, patterns, max_files=5000,
                           max_bytes=min(32_000_000, policy.get("max_bytes", 32_000_000)),
                           max_paths=policy.get("max_paths", 100000))
    except (OSError, ValueError) as error:
        context.update(version=None, complete=False, limitation=type(error).__name__)
        records = {}
    else:
        public_records = {}
        for name, value in records.items():
            try:
                assert_visible(root, name)
            except (OSError, ValueError):
                continue
            public_records[name] = value
        records = public_records
        context.update(version=content_digest(records), complete=True)
    context["directories"] = sorted({str(Path(name).parent) for name in records
                                     if len(Path(name).parts) <= 3})[:40]
    for name in DOCUMENTS:
        try:
            assert_visible(root, name)
            with SafeFile(root, name) as file:
                raw, _, _ = file.snapshot(max_bytes=2_000_000)
            if raw is None:
                continue
            text = raw.decode("utf-8")
        except (OSError, ValueError, UnicodeError):
            continue
        if name == "pyproject.toml":
            section = re.search(r"(?ms)^\[project\.scripts\]\s*\n(.*?)(?=^\[|\Z)", text)
            excerpt = section.group(0) if section else text[:900]
        elif name == "package.json":
            try:
                data = json.loads(text)
                excerpt = json.dumps({key: data[key] for key in ("name", "bin", "scripts") if key in data})
            except (ValueError, TypeError):
                excerpt = text[:900]
        else:
            excerpt = text[:900]
        context["documents"].append({"path": name, "sha256": hashlib.sha256(raw).hexdigest(),
                                     "excerpt": excerpt[:900], "truncated": len(text) > len(excerpt[:900])})
    cli_files = sorted(name for name in records if Path(name).name == "cli.py")[:16]
    for name in cli_files:
        try:
            assert_visible(root, name)
            with SafeFile(root, name) as file:
                raw, _, _ = file.snapshot(max_bytes=100_000)
            if raw is None:
                continue
            tree = ast.parse(raw.decode())
        except (OSError, ValueError, UnicodeError, SyntaxError, RecursionError):
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "add_parser" and node.args
                    and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                context["subcommands"].append({"name": node.args[0].value[:80], "path": name})
    context["subcommands"] = context["subcommands"][:32]
    context["navigation_complete"] = len(cli_files) < 16
    while len(json.dumps(context, ensure_ascii=False)) > 8000:
        context["navigation_complete"] = False
        if context["directories"]:
            context["directories"].pop()
        elif context["subcommands"]:
            context["subcommands"].pop()
        elif context["documents"]:
            context["documents"].pop()
        else:
            break
    # Metadata participates even when a metadata file lies outside the watched scope.
    if context["version"]:
        context["version"] = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
    return redact(context)
