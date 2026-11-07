"""Explicit host checkpoint retirement with reversible cold snapshots and purge."""

import copy
import fcntl
import gzip
import hashlib
import io
import json
import math
import re
import time
import uuid
from pathlib import Path

from homebrew_harness.runtime.runtime_state import (
    pending_model_work,
    settled_checkpoint,
    settled_operation,
)
from homebrew_harness.security.safe_files import SafeFile, canonical_root
from homebrew_harness.storage.checkpoint_lifecycle import session_id
from homebrew_harness.storage.durable_runtime import DurableStore, RecoveryRequired
from homebrew_harness.storage.retention_graph import (
    external_snapshot,
    references,
    state_roots,
)
from homebrew_harness.storage.storage_admin import canonical, maintenance

DEFAULT_DAYS = 15
DEFAULT_KEEP = 10
GRACE_DAYS = 7
MAX_BYTES = 64 * 1024 * 1024
_COLD = re.compile(r"\.harness/checkpoint-retirement/[0-9a-f]{32}\.json\.gz\Z")


def clock(now):
    value = time.time() if now is None else now
    if type(value) not in {int, float} or not math.isfinite(value):
        raise ValueError("Invalid lifecycle time.")
    return value


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def owns(sid, key):
    return key == "lead:" + sid or key.startswith(
        ("sub:" + sid + ":", "member:" + sid + ":")
    )


def bundle(state, sid):
    return {
        "format": 2,
        "session_id": sid,
        "meta": copy.deepcopy(state.get("session_meta", {}).get(sid)),
        "checkpoints": {
            k: copy.deepcopy(v) for k, v in state["checkpoints"].items() if owns(sid, k)
        },
        "operations": {
            k: copy.deepcopy(v)
            for k, v in state["operations"].items()
            if owns(sid, v.get("checkpoint", ""))
        },
        "team": copy.deepcopy(state["team"].get(sid)),
        "input_receipts": {
            k: copy.deepcopy(v)
            for k, v in state.get("input_receipts", {}).items()
            if owns(sid, v.get("checkpoint", ""))
        },
    }


def workspace_blockers(state, external, sid):
    reasons = []
    if any(items for items in state["inbox"].values()):
        reasons.append("workspace_unacknowledged_mailbox")
    for record in state["background"].values():
        if (
            record.get("ready")
            or record.get("status") == "running"
            or record.get("status") == "unknown"
            and not record.get("inspected")
        ):
            if record.get("session_id") in {None, sid}:
                reasons.append("workspace_unsettled_background")
    for record in state["cron"].values():
        if record.get("state") in {"received", "unknown"} and record.get(
            "session_id"
        ) in {None, sid}:
            reasons.append("workspace_unsettled_cron")
    for value in state.get("goals", {}).values():
        if value.get("status") not in {"achieved", "failed", "cleared", "replaced"}:
            reasons.append("workspace_unfinished_goal")
    for value in state.get("input_receipts", {}).values():
        if owns(sid, value.get("checkpoint", "")) and value.get("state") in {
            "blocked",
            "retry_wait",
        }:
            reasons.append("checkpoint_pending_input")
    for path, value in external["records"].items():
        if not isinstance(value, dict):
            continue
        if value.get("session_id") not in {None, sid}:
            continue
        name = Path(path).name
        if path.startswith(".tasks/") and value.get("status") != "completed":
            reasons.append("workspace_unfinished_task")
        elif (
            re.fullmatch(r"wf_[0-9a-f]{32}\.json", name)
            and value.get("task", {}).get("status") != "completed"
        ):
            reasons.append("workspace_unfinished_workflow")
        elif (
            path.startswith(".harness/containers/") and value.get("phase") != "removed"
        ):
            reasons.append("workspace_container_cleanup_pending")
    return reasons


def lease_blockers(root):
    reasons = []
    for resource in ("session", "workspace:" + str(root)):
        name = hashlib.sha256(resource.encode()).hexdigest()
        try:
            with (
                SafeFile(root, ".harness/leases/" + name + ".lock") as target,
                target.open_read() as stream,
            ):
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    reasons.append("active_execution_lease")
        except FileNotFoundError:
            pass
    return reasons


def usage_blockers(checkpoints):
    return sorted(
        {
            reason
            for snapshot in checkpoints.values()
            if snapshot.get("usage") is not None
            for reason in pending_model_work(snapshot["usage"])
        }
    )


def eligibility(root, state, external, sid):
    meta = state.get("session_meta", {}).get(sid)
    data = bundle(state, sid)
    reasons = workspace_blockers(state, external, sid)
    if not isinstance(meta, dict) or meta.get("format") != 1:
        reasons.append("session_metadata_missing_or_invalid")
        meta = meta or {}
    if meta.get("lifecycle", "retained") != "retained":
        reasons.append("not_retained")
    if meta.get("pinned"):
        reasons.append("session_pinned")
    if meta.get("cleanup_pending") or meta.get("orphan_cold_archive"):
        reasons.append("cold_cleanup_pending")
    if sid == state.get("latest_session"):
        reasons.append("latest_resume_target")
    if meta.get("close_complete") is not True:
        reasons.append("closure_not_finalized")
    if type(meta.get("closed_at")) not in {int, float} or not math.isfinite(
        meta["closed_at"]
    ):
        reasons.append("closure_time_unknown")
    lead = data["checkpoints"].get("lead:" + sid)
    if not isinstance(lead, dict) or lead.get("phase") != "closed":
        reasons.append("lead_not_closed")
    for key, snapshot in data["checkpoints"].items():
        if snapshot.get("session_id", sid) != sid:
            reasons.append("checkpoint_session_mismatch")
        if not settled_checkpoint(key, snapshot):
            reasons.append("unfinished_checkpoint")
        if (
            snapshot.get("recovery_blocked")
            or snapshot.get("pending_team")
            or snapshot.get("pending_background")
        ):
            reasons.append("checkpoint_pending_recovery")
        if snapshot.get("parent_checkpoint") and not owns(
            sid, snapshot["parent_checkpoint"]
        ):
            reasons.append("cross_session_parent")
        for field, prefix in (("lead_task", ".tasks/"), ("goal_id", ".harness/goals/")):
            identity = snapshot.get(field)
            if identity and prefix + identity + ".json" not in external["records"]:
                if field != "goal_id" or identity not in state.get("goals", {}):
                    reasons.append("missing_linked_dependency")
        for effect in snapshot.get("effects", []):
            if effect.get("state") in {"started", "unknown", "partial"}:
                reasons.append("uncertain_tool_effect")
    for record in data["operations"].values():
        if not settled_operation(record):
            reasons.append("uncertain_operation")
    team = data["team"] or {}
    for member in team.get("members", {}).values():
        if (
            member.get("status") not in {"stopped", "failed"}
            or member.get("pending_context")
            or member.get("gate") == "pending"
        ):
            reasons.append("unfinished_team_member")
    if any(p.get("status") == "pending" for p in team.get("protocols", [])):
        reasons.append("pending_team_protocol")
    reasons.extend(usage_blockers(data["checkpoints"]))
    seal = digest(
        {
            "bundle": data,
            "latest": state.get("latest_session"),
            "workspace": {
                "inbox": state["inbox"],
                "background": state["background"],
                "cron": state["cron"],
            },
            "external": external["versions"],
        }
    )
    return sorted(set(reasons)), seal, data


def preview(
    root,
    *,
    days=DEFAULT_DAYS,
    keep_recent=DEFAULT_KEEP,
    selected=None,
    now=None,
    _locked=False,
):
    root = canonical_root(Path(root))
    now = clock(now)
    if (
        type(days) is not int
        or days < 1
        or type(keep_recent) is not int
        or keep_recent < 1
    ):
        raise ValueError(
            "Retirement policy requires positive integer days and recent count."
        )
    selected = None if selected is None else [session_id(sid) for sid in selected]
    state = DurableStore(root).backend().read()
    external = external_snapshot(root)
    metas = state.get("session_meta", {})
    active = [] if _locked else lease_blockers(root)
    ids = set(metas) | {
        key[5:] for key in state["checkpoints"] if key.startswith("lead:")
    }
    if selected is not None and any(sid not in ids for sid in selected):
        raise ValueError("Selected session does not exist.")
    retained = [
        sid
        for sid in ids
        if metas.get(sid, {}).get("lifecycle", "retained") == "retained"
    ]
    retained.sort(
        key=lambda sid: (metas.get(sid, {}).get("last_activity_at", 0) or 0, sid),
        reverse=True,
    )
    recent = set(retained[:keep_recent])
    sessions = []
    candidates = []
    for sid in sorted(ids):
        meta = metas.get(sid, {})
        reasons, seal, data = eligibility(root, state, external, sid)
        reasons.extend(active)
        if selected is None:
            if sid in recent:
                reasons.append("recent_session_policy")
            closed = meta.get("closed_at")
            if type(closed) not in {int, float} or now - closed <= days * 86400:
                reasons.append("age_policy")
        elif sid not in selected:
            reasons.append("not_selected")
        item = {
            "session_id": sid,
            "lifecycle": meta.get("lifecycle", "retained"),
            "pinned": bool(meta.get("pinned")),
            "closed_at": meta.get("closed_at"),
            "last_activity_at": meta.get("last_activity_at"),
            "checkpoint_count": len(data["checkpoints"]),
            "snapshot_bytes": len(canonical(data)),
            "eligible": not reasons,
            "reasons": sorted(set(reasons)),
            "seal": seal,
        }
        sessions.append(item)
        if item["eligible"]:
            candidates.append({"session_id": sid, "seal": seal})
    return {
        "format": 1,
        "read_only": True,
        "kind": "checkpoint_retire",
        "policy": {
            "days": days,
            "keep_recent": keep_recent,
            "grace_days": GRACE_DAYS,
            "selected": selected,
        },
        "sessions": sessions,
        "candidates": candidates,
    }


def pin(root, sid, enabled=True):
    sid = session_id(sid)
    if type(enabled) is not bool:
        raise ValueError("Pin must be boolean.")
    with maintenance(root) as root:
        store = DurableStore(root)

        def update(state):
            metas = state.setdefault("session_meta", {})
            meta = metas.get(sid)
            if meta is None or meta.get("format") != 1:
                raise RecoveryRequired(
                    "Current session lifecycle record is missing or invalid."
                )
            if meta.get("lifecycle") == "purged":
                raise ValueError("Purged recovery data cannot be pinned.")
            metas[sid] = {
                **meta,
                "pinned": enabled,
                "revision": meta.get("revision", 0) + 1,
            }

        store.transaction(update)
    return {"session_id": sid, "pinned": enabled}


def _publish_archive(root, data):
    raw = canonical(data)
    if len(raw) > MAX_BYTES:
        raise ValueError("Cold snapshot exceeds the size limit.")
    packed = gzip.compress(raw, mtime=0)
    if len(packed) > MAX_BYTES:
        raise ValueError("Compressed snapshot exceeds the size limit.")
    reference = ".harness/checkpoint-retirement/" + uuid.uuid4().hex + ".json.gz"
    with SafeFile(root, reference, create_parents=True) as target:
        target.publish(packed, None, 0o600)
    return {
        "path": reference,
        "sha256": hashlib.sha256(packed).hexdigest(),
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(packed),
        "raw_bytes": len(raw),
    }


def _load_archive(root, sid, archive):
    if not isinstance(archive, dict) or not _COLD.fullmatch(archive.get("path", "")):
        raise RecoveryRequired("Invalid cold snapshot reference.")
    with SafeFile(root, archive["path"]) as target:
        packed, identity, _ = target.snapshot(max_bytes=MAX_BYTES)
    if packed is None or hashlib.sha256(packed).hexdigest() != archive.get("sha256"):
        raise RecoveryRequired("Cold snapshot is missing or changed.")
    with gzip.GzipFile(fileobj=io.BytesIO(packed)) as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES or hashlib.sha256(raw).hexdigest() != archive.get(
        "raw_sha256"
    ):
        raise RecoveryRequired("Cold snapshot failed its bounded content verification.")
    data = json.loads(raw)
    if (
        data.get("format") != 2
        or data.get("session_id") != sid
        or any(not owns(sid, key) for key in data["checkpoints"])
        or any(
            not owns(sid, value.get("checkpoint", ""))
            for value in data["input_receipts"].values()
        )
    ):
        raise RecoveryRequired("Cold snapshot ownership does not match.")
    return data, identity


def retire(root, plan, *, now=None):
    if (
        not isinstance(plan, dict)
        or plan.get("kind") != "checkpoint_retire"
        or plan.get("format") != 1
    ):
        raise ValueError("Retirement requires a saved preview.")
    now = clock(now)
    policy = plan["policy"]
    with maintenance(root) as root:
        fresh = preview(
            root,
            days=policy["days"],
            keep_recent=policy["keep_recent"],
            selected=policy["selected"],
            now=now,
            _locked=True,
        )
        valid = {item["session_id"]: item for item in fresh["candidates"]}
        selected = []
        already = []
        initial = DurableStore(root).read()
        for item in plan["candidates"]:
            if not isinstance(item, dict) or set(item) != {"session_id", "seal"}:
                raise ValueError("Invalid retirement candidate.")
            meta = initial.get("session_meta", {}).get(item["session_id"], {})
            if (
                meta.get("lifecycle") in {"retired", "purged"}
                and meta.get("retire_seal") == item["seal"]
            ):
                if item["session_id"] in already:
                    raise ValueError("Duplicate retirement candidate.")
                already.append(item["session_id"])
                continue
            if (
                item != valid.get(item.get("session_id"))
                or item["session_id"] in selected
            ):
                raise ValueError("Retirement preview is stale; inspect a new preview.")
            selected.append(item["session_id"])
        store = DurableStore(root)
        state = store.read()
        archives = {}
        external_snapshot_before = external_snapshot(root)["versions"]
        for sid in selected:
            archives[sid] = _publish_archive(root, bundle(state, sid))

        def update(current):
            if digest(current) != digest(state):
                raise ValueError("Runtime changed during retirement preparation.")
            external_now = external_snapshot(root)
            if external_now["versions"] != external_snapshot_before:
                raise ValueError("Dependencies changed during retirement preparation.")
            for sid in selected:
                reasons, seal, _ = eligibility(root, current, external_now, sid)
                if reasons or seal != valid[sid]["seal"]:
                    raise ValueError(
                        "Retirement eligibility changed during preparation."
                    )
            for sid in selected:
                data = bundle(current, sid)
                meta = current["session_meta"][sid]
                usage = data["checkpoints"].get("lead:" + sid, {}).get("usage", {})
                current["session_meta"][sid] = {
                    **meta,
                    "lifecycle": "retired",
                    "retired_at": now,
                    "purge_after": now + GRACE_DAYS * 86400,
                    "cold_archive": archives[sid],
                    "protected_refs": sorted(references(data)),
                    "checkpoint_keys": sorted(data["checkpoints"]),
                    "retire_seal": valid[sid]["seal"],
                    "revision": meta["revision"] + 1,
                    "retirement_reason": "host_explicit_retirement",
                    "usage_summary": {
                        key: usage[key]
                        for key in (
                            "attempted_calls",
                            "calls",
                            "known_tokens",
                            "unknown_usage_calls",
                            "ledger",
                        )
                        if key in usage
                    },
                }
                for key in data["checkpoints"]:
                    del current["checkpoints"][key]
                for key in data["operations"]:
                    del current["operations"][key]
                for key in data["input_receipts"]:
                    del current["input_receipts"][key]
                current["team"].pop(sid, None)

        store.transaction(update)
    return {"retired": selected, "already_retired": already, "grace_days": GRACE_DAYS}


def unretire(root, sid, *, now=None):
    sid = session_id(sid)
    now = clock(now)
    with maintenance(root) as root:
        store = DurableStore(root)
        meta = store.get("session_meta", sid)
        if meta and meta.get("lifecycle") == "retained":
            return {"session_id": sid, "already_retained": True}
        if not meta or meta.get("lifecycle") != "retired":
            raise ValueError("Session is not reversibly retired.")
        if now >= meta["purge_after"]:
            raise ValueError("Retirement undo window has ended.")
        data, _ = _load_archive(root, sid, meta["cold_archive"])
        for reference in meta["protected_refs"]:
            if re.fullmatch(
                r"\.harness/(?:[A-Za-z0-9_-]+/)?(?:tool-results|transcripts)/[0-9a-f]{32}\.(?:txt|jsonl)",
                reference,
            ):
                with SafeFile(root, reference) as target, target.open_read():
                    pass

        def update(state):
            if state.get("session_meta", {}).get(sid) != meta:
                raise ValueError("Session changed before undo.")
            if (
                any(key in state["checkpoints"] for key in data["checkpoints"])
                or sid in state["team"]
            ):
                raise ValueError("Recovery records conflict with the retired snapshot.")
            state["checkpoints"].update(data["checkpoints"])
            state["operations"].update(data["operations"])
            state.setdefault("input_receipts", {}).update(data["input_receipts"])
            if data["team"] is not None:
                state["team"][sid] = data["team"]
            original = data["meta"]
            state["session_meta"][sid] = {
                **original,
                "pinned": meta["pinned"],
                "last_activity_at": now,
                "closed_at": now,
                "revision": meta["revision"] + 1,
                "orphan_cold_archive": meta["cold_archive"],
                "cleanup_pending": True,
            }

        store.transaction(update)
        _cleanup_cold(root, sid, store.get("session_meta", sid), "orphan_cold_archive")
    return {
        "session_id": sid,
        "lifecycle": "retained",
        "cleanup_pending": bool(store.get("session_meta", sid).get("cleanup_pending")),
    }


def purge_preview(root, *, selected=None, now=None, _locked=False):
    root = canonical_root(Path(root))
    now = clock(now)
    state = DurableStore(root).backend().read()
    external = external_snapshot(root)
    selected = None if selected is None else [session_id(sid) for sid in selected]
    sessions = []
    candidates = []
    active = [] if _locked else lease_blockers(root)
    for sid, meta in state.get("session_meta", {}).items():
        if meta.get("lifecycle") != "retired":
            continue
        reasons = workspace_blockers(state, external, sid)
        reasons.extend(active)
        if meta.get("pinned"):
            reasons.append("session_pinned")
        if sid == state.get("latest_session"):
            reasons.append("latest_resume_target")
        if now < meta["purge_after"]:
            reasons.append("undo_grace_period")
        if selected is not None and sid not in selected:
            reasons.append("not_selected")
        other = {
            **state,
            "session_meta": {
                k: v for k, v in state.get("session_meta", {}).items() if k != sid
            },
        }
        if meta["cold_archive"]["path"] in state_roots(other, external["references"]):
            reasons.append("cold_archive_referenced")
        data, _ = _load_archive(root, sid, meta["cold_archive"])
        pending = usage_blockers(data["checkpoints"])
        reasons.extend(pending)
        seal = digest(
            {"meta": meta, "workspace": state, "external": external["versions"]}
        )
        item = {
            "session_id": sid,
            "purge_after": meta["purge_after"],
            "eligible": not reasons,
            "reasons": sorted(set(reasons)),
            "cold_bytes": meta["cold_archive"]["bytes"],
            "seal": seal,
        }
        sessions.append(item)
        if item["eligible"]:
            candidates.append({"session_id": sid, "seal": seal})
    if selected is not None and any(
        sid not in {v["session_id"] for v in sessions} for sid in selected
    ):
        raise ValueError("Selected retired session does not exist.")
    return {
        "format": 1,
        "kind": "checkpoint_purge",
        "read_only": True,
        "selected": selected,
        "sessions": sessions,
        "candidates": candidates,
    }


def _cleanup_cold(root, sid, meta, field):
    archive = meta.get(field)
    if not archive:
        return False
    try:
        reference = archive["path"]
        current = DurableStore(root).read()
        other = {
            **current,
            "session_meta": {
                k: v for k, v in current.get("session_meta", {}).items() if k != sid
            },
        }
        if reference in state_roots(other, external_snapshot(root)["references"]):
            return False
        with SafeFile(root, reference) as target:
            raw, identity, _ = target.snapshot(max_bytes=MAX_BYTES)
            if raw is not None:
                if (
                    not _COLD.fullmatch(reference)
                    or hashlib.sha256(raw).hexdigest() != archive["sha256"]
                ):
                    raise RecoveryRequired("Cold cleanup identity changed.")
                target.unlink(identity)
        DurableStore(root).update_fields(
            "session_meta", sid, {field: None, "cleanup_pending": False}
        )
        return True
    except (OSError, ValueError):
        return False


def purge(root, plan, *, now=None):
    if (
        not isinstance(plan, dict)
        or plan.get("kind") != "checkpoint_purge"
        or plan.get("format") != 1
    ):
        raise ValueError("Purge requires a saved preview.")
    now = clock(now)
    with maintenance(root) as root:
        initial = DurableStore(root).read()
        already = []
        for item in plan["candidates"]:
            if not isinstance(item, dict) or set(item) != {"session_id", "seal"}:
                raise ValueError("Invalid purge candidate.")
            meta = initial.get("session_meta", {}).get(item["session_id"], {})
            if (
                meta.get("lifecycle") == "purged"
                and meta.get("purge_seal") == item["seal"]
            ):
                if item["session_id"] in already:
                    raise ValueError("Duplicate purge candidate.")
                already.append(item["session_id"])
        selected = (
            None
            if plan["selected"] is None
            else [sid for sid in plan["selected"] if sid not in already]
        )
        fresh = purge_preview(root, selected=selected, now=now, _locked=True)
        valid = {item["session_id"]: item for item in fresh["candidates"]}
        ids = []
        for item in plan["candidates"]:
            if item["session_id"] in already:
                continue
            if item != valid.get(item.get("session_id")) or item["session_id"] in ids:
                raise ValueError("Purge preview is stale.")
            ids.append(item["session_id"])
        store = DurableStore(root)
        before = store.read()
        external_before = external_snapshot(root)["versions"]

        def update(state):
            if (
                digest(state) != digest(before)
                or external_snapshot(root)["versions"] != external_before
            ):
                raise ValueError("Purge dependencies changed.")
            for sid in ids:
                meta = state["session_meta"][sid]
                state["session_meta"][sid] = {
                    k: v
                    for k, v in meta.items()
                    if k
                    in {
                        "format",
                        "session_id",
                        "created_at",
                        "closed_at",
                        "last_activity_at",
                        "revision",
                        "retire_seal",
                        "retired_at",
                        "retirement_reason",
                        "usage_summary",
                    }
                } | {
                    "lifecycle": "purged",
                    "purged_at": now,
                    "snapshot_sha256": meta["cold_archive"]["raw_sha256"],
                    "cleanup_pending": True,
                    "purged_cold_archive": meta["cold_archive"],
                    "revision": meta["revision"] + 1,
                    "purge_seal": valid[sid]["seal"],
                }

        store.transaction(update)
        for sid in ids + already:
            _cleanup_cold(
                root, sid, store.get("session_meta", sid), "purged_cold_archive"
            )
    return {
        "purged": ids,
        "already_purged": already,
        "cleanup_pending": [
            sid
            for sid in ids + already
            if store.get("session_meta", sid).get("cleanup_pending")
        ],
    }


def reconcile(root):
    with maintenance(root) as root:
        store = DurableStore(root)
        state = store.read()
        cleaned = []
        for sid, meta in state.get("session_meta", {}).items():
            field = (
                "purged_cold_archive"
                if meta.get("lifecycle") == "purged"
                else "orphan_cold_archive"
            )
            if meta.get(field) and _cleanup_cold(root, sid, meta, field):
                cleaned.append(sid)
        # Failed retirement preparation: only remove verified copies of unchanged retained sessions.
        directory = root / ".harness/checkpoint-retirement"
        if directory.is_symlink():
            raise ValueError("Cold snapshot directory must not be a symlink.")
        referenced = {
            v[field]["path"]
            for v in store.items("session_meta").values()
            for field in ("cold_archive", "purged_cold_archive", "orphan_cold_archive")
            if v.get(field)
        }
        orphans = []
        external_refs = external_snapshot(root)["references"]
        total = 0
        if directory.exists():
            for index, path in enumerate(directory.iterdir()):
                if index >= 10000:
                    raise ValueError("Cold snapshot inventory exceeds its limit.")
                reference = path.relative_to(root).as_posix()
                if reference in referenced or reference in external_refs:
                    continue
                if not _COLD.fullmatch(reference):
                    continue
                with SafeFile(root, reference) as target:
                    packed, identity, _ = target.snapshot(max_bytes=MAX_BYTES - total)
                    total += len(packed)
                    with gzip.GzipFile(fileobj=io.BytesIO(packed)) as stream:
                        raw = stream.read(MAX_BYTES + 1)
                    if len(raw) > MAX_BYTES:
                        raise ValueError("Orphan snapshot exceeds its limit.")
                    data = json.loads(raw)
                    sid = session_id(data["session_id"])
                    current = bundle(store.read(), sid)
                    if (
                        current["meta"]
                        and current["meta"].get("lifecycle") == "retained"
                        and digest(current) == digest(data)
                    ):
                        target.unlink(identity)
                        orphans.append(reference)
        return {"cleaned_sessions": cleaned, "removed_prepared_orphans": orphans}
