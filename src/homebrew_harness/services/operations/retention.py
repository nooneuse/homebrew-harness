"""Service references extend the existing GC roots across owned Run workspaces."""

import hashlib
import json
import os
import shutil
import stat
import time
from pathlib import Path

from homebrew_harness.execution.execution_control import check
from homebrew_harness.security.safe_files import SafeFile, canonical_root
from homebrew_harness.services.protocol import fingerprint

TERMINAL = {"delivered", "cancelled", "rejected", "merged", "failed", "deferred"}


def records(manager, section):
    with manager.store.db.connection() as conn:
        return [
            json.loads(r[0])
            for r in conn.execute(
                "SELECT value FROM records WHERE section=? AND owner=?",
                ("service_" + section, "service:" + manager.runtime.id),
            )
        ]


def roots(manager):
    values = {
        "control": manager.store.root,
        "workspace": Path(manager.runtime.spec.workspace),
    }
    for work in records(manager, "work"):
        for location in [
            work.get("run_workspace"),
            *work.get("run_locations", {}).values(),
        ]:
            if not location:
                continue
            root = canonical_root(Path(location))
            if root == values["workspace"]:
                continue
            if not root.is_relative_to(
                manager.store.root / ".harness/delivery/repairs"
            ):
                raise ValueError("Service execution location escapes its owner.")
            values["repair_" + hashlib.sha256(str(root).encode()).hexdigest()[:16]] = (
                root
            )
    return values


def business_fingerprint(manager):
    result = {}
    for name, root in roots(manager).items():
        from homebrew_harness.storage.durable_runtime import DurableStore

        db = DurableStore(root).backend()
        rows = []
        if db.exists():
            with db.connection() as conn:
                for section, key, raw in conn.execute(
                    "SELECT section,key,value FROM records ORDER BY section,key"
                ):
                    if section in {
                        "service_meta",
                        "service_operations",
                        "service_incidents",
                        "service_backups",
                        "service_retention_plans",
                        "service_upgrade_plans",
                        "service_commands",
                        "service_health",
                    }:
                        continue
                    if section == "service_audits":
                        continue
                    rows.append([section, key, raw])
                if name == "control":
                    for key, raw in conn.execute(
                        "SELECT key,value FROM records WHERE section='service_commands' ORDER BY key"
                    ):
                        value = json.loads(raw)
                        if not value.get("method", "").startswith("ops_") and value.get(
                            "method"
                        ) not in {"stop", "pause", "resume"}:
                            rows.append(["service_commands", key, raw])
        if db.exists():
            with db.connection() as conn:
                for table in ("metadata", "inbox", "recipients"):
                    rows.append(
                        [
                            table,
                            conn.execute(
                                "SELECT * FROM " + table + " ORDER BY 1,2"
                                if table != "recipients"
                                else "SELECT * FROM recipients ORDER BY name"
                            ).fetchall(),
                        ]
                    )
                rows.append(
                    [
                        "inc_heads",
                        conn.execute(
                            "SELECT doc,revision FROM inc_heads WHERE root=1 ORDER BY doc"
                        ).fetchall(),
                    ]
                )
        result[name] = fingerprint(rows)
    return fingerprint(result)


def refresh(manager):
    work = {w["id"]: w for w in records(manager, "work")}
    protected = {
        identity
        for identity, w in work.items()
        if w["state"] not in TERMINAL
        or (w.get("workspace_sync") or {}).get("state") in {"pending", "conflict"}
    }
    actions = records(manager, "actions")
    pending = [
        a
        for a in actions
        if a["state"] in {"queued", "awaiting_approval", "started", "unknown"}
    ]
    protected.update(a["work_id"] for a in pending)
    for request in records(manager, "decisions"):
        if (
            request["state"] == "pending"
            and request["work_id"] in work
            and request["requirements_revision"]
            == work[request["work_id"]]["requirements_revision"]
        ):
            protected.add(request["work_id"])
    with manager.store.db.connection() as conn:
        acknowledged = (
            conn.execute(
                "SELECT min(json_extract(value,'$.activity')) FROM records WHERE section='service_report_reads' AND owner=?",
                ("service:" + manager.runtime.id,),
            ).fetchone()[0]
            or 0
        )
    for report in records(manager, "reports"):
        if report["state"] in {"queued", "ready"} and report["activity"] > acknowledged:
            protected.update(
                f.get("snapshot", {}).get("id") for f in report.get("facts", [])
            )
    changes = {c["id"]: c for c in records(manager, "changesets")}
    runs = {
        w["run_id"]
        for identity, w in work.items()
        if identity in protected and w.get("run_id")
    }
    runs.update(
        r
        for identity, w in work.items()
        if identity in protected
        for r in w.get("run_history", [])
    )
    runs.update(
        changes[a["changeset_id"]]["run_id"]
        for a in pending
        if a["changeset_id"] in changes
    )
    from homebrew_harness.runs.store import RunStore

    for name, root in roots(manager).items():
        if name == "control":
            continue
        store = RunStore(root)
        sessions = set()
        for identity in runs:
            try:
                run = store.get(identity)
            except ValueError:
                continue
            sessions.update(run.get("session_refs", [run["session_id"]]))
        value = {
            "format": 1,
            "service_id": manager.runtime.id,
            "sessions": sorted(sessions),
            "run_ids": sorted(runs),
            "work_ids": sorted(p for p in protected if p),
            "control_identity": fingerprint(str(manager.store.root)),
        }
        reference = ".harness/service-retention.json"
        raw = json.dumps(value, sort_keys=True).encode()
        with SafeFile(root, reference, create_parents=True) as target:
            previous, version, _ = target.snapshot(max_bytes=1_000_000)
            if previous != raw:
                target.publish(raw, version, 0o600)
    return {"work_ids": sorted(p for p in protected if p), "run_ids": sorted(runs)}


def directory_version(manager, path):
    if path.is_symlink() or not path.is_dir():
        raise ValueError("Candidate directory is not a regular retained directory.")
    files = {}
    total = 0
    for parent, dirs, names in os.walk(path, followlinks=False):
        check()
        if any((Path(parent) / d).is_symlink() for d in dirs):
            raise ValueError("Candidate retirement refuses internal symlinks.")
        for name in names:
            ref = (Path(parent) / name).relative_to(manager.store.root).as_posix()
            with SafeFile(manager.store.root, ref) as file:
                raw, version, mode = file.snapshot(
                    max_bytes=manager.spec["max_backup_bytes"] - total
                )
            total += len(raw or b"")
            if (
                total > manager.spec["max_backup_bytes"]
                or len(files) >= manager.spec["max_backup_files"]
            ):
                raise ValueError("Candidate retirement inventory exceeds its limit.")
            files[(Path(parent) / name).relative_to(path).as_posix()] = [
                version[:-1],
                version[-1].hex(),
                mode,
            ]
    info = path.stat()
    return fingerprint([info.st_dev, info.st_ino, files])


def candidate_plan(manager, pins):
    result = []
    for row in records(manager, "changesets"):
        candidate = row.get("candidate")
        if (
            not candidate
            or candidate.get("artifacts_retired")
            or row["work_id"] in pins["work_ids"]
            or time.time() - row["created_at"]
            < manager.spec["candidate_retention_days"] * 86400
        ):
            continue
        if row["state"] not in {
            "delivered",
            "superseded",
            "failed",
            "quality_failed",
            "blocked",
            "repairing",
        }:
            continue
        relative = (
            ".harness/delivery/candidates/" + row["id"] + "/" + str(row["attempts"])
        )
        path = manager.store.root / relative
        if Path(candidate["workspace"]).parent != path:
            raise ValueError("Candidate ownership path changed.")
        if path.exists():
            result.append(
                {
                    "id": row["id"],
                    "path": relative,
                    "version": directory_version(manager, path),
                }
            )
    return result


def retire_candidate(manager, row, item):
    source = manager.store.root / item["path"]
    target_ref = (
        ".harness/service_operations/quarantine/" + row["id"] + "/" + item["id"]
    )
    target = manager.store.root / target_ref
    if source.exists():
        if directory_version(manager, source) != item["version"]:
            raise ValueError("Candidate retirement preview is stale.")
        if target.exists():
            raise ValueError("Inspect existing retirement quarantine.")
        with (
            SafeFile(manager.store.root, item["path"]) as src,
            SafeFile(manager.store.root, target_ref, create_parents=True) as dst,
        ):
            info = os.stat(src.parts[-1], dir_fd=src.fds[-1], follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("Candidate directory binding changed.")
            os.rename(
                src.parts[-1],
                dst.parts[-1],
                src_dir_fd=src.fds[-1],
                dst_dir_fd=dst.fds[-1],
            )
    if target.exists():
        if directory_version(manager, target) != item["version"]:
            raise ValueError(
                "Retirement quarantine changed; inspect it before recovery."
            )
        if not shutil.rmtree.avoids_symlink_attacks:
            raise ValueError("Secure directory removal is unavailable.")
        with SafeFile(manager.store.root, target_ref) as dst:
            shutil.rmtree(dst.parts[-1], dir_fd=dst.fds[-1])
    with manager.store.db.write() as conn:
        manager.store.guard(conn, manager.runtime.header)
        candidate = manager.store.db._get(conn, "service_changesets", item["id"])
        candidate["candidate"].update(artifacts_retired=True)
        candidate["candidate"].pop("files", None)
        candidate.pop("primary_expected", None)
        candidate["revision"] += 1
        manager.store.db._put(conn, "service_changesets", item["id"], candidate)


def preview(manager):
    if manager.store.get(manager.runtime.id).get("operations_phase") != "parked":
        raise ValueError("Park service before retention planning.")
    pins = refresh(manager)
    from homebrew_harness.storage.checkpoint_retirement import preview as retire_preview
    from homebrew_harness.storage.history_gc import preview as artifact_preview
    from homebrew_harness.storage.incremental_gc import preview as incremental_preview

    plans = {}
    for name, root in roots(manager).items():
        check()
        plans[name] = {
            "retirement": retire_preview(
                root,
                days=manager.spec["retention_days"],
                keep_recent=manager.spec["keep_recent"],
            ),
            "artifacts": artifact_preview(
                root, retention_days=manager.spec["retention_days"]
            ),
            "incremental": incremental_preview(root),
        }
    return {
        "business": business_fingerprint(manager),
        "pins": pins,
        "plans": plans,
        "candidates": candidate_plan(manager, pins),
    }


def perform(manager, row):
    if row["kind"] == "retention_preview":
        return preview(manager)
    plan = manager.get(row["payload"]["id"])
    if plan["kind"] != "retention_preview" or plan["state"] != "completed":
        raise ValueError("Use a completed service retention preview.")
    progress = row.get("progress") or {
        "completed": {},
        "business": plan["result"]["business"],
    }
    if progress["business"] != business_fingerprint(manager):
        raise ValueError("Service retention preview is stale.")
    refresh(manager)
    from homebrew_harness.storage.checkpoint_retirement import retire
    from homebrew_harness.storage.history_gc import apply
    from homebrew_harness.storage.incremental_gc import prune

    result = progress["completed"]
    for name, root in roots(manager).items():
        if name in result:
            continue
        part = plan["result"]["plans"][name]
        result[name] = {
            "retirement": retire(root, part["retirement"]),
            "artifacts": apply(root, part["artifacts"]),
            "incremental": prune(root, part["incremental"]),
        }
        manager.update(
            row,
            progress={"completed": result, "business": business_fingerprint(manager)},
        )
    for item in plan["result"].get("candidates", []):
        name = "candidate:" + item["id"]
        if name in result:
            continue
        retire_candidate(manager, row, item)
        result[name] = {"retired": True}
        manager.update(
            row,
            progress={"completed": result, "business": business_fingerprint(manager)},
        )
    return result
