"""Quiescent Run exports and consistent database/attachment backup bundles."""

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path

from homebrew_harness.execution.execution_control import check
from homebrew_harness.execution.execution_leases import ExecutionLeases
from homebrew_harness.security.data_boundary import redact
from homebrew_harness.security.safe_files import SafeFile, canonical_root
from homebrew_harness.storage.storage_admin import maintenance
from .spec import RunStopped


def require_space(root, minimum):
    if minimum and shutil.disk_usage(root).free < minimum:
        raise RunStopped("storage_space_low")


@contextmanager
def parked(store, run_id):
    leases = ExecutionLeases(store.root)
    try:
        with (
            leases.use("managed-supervisor"),
            leases.use("managed-services"),
            maintenance(store.root),
        ):
            run = store.get(run_id)
            if not run.get("cleanup_complete") or run["status"] not in {
                "waiting",
                "paused",
                "blocked",
                "completed",
                "cancelled",
                "failed",
            }:
                raise ValueError(
                    "Stop the supervisor and finish execution cleanup before maintenance."
                )
            from .supervisor import RunSupervisor

            supervisor = RunSupervisor(store)
            try:
                if supervisor.worker_alive(run) or not supervisor.recovery_safe(
                    run, execution_owned=True
                ):
                    raise ValueError(
                        "Maintenance requires settled operations and processes."
                    )
            finally:
                supervisor.close()
            yield run
    finally:
        leases.close()


def export_run(store, run_id):
    run = store.get(run_id)
    sections = (
        "tasks",
        "stage_submissions",
        "stage_handoffs",
        "run_requests",
        "run_decisions",
        "run_events",
        "run_outbox",
        "run_attempts",
        "run_episodes",
        "run_handoffs",
        "run_transfers",
        "run_members",
        "run_member_outcomes",
        "stage_inputs",
    )
    state = {"format": 1, "run": run, "records": {}}
    for name in sections:
        values = store.runtime.items(name)
        state["records"][name] = {
            key: value
            for key, value in values.items()
            if (value.get("stage") or value).get("run_id") == run_id
        }
    # References make full evidence retrievable without exporting protocol-private history.
    state["sessions"] = run.get("session_refs", [run["session_id"]])
    state["records"]["input_receipts"] = {
        key: value
        for key, value in store.runtime.items("input_receipts").items()
        if value.get("checkpoint") in {"lead:" + sid for sid in state["sessions"]}
    }
    return redact(state)


def _destination(path, workspace):
    path = Path(path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError("Export and backup destinations cannot contain symlinks.")
    path = path.resolve()
    if path.is_relative_to(workspace):
        raise ValueError("Keep exports and backups outside the working repository.")
    if path.exists():
        raise ValueError(
            "Choose a new output path; existing files are not overwritten."
        )
    if not path.parent.is_dir():
        raise ValueError("Create the output parent directory first.")
    return path


def write_export(store, run_id, output):
    target = _destination(output, store.root)
    raw = json.dumps(export_run(store, run_id), ensure_ascii=False, indent=2).encode()
    with SafeFile(target.parent, target.name) as safe:
        safe.publish(raw, None, 0o600)
    return {
        "exported": True,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def backup_run(store, run_id, output, *, max_bytes=256_000_000, max_files=20000):
    target = _destination(output, store.root)
    temporary = Path(tempfile.mkdtemp(prefix=".harness-backup-", dir=target.parent))
    try:
        with parked(store, run_id) as run:
            manifest = {
                "format": 1,
                "run_id": run_id,
                "files": {},
                "database": "runtime.sqlite3",
                "sessions": run.get("session_refs", [run["session_id"]]),
            }
            database = temporary / manifest["database"]
            fd = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            destination = sqlite3.connect(database)
            try:
                with store.runtime.backend().connection() as source:
                    source.backup(destination, pages=256, progress=lambda *_: check())
                if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("Backup database verification failed.")
            finally:
                destination.close()
            total = database.stat().st_size
            if total > max_bytes:
                raise ValueError("Backup database exceeds its byte limit.")
            manifest["files"]["runtime.sqlite3"] = {
                "bytes": total,
                "sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
            }
            for folder in (".harness", ".memory"):
                root = store.root / folder
                if not root.exists():
                    continue
                for parent, dirs, files in os.walk(root, followlinks=False):
                    check()
                    relative = Path(parent).relative_to(store.root)
                    dirs[:] = [
                        name
                        for name in dirs
                        if (relative / name).as_posix()
                        not in {".harness/runtime/database", ".harness/leases"}
                    ]
                    if any((Path(parent) / name).is_symlink() for name in dirs):
                        raise ValueError(
                            "Backup attachments contain a symlink directory."
                        )
                    for name in files:
                        if name.endswith(".lock"):
                            continue
                        reference = (relative / name).as_posix()
                        if len(manifest["files"]) >= max_files:
                            raise ValueError("Backup file count limit reached.")
                        with SafeFile(store.root, reference) as item:
                            raw, before, _ = item.snapshot(
                                max_bytes=max(0, max_bytes - total)
                            )
                            if raw is None:
                                raise ValueError("Backup attachment disappeared.")
                            total += len(raw)
                            if (
                                total > max_bytes
                                or item.snapshot(max_bytes=len(raw))[1] != before
                            ):
                                raise ValueError(
                                    "Backup attachments exceed limits or changed."
                                )
                        dest = temporary / "attachments" / reference
                        dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        dest.write_bytes(raw)
                        os.chmod(dest, 0o600)
                        manifest["files"][str(dest.relative_to(temporary))] = {
                            "bytes": len(raw),
                            "sha256": hashlib.sha256(raw).hexdigest(),
                        }
            # An explicit logical export describes code/worktree identities; this bundle
            # restores control-plane facts, not arbitrary Git worktree paths on a new host.
            exported = json.dumps(
                export_run(store, run_id), ensure_ascii=False, indent=2
            ).encode()
            (temporary / "run.json").write_bytes(exported)
            os.chmod(temporary / "run.json", 0o600)
            manifest["files"]["run.json"] = {
                "bytes": len(exported),
                "sha256": hashlib.sha256(exported).hexdigest(),
            }
            (temporary / "manifest.json").write_text(json.dumps(manifest, indent=2))
            os.chmod(temporary / "manifest.json", 0o600)
            verify_backup(temporary)
            if target.exists():
                raise ValueError("Backup destination appeared during creation.")
            os.rename(temporary, target)
            return {
                "backed_up": True,
                "files": len(manifest["files"]),
                "bytes": sum(v["bytes"] for v in manifest["files"].values()),
            }
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def verify_backup(directory):
    root = canonical_root(Path(directory))
    with SafeFile(root, "manifest.json") as target:
        raw, _, _ = target.snapshot(max_bytes=4_000_000)
    manifest = json.loads(raw)
    if (
        manifest.get("format") != 1
        or not isinstance(manifest.get("files"), dict)
        or not 1 <= len(manifest["files"]) <= 20002
    ):
        raise ValueError("Invalid backup manifest.")
    total = 0
    for path, record in manifest["files"].items():
        check()
        if (
            not isinstance(record, dict)
            or set(record) != {"bytes", "sha256"}
            or type(record["bytes"]) is not int
            or record["bytes"] < 0
            or not isinstance(record["sha256"], str)
            or Path(path).is_absolute()
            or ".." in Path(path).parts
        ):
            raise ValueError("Invalid backup member path.")
        with SafeFile(root, path) as target:
            data, _, _ = target.snapshot(max_bytes=max(0, 256_000_000 - total))
        total += len(data or b"")
        if total > 256_000_000:
            raise ValueError("Backup contents exceed the verification limit.")
        if (
            data is None
            or len(data) != record["bytes"]
            or hashlib.sha256(data).hexdigest() != record["sha256"]
        ):
            raise ValueError("Backup member does not match its manifest.")
    if (
        "runtime.sqlite3" not in manifest["files"]
        or "run.json" not in manifest["files"]
    ):
        raise ValueError("Backup lacks its database or run export.")
    conn = sqlite3.connect((root / "runtime.sqlite3").as_uri() + "?mode=ro", uri=True)
    try:
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Backup database is invalid.")
        record = conn.execute(
            "SELECT value FROM records WHERE section='runs' AND key=?",
            (manifest["run_id"],),
        ).fetchone()
        if not record:
            raise ValueError("Backup does not contain its declared Run.")
    finally:
        conn.close()
    return {
        "valid": True,
        "run_id": manifest["run_id"],
        "files": len(manifest["files"]),
    }


def restore_backup(directory, output):
    """Restore a verified control-plane bundle for inspection in a new directory."""
    source = canonical_root(Path(directory))
    verified = verify_backup(source)
    target = _destination(output, source)
    temporary = Path(tempfile.mkdtemp(prefix=".harness-restore-", dir=target.parent))
    try:
        with SafeFile(source, "manifest.json") as item:
            raw, _, _ = item.snapshot(max_bytes=4_000_000)
        manifest = json.loads(raw)
        for name, record in manifest["files"].items():
            if name == "run.json":
                relative = "RESTORED-RUN.json"
            elif name == "runtime.sqlite3":
                relative = ".harness/runtime/database/runtime.sqlite3"
            elif name.startswith("attachments/"):
                relative = name[len("attachments/") :]
                if not relative.startswith((".harness/", ".memory/")):
                    raise ValueError(
                        "Restore attachment is outside the control-plane roots."
                    )
            else:
                raise ValueError("Unexpected backup member.")
            with SafeFile(source, name) as item:
                data, _, _ = item.snapshot(max_bytes=record["bytes"])
            if (
                len(data) != record["bytes"]
                or hashlib.sha256(data).hexdigest() != record["sha256"]
            ):
                raise ValueError("Backup changed during restore.")
            with SafeFile(temporary, relative, create_parents=True) as item:
                item.publish(data, None, 0o600)
        # Workspace, process and Git identities deliberately require inspection.
        # The original bundle remains intact; restored tasks are never auto-started.
        from .store import RunStore

        restored = RunStore(temporary)
        try:
            db = restored.runtime.backend()
            with db.write() as conn:
                rows = conn.execute(
                    "SELECT key,value FROM records WHERE section='runs'"
                ).fetchall()
                for key, raw in rows:
                    run = json.loads(raw)
                    if run["status"] not in {"completed", "failed", "cancelled"}:
                        db._put(
                            conn,
                            "runs",
                            key,
                            {
                                **run,
                                "status": "blocked",
                                "cleanup_complete": False,
                                "revision": run["revision"] + 1,
                                "restore_required": True,
                            },
                        )
            db.set_meta("restore_required", True)
        finally:
            restored.close()
        if target.exists():
            raise ValueError("Restore destination appeared during reconstruction.")
        os.rename(temporary, target)
        return {
            "restored": True,
            "run_id": verified["run_id"],
            "inspection_required": True,
        }
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
