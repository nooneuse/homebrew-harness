"""Fixed, verified PR files and diff anchors, outside mutable project workspaces."""

import hashlib
import re
from pathlib import Path

from homebrew_harness.security.safe_files import SafeFile
from homebrew_harness.platforms.contracts import digest


def anchors(changed):
    result = {}
    for entry in changed:
        path = entry.get("filename")
        patch = entry.get("patch")
        if not isinstance(path, str) or not isinstance(patch, str):
            continue
        left = right = None
        for line in patch.splitlines():
            header = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if header:
                left, right = map(int, header.groups())
                continue
            if left is None or line.startswith("\\"):
                continue
            if line.startswith("-"):
                result.setdefault(path + ":LEFT", []).append(left)
                left += 1
            elif line.startswith("+"):
                result.setdefault(path + ":RIGHT", []).append(right)
                right += 1
            elif line.startswith(" "):
                result.setdefault(path + ":LEFT", []).append(left)
                result.setdefault(path + ":RIGHT", []).append(right)
                left += 1
                right += 1
    return result


def materialize(control, subject, captured, policy):
    identity = "review_" + digest(
        [
            subject,
            captured["basis"],
            captured["tree"],
            policy["review_globs"],
            captured["files"],
            captured.get("base_files", {}),
            captured["changed"],
            captured["coverage"],
            captured.get("modes", {}),
            policy["required_tests"],
        ]
    )
    root = Path(control) / ".harness" / "communication_reviews" / identity
    manifest = {}
    modes = {}
    relative = root.relative_to(Path(control)).as_posix()
    for side in ("head", "base"):
        with SafeFile(
            Path(control),
            relative + "/" + side + "/.harness/.review-root",
            create_parents=True,
        ) as directory:
            data, _, _ = directory.snapshot(max_bytes=1)
            if data is None:
                directory.publish(b"", None, 0o400)
    for side, files in (
        ("head", captured["files"]),
        ("base", captured.get("base_files", {})),
    ):
        for path, content in files.items():
            data = content.encode()
            key = side + "/" + path
            mode = (
                0o500
                if side == "head" and captured.get("modes", {}).get(path) == "100755"
                else 0o400
            )
            with SafeFile(
                Path(control), relative + "/" + key, create_parents=True
            ) as target:
                before, _, version = target.snapshot(max_bytes=policy["max_file_bytes"])
                if before is not None and before != data:
                    raise ValueError("Fixed PR snapshot content changed.")
                if before is None:
                    target.publish(data, None, mode)
            manifest[key] = hashlib.sha256(data).hexdigest()
            modes[key] = mode
    return {
        "id": identity,
        "subject": subject,
        "basis": captured["basis"],
        "tree": captured["tree"],
        "merge_base": captured["merge_base"],
        "workspace": str(root / "head"),
        "root": str(root),
        "manifest": manifest,
        "modes": modes,
        "changed": captured["changed"],
        "anchors": anchors(captured["changed"]),
        "coverage": captured["coverage"],
        "state": "ready",
        "tests": [],
        "tests_passed": False,
    }


def verify(snapshot, policy):
    root = Path(snapshot["root"])
    for key, expected in snapshot["manifest"].items():
        with SafeFile(root, key) as file:
            data, _, mode = file.snapshot(max_bytes=policy["max_file_bytes"])
        if data is None or hashlib.sha256(data).hexdigest() != expected:
            raise ValueError("Fixed PR review snapshot changed.")
        if mode != snapshot["modes"][key]:
            raise ValueError("Fixed PR review file mode changed.")
