"""Account for every changed side separately from optional repository context."""

from homebrew_harness.execution.source_snapshot import matches
from homebrew_harness.security.file_visibility import host_managed_parts


def coverage(changed, files, base_files, context_skipped, policy, size):
    entries = []
    skipped = []
    seen = set()
    for item in changed:
        path = item.get("filename")
        previous = item.get("previous_filename", path)
        for name in (path, previous):
            if (
                not isinstance(name, str)
                or name.startswith("/")
                or "\\" in name
                or any(p in {"", ".", ".."} for p in name.split("/"))
            ):
                raise ValueError("Invalid changed-file coverage path.")
        if path in seen:
            raise ValueError("Duplicate changed-file coverage.")
        seen.add(path)
        status = item.get("status")
        reasons = []
        if status not in {
            "added",
            "removed",
            "modified",
            "renamed",
            "copied",
            "changed",
        }:
            reasons.append("unsupported_change")
        sides = []
        if status != "removed":
            sides.append(("head", path, files))
        if status != "added":
            sides.append(("base", previous, base_files))
        for side, name, available in sides:
            if host_managed_parts(tuple(name.split("/"))) or not matches(
                name, policy["review_globs"]
            ):
                reasons.append(side + "_outside_review_scope")
            elif name not in available:
                reasons.append(side + "_unavailable")
        if not isinstance(item.get("patch"), str):
            reasons.append("diff_unavailable")
        entries.append(
            {
                "path": path,
                "previous_path": previous,
                "status": status,
                "complete": not reasons,
                "reasons": reasons,
            }
        )
        skipped.extend({"path": path, "reason": reason} for reason in reasons)
    return {
        "paths": len(files),
        "bytes": size,
        "changes": entries,
        "skipped": skipped,
        "context_skipped": context_skipped,
        "complete": not skipped,
    }
