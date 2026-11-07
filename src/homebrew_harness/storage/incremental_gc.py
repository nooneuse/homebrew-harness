"""Explicit maintenance-only cleanup of unreachable SQLite document generations."""

import hashlib
import json

from homebrew_harness.storage.durable_runtime import DurableStore
from homebrew_harness.storage.storage_admin import canonical, maintenance

LIVE = """WITH RECURSIVE live(doc,revision) AS (
 SELECT doc,revision FROM inc_heads WHERE root=1
 UNION SELECT e.target,e.target_revision FROM inc_edges e JOIN live l ON e.doc=l.doc AND e.revision=l.revision
) SELECT doc,revision FROM live"""


def inventory(engine, conn):
    live = set(conn.execute(LIVE).fetchall())
    versions = conn.execute("SELECT doc,revision,body FROM inc_versions").fetchall()
    if len(versions) > 100000:
        raise ValueError("Incremental version inventory exceeds its bound.")
    epochs = {}
    headers = set()
    components = set()
    dead = []
    for doc, revision, body in versions:
        if (doc, revision) not in live:
            dead.append([doc, revision])
            continue
        meta = json.loads(body)
        epochs[meta["epoch"]] = max(epochs.get(meta["epoch"], 0), meta["count"])
        headers.update((meta["owner"], mid) for _, mid in meta["headers"])
        for name, ref in meta["fields"].items():
            if ref["kind"] == "value":
                components.add((doc, name, ref["revision"]))
    views = conn.execute("SELECT epoch,seq,owner,message FROM inc_views").fetchall()
    if len(views) > 1000000:
        raise ValueError("Incremental message inventory exceeds its bound.")
    removed_views = []
    kept_messages = set(headers)
    for epoch, seq, owner, message in views:
        if seq >= epochs.get(epoch, 0):
            removed_views.append([epoch, seq])
        else:
            kept_messages.add((owner, message))
    messages = [
        list(row)
        for row in conn.execute("SELECT owner,id FROM inc_messages")
        if row not in kept_messages
    ]
    removed_components = [
        list(row)
        for row in conn.execute("SELECT doc,name,revision FROM inc_components")
        if row not in components
    ]
    seal = hashlib.sha256(
        canonical(
            {
                "live": sorted(live),
                "dead": dead,
                "views": removed_views,
                "messages": messages,
                "components": removed_components,
            }
        )
    ).hexdigest()
    return {
        "live": live,
        "dead": dead,
        "views": removed_views,
        "messages": messages,
        "components": removed_components,
        "seal": seal,
    }


def preview(root):
    engine = DurableStore(root).backend()
    if not engine.exists():
        seal = hashlib.sha256(
            canonical(
                {"live": [], "dead": [], "views": [], "messages": [], "components": []}
            )
        ).hexdigest()
        return {
            "format": 1,
            "kind": "incremental_prune",
            "read_only": True,
            "seal": seal,
            "versions": 0,
            "view_rows": 0,
            "messages": 0,
            "components": 0,
        }
    with engine.connection() as conn:
        conn.execute("BEGIN")
        try:
            data = inventory(engine, conn)
        finally:
            conn.execute("ROLLBACK")
    return {
        "format": 1,
        "kind": "incremental_prune",
        "read_only": True,
        "seal": data["seal"],
        "versions": len(data["dead"]),
        "view_rows": len(data["views"]),
        "messages": len(data["messages"]),
        "components": len(data["components"]),
    }


def prune(root, plan):
    if (
        not isinstance(plan, dict)
        or plan.get("kind") != "incremental_prune"
        or plan.get("format") != 1
    ):
        raise ValueError("Incremental cleanup requires a saved preview.")
    with maintenance(root) as root:
        engine = DurableStore(root).backend()
        with engine.write() as conn:
            data = inventory(engine, conn)
            if data["seal"] != plan.get("seal"):
                raise ValueError("Incremental cleanup preview is stale.")
            for doc, revision in data["dead"]:
                conn.execute(
                    "DELETE FROM inc_edges WHERE doc=? AND revision=?", (doc, revision)
                )
            for doc, revision in data["dead"]:
                conn.execute(
                    "DELETE FROM inc_heads WHERE doc=? AND revision=? AND root=0",
                    (doc, revision),
                )
            conn.executemany(
                "DELETE FROM inc_versions WHERE doc=? AND revision=?", data["dead"]
            )
            conn.executemany(
                "DELETE FROM inc_views WHERE epoch=? AND seq=?", data["views"]
            )
            conn.executemany(
                "DELETE FROM inc_components WHERE doc=? AND name=? AND revision=?",
                data["components"],
            )
            conn.executemany(
                "DELETE FROM inc_messages WHERE owner=? AND id=?", data["messages"]
            )
            conn.execute(
                "DELETE FROM inc_results WHERE 'checkpoint:'||context NOT IN (SELECT doc FROM inc_heads WHERE root=1)"
            )
            import uuid

            conn.execute("UPDATE inc_epoch SET token=?", (uuid.uuid4().hex,))
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("Incremental cleanup would break a live reference.")
        return {
            "versions_removed": len(data["dead"]),
            "messages_removed": len(data["messages"]),
            "components_removed": len(data["components"]),
        }
