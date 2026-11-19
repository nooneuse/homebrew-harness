"""Owned immutable work-source copies, independent of a conversation checkpoint."""

import hashlib
import json
import time
from pathlib import Path

from homebrew_harness.execution.execution_control import check
from homebrew_harness.execution.source_snapshot import snapshot, content_digest
from homebrew_harness.security.safe_files import SafeFile


def freeze(root, identity, limits):
    root = Path(root)
    manifest = snapshot(root, **limits)
    prefix = ".harness/work-progress/" + identity + "/source/"
    with SafeFile(root, prefix + ".snapshot-anchor", create_parents=True):
        pass
    inventory = []
    for name, metadata in manifest.items():
        check()
        with SafeFile(root, name) as source:
            data, _, _ = source.snapshot(max_bytes=limits["max_bytes"])
        if data is None or hashlib.sha256(data).hexdigest() != metadata["sha256"]:
            raise ValueError("Work source changed during handoff capture: " + name)
        with SafeFile(root, prefix + name, create_parents=True) as target:
            existing, version, _ = target.snapshot(max_bytes=limits["max_bytes"])
            if existing is not None and existing != data:
                raise ValueError(
                    "Immutable work snapshot already has different content."
                )
            if existing is None:
                target.publish(
                    data, version, 0o500 if metadata["executable"] else 0o400
                )
        inventory.append(
            {
                "path": prefix + name,
                "kind": "work_source",
                "created_at": time.time(),
                "bytes": len(data),
                "sha256": metadata["sha256"],
                "format": 1,
            }
        )
    if content_digest(snapshot(root, **limits)) != content_digest(manifest):
        raise ValueError("Work source changed before handoff commit.")
    manifest_path = ".harness/work-progress/" + identity + "/manifest.json"
    raw = json.dumps(
        {"files": [prefix + name for name in manifest]}, sort_keys=True
    ).encode()
    with SafeFile(root, manifest_path, create_parents=True) as target:
        previous, version, _ = target.snapshot()
        if previous is None:
            target.publish(raw, version, 0o400)
    inventory.append(
        {
            "path": manifest_path,
            "kind": "work_source_manifest",
            "created_at": time.time(),
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "format": 1,
        }
    )
    from homebrew_harness.storage.durable_runtime import DurableStore

    db = DurableStore(root).backend()
    with db.write() as conn:
        for entry in inventory:
            db._put(conn, "artifacts", entry["path"], entry)
        db._put(
            conn,
            "work_source_bundles",
            identity,
            {
                "id": identity,
                "state": "published",
                "reference": manifest_path,
                "files": [prefix + name for name in manifest],
            },
        )
    return {
        "workspace": str(root / prefix),
        "reference": manifest_path,
        "files": manifest,
        "content_hash": content_digest(manifest),
        "limits": limits,
    }


def verify(value):
    actual = snapshot(Path(value["workspace"]), **value["limits"])
    if content_digest(actual) != value["content_hash"]:
        raise ValueError("Accepted work snapshot changed.")
    return value
