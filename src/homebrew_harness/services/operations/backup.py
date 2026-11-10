"""Quiescent service backup sets and guarded restoration of their original roots."""

import hashlib
import json
import os
import re
import sqlite3
from contextlib import ExitStack
from pathlib import Path

from homebrew_harness.execution.execution_control import check
from homebrew_harness.security.safe_files import SafeFile, canonical_root
from homebrew_harness.storage.durable_runtime import DurableStore
from homebrew_harness.storage.storage_admin import maintenance
from .retention import roots, business_fingerprint, refresh


def location(manager, identity):
    if not isinstance(identity, str) or not re.fullmatch(r"op_[0-9a-f]{32}", identity):
        raise ValueError("Invalid backup identity.")
    if not manager.spec["backup_directory"]:
        raise ValueError("Configure an external backup directory first.")
    root = canonical_root(Path(manager.spec["backup_directory"]))
    if root.stat().st_mode & 0o077:
        raise ValueError("Backup directory must be owner-only.")
    return root / identity


def read(root, path, limit):
    with SafeFile(root, path) as file:
        raw, _, _ = file.snapshot(max_bytes=limit)
    if raw is None:
        raise ValueError("Backup member is missing.")
    return raw


def write(root, path, raw, mode=0o600):
    with SafeFile(root, path, create_parents=True) as file:
        file.publish(raw, file.snapshot(max_bytes=max(len(raw), 4_000_000))[1], mode)


def verify(manager, path):
    root = canonical_root(path)
    manifest = json.loads(read(root, "manifest.json", 8_000_000))
    if (
        not isinstance(manifest, dict)
        or manifest.get("format") != 1
        or manifest.get("service_id") != manager.runtime.id
        or not isinstance(manifest.get("files"), dict)
        or not isinstance(manifest.get("roots"), dict)
    ):
        raise ValueError("Backup belongs to another service or format.")
    if len(manifest["files"]) > manager.spec["max_backup_files"]:
        raise ValueError("Backup file count exceeds the declared bound.")
    total = 0
    for name, entry in manifest["files"].items():
        check()
        raw = read(root, name, manager.spec["max_backup_bytes"] - total)
        total += len(raw)
        if (
            total > manager.spec["max_backup_bytes"]
            or len(raw) != entry["bytes"]
            or hashlib.sha256(raw).hexdigest() != entry["sha256"]
        ):
            raise ValueError("Backup member differs from its manifest.")
    for name in manifest["roots"]:
        if name + "/database.sqlite3" not in manifest["files"]:
            raise ValueError("Backup lacks a root database.")
        connection = sqlite3.connect(
            (root / name / "database.sqlite3").as_uri() + "?mode=ro", uri=True
        )
        try:
            if (
                connection.execute("PRAGMA quick_check").fetchone()[0] != "ok"
                or connection.execute("PRAGMA user_version").fetchone()[0] != 1
            ):
                raise ValueError("Backup database failed validation.")
        finally:
            connection.close()
    return manifest


def create(manager, row):
    from homebrew_harness.execution.execution_leases import ExecutionLeases

    with ExitStack() as owned:
        for root in roots(manager).values():
            lease = ExecutionLeases(root)
            owned.callback(lease.close)
            for name in (
                "session",
                "workspace:" + str(root),
                "managed-supervisor",
                "managed-services",
            ):
                owned.enter_context(lease.use(name))
        return _create(manager, row)


def _create(manager, row):
    final = location(manager, row["id"])
    if final.exists():
        manifest = verify(manager, final)
        return summary(row["id"], manifest)
    refresh(manager)
    business = business_fingerprint(manager)
    stage = final.parent / (".building-" + row["id"])
    stage.mkdir(mode=0o700, exist_ok=True)
    ownership = {
        "service_id": manager.runtime.id,
        "operation_id": row["id"],
        "business": business,
    }
    if (stage / "owner.json").exists():
        if json.loads(read(stage, "owner.json", 10000)) != ownership:
            raise ValueError("Partial backup inputs changed; inspect before retry.")
    else:
        write(stage, "owner.json", json.dumps(ownership).encode())
    manifest = {
        "format": 1,
        "service_id": manager.runtime.id,
        "operation_id": row["id"],
        "business": business,
        "roots": {},
        "files": {},
    }
    total = 0
    bound_roots = roots(manager)
    for name, root in bound_roots.items():
        check()
        db = DurableStore(root).backend()
        if not db.exists():
            continue
        manifest["roots"][name] = str(root)
        target = stage / name / "database.sqlite3"
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if target.is_symlink():
            raise ValueError("Backup database target cannot be a symlink.")
        connection = sqlite3.connect(target)
        try:
            with db.connection() as source:
                source.backup(connection, pages=256, progress=lambda *_: check())
        finally:
            connection.close()
        target.chmod(0o600)
        data = target.read_bytes()
        total = register(
            manager, manifest, name + "/database.sqlite3", data, 0o600, total
        )
        nested = [
            p
            for label, p in bound_roots.items()
            if label != name and p.is_relative_to(root)
        ]
        for folder in (".harness", ".memory"):
            directory = root / folder
            if not directory.exists():
                continue
            if directory.is_symlink():
                raise ValueError("Backup source directory cannot be a symlink.")
            for parent, dirs, files in os.walk(directory, followlinks=False):
                check()
                relative = Path(parent).relative_to(root)
                dirs[:] = [
                    n
                    for n in dirs
                    if (relative / n).as_posix()
                    not in {
                        ".harness/runtime/database",
                        ".harness/leases",
                        ".harness/control",
                    }
                    and not any((Path(parent) / n).is_relative_to(p) for p in nested)
                ]
                if any((Path(parent) / n).is_symlink() for n in dirs):
                    raise ValueError("Backup source contains an internal symlink.")
                for file in files:
                    if file.endswith(".lock"):
                        continue
                    reference = (relative / file).as_posix()
                    with SafeFile(root, reference) as source:
                        raw, version, mode = source.snapshot(
                            max_bytes=manager.spec["max_backup_bytes"] - total
                        )
                        if (
                            raw is None
                            or source.snapshot(max_bytes=len(raw))[1] != version
                        ):
                            raise ValueError("Backup input changed during capture.")
                    member = name + "/attachments/" + reference
                    total = register(manager, manifest, member, raw, mode, total)
                    write(stage, member, raw, mode)
    if business_fingerprint(manager) != business:
        raise ValueError("Service state changed during backup.")
    write(stage, "manifest.json", json.dumps(manifest, sort_keys=True).encode())
    verify(manager, stage)
    if final.exists():
        raise ValueError("Backup target appeared before publication.")
    os.rename(stage, final)
    return summary(row["id"], manifest)


def register(manager, manifest, name, raw, mode, total):
    total += len(raw)
    if (
        total > manager.spec["max_backup_bytes"]
        or len(manifest["files"]) >= manager.spec["max_backup_files"]
    ):
        raise ValueError("Service backup exceeds host limits.")
    manifest["files"][name] = {
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "mode": mode,
    }
    return total


def summary(identity, manifest):
    return {
        "backup_id": identity,
        "files": len(manifest["files"]),
        "bytes": sum(v["bytes"] for v in manifest["files"].values()),
        "business": manifest["business"],
        "manifest_hash": hashlib.sha256(
            json.dumps(manifest, sort_keys=True).encode()
        ).hexdigest(),
    }


def restore(manager, row, manifest, source):
    if manifest["business"] != business_fingerprint(manager):
        raise ValueError("New business facts prevent restoring this backup.")
    actual = {
        name: str(root)
        for name, root in roots(manager).items()
        if DurableStore(root).backend().exists()
    }
    if actual != manifest["roots"]:
        raise ValueError(
            "Restore requires the original service workspace relationships."
        )
    journal = ".harness/service_operations/restore/" + row["id"] + ".json"
    with SafeFile(manager.store.root, journal, create_parents=True) as file:
        raw, version, _ = file.snapshot(max_bytes=1_000_000)
        state = (
            json.loads(raw)
            if raw
            else {
                "backup_id": row["payload"]["id"],
                "completed": [],
                "state": "started",
            }
        )
        if state["backup_id"] != row["payload"]["id"]:
            raise ValueError("Restore journal belongs to another backup.")
        file.publish(json.dumps(state).encode(), version, 0o600)
    # Preserve controls and accepted maintenance receipts independently of backup age.
    with manager.store.db.connection() as conn:
        overlay = [
            (section, key, value)
            for section, key, value in conn.execute(
                "SELECT section,key,value FROM records WHERE section IN ('service_meta','service_operations','service_incidents','service_commands','service_upgrade_plans','service_retention_plans')"
            )
        ]
    with ExitStack() as resources:
        for root in sorted(map(Path, actual.values())):
            resources.enter_context(maintenance(root))
        for name, root_str in actual.items():
            root = Path(root_str)
            if name in state["completed"]:
                continue
            check()
            for member, entry in manifest["files"].items():
                prefix = name + "/attachments/"
                if member.startswith(prefix):
                    write(
                        root,
                        member[len(prefix) :],
                        read(source, member, manager.spec["max_backup_bytes"]),
                        entry["mode"],
                    )
            target_db = DurableStore(root).backend()
            # Backup API updates the existing inode and preserves SQLite binding contracts.
            origin = source / name / "database.sqlite3"
            if name == "control":
                stage = source.parent / (".restore-" + row["id"] + ".sqlite3")
                if stage.is_symlink():
                    raise ValueError("Restore staging database cannot be a symlink.")
                with manager.runtime.lock:
                    manager.restoring = True
                    with manager.store.db.connection() as conn:
                        overlay = [
                            tuple(r)
                            for r in conn.execute(
                                "SELECT section,key,value,owner,revision FROM records WHERE section IN ('service_meta','service_operations','service_incidents','service_commands','service_upgrade_plans','service_retention_plans')"
                            )
                        ]
                src = sqlite3.connect(origin.as_uri() + "?mode=ro", uri=True)
                dst = sqlite3.connect(stage)
                try:
                    src.backup(
                        dst,
                        pages=256,
                        progress=lambda *_: check(cancel_events=(manager.stop,)),
                    )
                    with dst:
                        for section, key, value, owner, revision in overlay:
                            dst.execute(
                                "INSERT INTO sections(name) VALUES(?) ON CONFLICT DO NOTHING",
                                (section,),
                            )
                            dst.execute(
                                "INSERT INTO records(section,key,value,owner,revision) VALUES(?,?,?,?,?) ON CONFLICT(section,key) DO UPDATE SET value=excluded.value,owner=excluded.owner,revision=excluded.revision",
                                (section, key, value, owner, revision),
                            )
                finally:
                    src.close()
                    dst.close()
                stage.chmod(0o600)
                origin = stage
            old = sqlite3.connect(origin.as_uri() + "?mode=ro", uri=True)
            try:
                with target_db.connection() as target:
                    old.backup(
                        target,
                        pages=256,
                        progress=lambda *_: check(cancel_events=(manager.stop,)),
                    )
            finally:
                old.close()
                if name == "control":
                    manager.restoring = False
            target_db.cache.clear()
            if name == "control":
                manager.store.db.cache.clear()
            for child in manager.runtime.children.values():
                child.store.backend().cache.clear()
                child.run_store.runtime.backend().cache.clear()
            state["completed"].append(name)
            write(manager.store.root, journal, json.dumps(state).encode())
        state["state"] = "completed"
        write(manager.store.root, journal, json.dumps(state).encode())
    return {
        "restored": True,
        "backup_id": row["payload"]["id"],
        "roots": list(actual),
        "journal": journal,
    }


def perform(manager, row):
    if (
        row["kind"] != "verify_backup"
        and manager.store.get(manager.runtime.id).get("operations_phase") != "parked"
    ):
        raise ValueError("Backup operations require actual parked writers.")
    if row["kind"] == "backup":
        return create(manager, row)
    backup = manager.get(row["payload"]["id"])
    if backup["kind"] != "backup" or backup["state"] != "completed":
        raise ValueError("Use a completed service backup.")
    source = location(manager, backup["id"])
    manifest = verify(manager, source)
    if (
        summary(backup["id"], manifest)["manifest_hash"]
        != backup["result"]["manifest_hash"]
    ):
        raise ValueError("Backup manifest changed since creation.")
    if row["kind"] == "verify_backup":
        return {"verified": True, **summary(backup["id"], manifest)}
    return restore(manager, row, manifest, source)
