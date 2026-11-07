"""Host-owned current-state backup, status and maintenance boundaries."""

import json
import uuid
from contextlib import contextmanager
from pathlib import Path

from homebrew_harness.execution.execution_leases import ExecutionLeases
from homebrew_harness.security.safe_files import SafeFile, canonical_root
from homebrew_harness.storage.durable_runtime import DurableStore, RecoveryRequired
from homebrew_harness.storage.storage_gate import storage_gate


def canonical(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False
    ).encode()


def publish(root, reference, value):
    with SafeFile(root, reference, create_parents=True) as target:
        target.publish(canonical(value), target.snapshot()[1], 0o600)


@contextmanager
def maintenance(root):
    root = canonical_root(Path(root))
    leases = ExecutionLeases(root)
    try:
        with (
            leases.use("session"),
            leases.use("workspace:" + str(root)),
            storage_gate(root, exclusive=True),
        ):
            yield root
    finally:
        leases.close()


def storage_status(root):
    return DurableStore(root).backend().health()


def backup(root):
    """Copy the current database consistently; no alternate backend or format conversion."""
    import os
    import sqlite3

    with maintenance(root) as root:
        store = DurableStore(root).backend()
        if not store.exists():
            return {"initialized": False, "backup": None}
        reference = ".harness/runtime/database/backups/" + uuid.uuid4().hex + ".sqlite3"
        with SafeFile(root, reference, create_parents=True) as target:
            fd = os.open(
                target.parts[-1],
                os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
                dir_fd=target.fds[-1],
            )
            try:
                destination = sqlite3.connect(
                    (root / reference).as_uri() + "?mode=rw", uri=True
                )
                try:
                    with store.connection() as source:
                        source.backup(destination)
                    if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise RecoveryRequired(
                            "Current database backup failed verification."
                        )
                finally:
                    destination.close()
                target.verify_binding()
                os.fsync(fd)
                os.fsync(target.fds[-1])
            finally:
                os.close(fd)
        return {"initialized": True, "backup": reference, "schema": 1}
