"""Explicit, version-checked orphan archive GC with durable quarantine journals."""

import json
import os
import re
import uuid

from homebrew_harness.security.safe_files import SafeFile
from homebrew_harness.storage.artifact_inventory import (
    archive_path,
    inspect_artifacts,
    version,
)
from homebrew_harness.storage.durable_runtime import DurableStore
from homebrew_harness.storage.retention_graph import state_roots
from homebrew_harness.storage.storage_admin import maintenance, publish

_RUN = re.compile(r"[0-9a-f]{32}\Z")


def preview(root, **kwargs):
    return inspect_artifacts(root, **kwargs)


def _move(root, source, destination, expected):
    with (
        SafeFile(root, source) as src,
        SafeFile(root, destination, create_parents=True) as dst,
    ):
        if version(src.snapshot()[1]) != expected:
            raise ValueError("Artifact changed before quarantine.")
        if dst.snapshot()[1] is not None:
            raise ValueError("Quarantine destination already exists.")
        src.verify_binding()
        dst.verify_binding()
        # Exclusive link publication + unlink is recoverable even between the two calls.
        os.link(
            src.parts[-1],
            dst.parts[-1],
            src_dir_fd=src.fds[-1],
            dst_dir_fd=dst.fds[-1],
            follow_symlinks=False,
        )
        os.fsync(dst.fds[-1])
        src.unlink(src.snapshot()[1])


def apply(root, plan):
    if (
        not isinstance(plan, dict)
        or plan.get("format") != 1
        or not isinstance(plan.get("candidates"), list)
    ):
        raise ValueError("Invalid GC preview.")
    with maintenance(root) as root:
        current = inspect_artifacts(root, retention_days=plan["retention_days"])
        candidates = {item["path"]: item for item in current["candidates"]}
        run_id = uuid.uuid4().hex
        journal = {"format": 1, "run_id": run_id, "phase": "planned", "entries": []}
        seen = set()
        for item in plan["candidates"]:
            if (
                not isinstance(item, dict)
                or item != candidates.get(item.get("path"))
                or not archive_path(item.get("path"))
                or item["path"] in seen
            ):
                raise ValueError(
                    "GC preview is stale or an artifact gained a reference; rerun preview."
                )
            seen.add(item["path"])
            journal["entries"].append(
                {
                    **item,
                    "quarantine": ".harness/gc/quarantine/"
                    + run_id
                    + "/"
                    + uuid.uuid4().hex,
                    "phase": "planned",
                }
            )
        reference = ".harness/gc/" + run_id + ".json"
        publish(root, reference, journal)
        for entry in journal["entries"]:
            _move(root, entry["path"], entry["quarantine"], entry["version"])
            entry["phase"] = "quarantined"
            publish(root, reference, journal)
        journal["phase"] = "quarantined"
        publish(root, reference, journal)
        return _finish(root, reference, journal)


def _finish(root, reference, journal):
    state = DurableStore(root).read()
    # Include current Goal/Workflow/pin roots; quarantine files are excluded from candidates.
    current = inspect_artifacts(root)
    roots = state_roots(state, current["references"])
    for item in current["files"]:
        if item["reason"] == "referenced_by_retained_state":
            roots.add(item["path"])
    deleted = restored = 0
    for entry in journal["entries"]:
        if entry["phase"] in {"deleted", "restored"}:
            continue
        with SafeFile(root, entry["quarantine"], create_parents=True) as quarantine:
            raw, identity, _ = quarantine.snapshot()
            if raw is None:
                with SafeFile(root, entry["path"]) as original:
                    original_raw, original_identity, _ = original.snapshot()
                if original_raw is not None:
                    import hashlib

                    if hashlib.sha256(original_raw).hexdigest() != entry["sha256"]:
                        raise ValueError(
                            "Interrupted GC has a changed original; inspect it with the host."
                        )
                    entry["phase"] = "restored"
                elif entry["phase"] == "quarantined":
                    # Deletion may have committed before the journal update.
                    DurableStore(root).remove("artifacts", entry["path"])
                    entry["phase"] = "deleted"
                else:
                    raise ValueError(
                        "Interrupted GC is missing both original and quarantine; inspect it."
                    )
            else:
                import hashlib

                if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
                    raise ValueError("Quarantine content changed; cleanup refused.")
                with SafeFile(root, entry["path"]) as original:
                    original_raw, original_identity, _ = original.snapshot()
                    if original_raw is not None:
                        # Crash between link and unlink: never delete a remaining original.
                        if original_raw != raw:
                            raise ValueError(
                                "Original changed during interrupted quarantine."
                            )
                        quarantine.unlink(identity)
                        entry["phase"] = "restored"
                    elif entry["path"] in roots:
                        original.publish(raw, None, 0o600)
                        quarantine.unlink(identity)
                        entry["phase"] = "restored"
                    else:
                        quarantine.unlink(identity)
                        DurableStore(root).remove("artifacts", entry["path"])
                        entry["phase"] = "deleted"
            deleted += entry["phase"] == "deleted"
            restored += entry["phase"] == "restored"
            publish(root, reference, journal)
    journal["phase"] = "complete"
    publish(root, reference, journal)
    return {
        "run_id": journal["run_id"],
        "phase": "complete",
        "deleted": deleted,
        "restored": restored,
    }


def recover(root, run_id):
    if not isinstance(run_id, str) or not _RUN.fullmatch(run_id):
        raise ValueError("Invalid GC run ID.")
    with maintenance(root) as root:
        reference = ".harness/gc/" + run_id + ".json"
        with SafeFile(root, reference) as target:
            raw, _, _ = target.snapshot(max_bytes=4 * 1024 * 1024)
        journal = json.loads(raw)
        if journal.get("format") != 1 or journal.get("run_id") != run_id:
            raise ValueError("Invalid GC journal.")
        for entry in journal["entries"]:
            if (
                not archive_path(entry.get("path"))
                or not isinstance(entry.get("quarantine"), str)
                or not re.fullmatch(
                    r"\.harness/gc/quarantine/" + run_id + r"/[0-9a-f]{32}",
                    entry["quarantine"],
                )
            ):
                raise ValueError("Invalid GC journal path.")
        if journal.get("phase") == "complete":
            return {"run_id": run_id, "phase": "complete", "already_complete": True}
        return _finish(root, reference, journal)
