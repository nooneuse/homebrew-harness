"""Immutable output bundles and recoverable create-only workspace publication."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import time
import uuid
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from pathlib import Path

from homebrew_harness.execution.artifact_policy import ArtifactError, artifact_path
from homebrew_harness.execution.execution_control import check, cleanup_scope
from homebrew_harness.execution.execution_leases import (
    file_update_batch,
    file_update_lease,
)
from homebrew_harness.security.data_boundary import redact
from homebrew_harness.security.safe_files import SafeFile, canonical_root
from homebrew_harness.storage.artifact_inventory import register_artifact
from homebrew_harness.storage.durable_runtime import DurableStore
from homebrew_harness.storage.storage_gate import storage_gate
from homebrew_harness.tools.tool_outcome import ToolExecutionError

_APPROVAL = ContextVar("artifact_publication_approval", default=None)


@contextmanager
def publication_scope(value=None):
    token = _APPROVAL.set(value)
    try:
        yield
    finally:
        _APPROVAL.reset(token)


_TASK = ContextVar("artifact_task", default=None)


@contextmanager
def artifact_context(task_id):
    token = _TASK.set(task_id)
    try:
        yield
    finally:
        _TASK.reset(token)


def current_artifact_task():
    return _TASK.get()


_ID = re.compile(r"art_[0-9a-f]{32}\Z")


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ArtifactStore:
    def __init__(
        self, root, policy, workspace_resolver=None, test_runner=None, owner=None
    ):
        self.root = canonical_root(Path(root))
        self.policy = policy
        self.store = DurableStore(self.root)
        self.workspace_resolver = workspace_resolver
        self.test_runner = test_runner
        self.owner = owner

    def _reference(self):
        return ".harness/artifact-bundles/tool-results/" + uuid.uuid4().hex + ".txt"

    def _archive(self, data, kind, reference=None):
        reference = reference or self._reference()
        with SafeFile(self.root, reference, create_parents=True) as target:
            target.publish(data, None, 0o600)
        register_artifact(self.root, reference, data, kind)
        return reference

    def _load(self, bundle_id):
        if not isinstance(bundle_id, str) or not _ID.fullmatch(bundle_id):
            raise ArtifactError("Invalid artifact bundle ID.")
        value = self.store.get("artifact_bundles", bundle_id)
        if not isinstance(value, dict) or value.get("format") != 2:
            raise ArtifactError("Artifact bundle is unavailable.")
        manifest = {
            k: value[k]
            for k in (
                "bundle_id",
                "execution_id",
                "policy",
                "profile",
                "workspace",
                "workspace_identity",
                "task_id",
                "owner",
                "files",
                "missing",
            )
        }
        files = [
            {k: v for k, v in row.items() if k not in {"state", "published_sha256"}}
            for row in manifest["files"]
        ]
        manifest["files"] = files
        if digest(manifest) != value.get("manifest_sha256"):
            raise ArtifactError("Artifact manifest changed; publication is refused.")
        return value

    def _cwd(self, bundle, current, task_id):
        if bundle["policy"] != self.policy.fingerprint():
            raise ArtifactError(
                "Artifact bundle requires its original host output policy."
            )
        owner = self.owner() if callable(self.owner) else self.owner
        if bundle["owner"] != owner:
            raise ArtifactError("Artifact bundle belongs to another host session.")
        path = self.root / bundle["workspace"]
        info = os.stat(path, follow_symlinks=False)
        if [info.st_dev, info.st_ino] != bundle["workspace_identity"]:
            raise ArtifactError("Artifact workspace was replaced after collection.")
        if bundle["task_id"] is not None:
            if task_id != bundle["task_id"]:
                raise ArtifactError(
                    "Artifact access requires the original assigned task."
                )
            if self.workspace_resolver is not None:
                selected = self.workspace_resolver(task_id)
                if Path(selected).resolve() != path:
                    raise ArtifactError(
                        "The artifact task workspace changed after collection."
                    )
        elif task_id is not None:
            raise ArtifactError(
                "A task cannot publish an unrelated base-workspace bundle."
            )
        if canonical_root(Path(current)) != canonical_root(path):
            raise ArtifactError(
                "Artifact access belongs to a different working directory."
            )
        return path

    def _read_blob(self, row):
        with SafeFile(self.root, row["reference"]) as target:
            raw, _, _ = target.snapshot(max_bytes=row["bytes"] * 2 + 1024)
        if raw is None:
            raise ArtifactError("Artifact contents were reclaimed or are unavailable.")
        try:
            data = base64.b64decode(raw, validate=True)
        except (ValueError, TypeError):
            raise ArtifactError("Artifact content encoding changed.") from None
        if (
            len(data) != row["bytes"]
            or hashlib.sha256(data).hexdigest() != row["sha256"]
        ):
            raise ArtifactError("Artifact content does not match its frozen manifest.")
        return data

    def collect(self, execution_id, cwd, profile, files, missing, task_id=None):
        cwd = canonical_root(Path(cwd))
        if not cwd.is_relative_to(self.root):
            raise ArtifactError(
                "Collected artifact workspace is outside its owning host."
            )
        if profile not in self.policy.outputs:
            raise ArtifactError("Collection requires the frozen host output rule.")
        if not files:
            return {"state": "empty", "files": [], "missing": missing}
        bundle_id = "art_" + uuid.uuid4().hex
        rows = [
            {
                "source": rule.source,
                "target": rule.target,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "reference": self._reference(),
            }
            for rule, data in files
        ]
        manifest = {
            "bundle_id": bundle_id,
            "execution_id": execution_id,
            "policy": self.policy.fingerprint(),
            "profile": profile.name,
            "workspace": cwd.relative_to(self.root).as_posix(),
            "workspace_identity": [cwd.stat().st_dev, cwd.stat().st_ino],
            "task_id": task_id,
            "owner": self.owner() if callable(self.owner) else self.owner,
            "files": rows,
            "missing": list(missing),
        }
        record = {
            **manifest,
            "format": 2,
            "manifest_sha256": digest(manifest),
            "reference": self._reference(),
            "created_at": time.time(),
            "state": "collecting",
            "revision": 0,
        }
        record["files"] = [{**row, "state": "pending"} for row in rows]
        with storage_gate(self.root):
            # Publish the references before contents, retaining ownership if persistence fails.
            try:
                self.store.put("artifact_bundles", bundle_id, record)
                self._archive(
                    json.dumps(manifest, sort_keys=True).encode(),
                    "artifact_manifest",
                    record["reference"],
                )
                for row, (_, data) in zip(rows, files):
                    check()
                    self._archive(
                        base64.b64encode(data), "artifact_blob", row["reference"]
                    )
                record["state"] = "collected"
                self._save(record)
            except Exception as error:
                with cleanup_scope():
                    try:
                        record["state"] = "collection_unknown"
                        self._save(record)
                    except Exception:
                        pass  # Original durable intent remains the recovery root.
                raise ToolExecutionError(
                    "Artifact persistence was interrupted; inspect this bundle before rerunning Bash.",
                    outcome="unknown",
                    details={"bundle_id": bundle_id, "reference": record["reference"]},
                ) from error
        return self.summary(record)

    def summary(self, bundle):
        return {
            k: copy.deepcopy(bundle[k])
            for k in (
                "bundle_id",
                "manifest_sha256",
                "reference",
                "state",
                "files",
                "missing",
                "revision",
            )
        }

    def list(self, *, workdir, task_id=None, offset=0, limit=20):
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 50
        ):
            raise ArtifactError(
                "Artifact listing needs a nonnegative offset and limit between 1 and 50."
            )
        cwd = canonical_root(Path(workdir))
        if not cwd.is_relative_to(self.root):
            raise ArtifactError("Artifact listing belongs to another workspace.")
        info = cwd.stat()
        scope = {
            "owner": self.owner() if callable(self.owner) else self.owner,
            "task_id": task_id,
            "workspace": cwd.relative_to(self.root).as_posix(),
            "workspace_identity": [info.st_dev, info.st_ino],
            "policy": self.policy.fingerprint(),
        }
        rows = self.store.artifact_page(scope, offset=offset, limit=limit + 1)
        page = []
        for bundle in rows[:limit]:
            self._cwd(bundle, workdir, task_id)
            page.append(self.summary(bundle))
        return {
            "bundles": page,
            "next_offset": offset + limit if len(rows) > limit else None,
        }

    def get(self, bundle_id, *, workdir, task_id=None, path=None):
        bundle = self._load(bundle_id)
        self._cwd(bundle, workdir, task_id)
        if path is None:
            return self.summary(bundle)
        rows = {row["target"]: row for row in bundle["files"]}
        if path not in rows:
            raise ArtifactError("The requested file is not in this artifact bundle.")
        data = self._read_blob(rows[path])
        preview = data[:8000]
        try:
            text = preview.decode("utf-8")
            encoding = "utf-8"
        except UnicodeError:
            text = base64.b64encode(preview).decode()
            encoding = "base64"
        return {
            "path": path,
            "bytes": len(data),
            "sha256": rows[path]["sha256"],
            "preview": redact(text),
            "encoding": encoding,
            "truncated": len(preview) < len(data),
        }

    def approval(self, bundle_id, *, workdir, task_id=None):
        bundle = self._load(bundle_id)
        self._cwd(bundle, workdir, task_id)
        if bundle["state"] == "discarded":
            raise ArtifactError(
                "Discarded artifacts cannot be authorized for publication."
            )
        for row in bundle["files"]:
            self._read_blob(row)
        return self._approval_details(bundle)

    @staticmethod
    def _approval_details(bundle):
        return {
            "bundle_id": bundle["bundle_id"],
            "manifest_sha256": bundle["manifest_sha256"],
            "revision": bundle["revision"],
            "files": [
                {
                    "target": row["target"],
                    "bytes": row["bytes"],
                    "sha256": row["sha256"],
                }
                for row in bundle["files"]
            ],
        }

    def _save(self, bundle):
        # A single SQLite write transaction compares and advances the revision;
        # host execution leases do not act as thread mutexes within one group.
        replacement = copy.deepcopy(bundle)
        expected = bundle["revision"]
        replacement["revision"] = expected + 1
        if not self.store.compare_put(
            "artifact_bundles", bundle["bundle_id"], replacement, expected
        ):
            raise ArtifactError("Artifact publication changed concurrently.")
        bundle["revision"] = replacement["revision"]

    def publish(self, bundle_id, *, workdir, task_id=None, approval=None):
        bundle = self._load(bundle_id)
        cwd = self._cwd(bundle, workdir, task_id)
        if bundle["state"] == "discarded":
            raise ArtifactError("Discarded artifacts cannot be published.")
        if approval != self._approval_details(bundle):
            raise ArtifactError(
                "Publication requires exact current host approval of this manifest."
            )
        committed = []
        started = False
        with storage_gate(self.root), file_update_batch(cwd), ExitStack() as locks:
            for row in sorted(bundle["files"], key=lambda row: row["target"]):
                artifact_path(row["target"], target=True)
                if self.test_runner is not None:
                    from homebrew_harness.execution.source_snapshot import matches

                    if any(
                        matches(row["target"], r.locked_globs)
                        for r in self.test_runner.recipes.values()
                    ):
                        raise ArtifactError(
                            "Publication refuses a protected test or execution definition."
                        )
                locks.enter_context(file_update_lease(cwd, row["target"]))
            bundle = self._load(bundle_id)
            if (
                approval != self._approval_details(bundle)
                or bundle["state"] == "discarded"
            ):
                raise ArtifactError(
                    "Artifact approval became stale before publication."
                )
            # Preflight every destination and freeze validated bytes before writing.
            contents = {}
            for row in bundle["files"]:
                contents[row["target"]] = self._read_blob(row)
                try:
                    with SafeFile(cwd, row["target"]) as target:
                        data, version, _ = target.snapshot(
                            max_bytes=max(row["bytes"], 1)
                        )
                except FileNotFoundError:
                    data = version = None
                except ValueError as error:
                    bundle["state"] = "conflict"
                    self._save(bundle)
                    raise ArtifactError(
                        "Artifact target cannot be safely created; no files were overwritten."
                    ) from error
                if row["state"] == "pending" and version is not None:
                    bundle["state"] = "conflict"
                    self._save(bundle)
                    raise ArtifactError(
                        "Artifact target already exists; no files were overwritten."
                    )
                if row["state"] in {"publishing", "published"}:
                    if data is None and row["state"] == "publishing":
                        row["state"] = "pending"
                    elif (
                        data is None
                        or hashlib.sha256(data).hexdigest() != row["sha256"]
                    ):
                        raise ArtifactError(
                            "Previously published content changed; inspect the bundle before continuing."
                        )
                    else:
                        row["state"] = "published"
                        committed.append(row["target"])
            try:
                for row in bundle["files"]:
                    if row["state"] == "published":
                        continue
                    check()
                    row["state"] = "publishing"
                    bundle["state"] = "publishing"
                    self._save(bundle)
                    started = True
                    data = contents[row["target"]]
                    check()
                    with SafeFile(cwd, row["target"], create_parents=True) as target:
                        self._cwd(bundle, cwd, task_id)
                        check()
                        target.publish(data, None, 0o644)
                    committed.append(row["target"])
                    row["state"] = "published"
                    self._save(bundle)
                bundle["state"] = "published"
                self._save(bundle)
            except BaseException as error:
                outcome = (
                    "unknown"
                    if any(row["state"] == "publishing" for row in bundle["files"])
                    else "partial"
                    if committed
                    else "unknown"
                    if started
                    else None
                )
                if outcome:
                    with cleanup_scope():
                        try:
                            bundle["state"] = outcome
                            self._save(bundle)
                        except Exception:
                            pass
                    if isinstance(error, Exception):
                        raise ToolExecutionError(
                            "Artifact publication interrupted; inspect or resume this bundle, do not rerun Bash.",
                            outcome=outcome,
                            details={
                                "bundle_id": bundle_id,
                                "committed": committed,
                                "reference": bundle["reference"],
                            },
                        ) from error
                raise
        return {
            "bundle_id": bundle_id,
            "state": "published",
            "manifest_sha256": bundle["manifest_sha256"],
            "files": committed,
            "reference": bundle["reference"],
        }

    def run_tool(self, name, *, workdir, task_id=None, **arguments):
        if name == "list_artifacts":
            result = self.list(workdir=workdir, task_id=task_id, **arguments)
        elif name == "get_artifact":
            result = self.get(workdir=workdir, task_id=task_id, **arguments)
        elif name == "publish_artifacts":
            result = self.publish(
                workdir=workdir, task_id=task_id, approval=_APPROVAL.get(), **arguments
            )
        else:
            raise ArtifactError("Unknown artifact operation.")
        return json.dumps(result, ensure_ascii=False)


def host_artifact_list(root, *, offset=0, limit=20):
    """Offline host inventory across sessions; never registered as a model tool."""
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ArtifactError("Host artifact page limit must be between 1 and 50.")
    store = ArtifactStore(root, None)
    rows = store.store.artifact_page(offset=offset, limit=limit + 1)
    return {
        "bundles": [
            {
                **store.summary(row),
                "owner": row["owner"],
                "task_id": row["task_id"],
                "workspace": row["workspace"],
            }
            for row in rows[:limit]
        ],
        "next_offset": offset + limit if len(rows) > limit else None,
    }


def discard_preview(root, bundle_id):
    store = ArtifactStore(root, None)
    bundle = store._load(bundle_id)
    return {
        "format": 1,
        "kind": "artifact_discard",
        "bundle_id": bundle_id,
        "manifest_sha256": bundle["manifest_sha256"],
        "revision": bundle["revision"],
        "state": bundle["state"],
        "files": [
            {"target": row["target"], "state": row["state"], "sha256": row["sha256"]}
            for row in bundle["files"]
        ],
    }


def discard(root, plan):
    from homebrew_harness.storage.storage_admin import maintenance

    if (
        not isinstance(plan, dict)
        or plan.get("kind") != "artifact_discard"
        or plan.get("format") != 1
    ):
        raise ArtifactError("Artifact discard requires a saved host preview.")
    with maintenance(root) as root:
        store = ArtifactStore(root, None)
        bundle = store._load(plan.get("bundle_id"))
        if bundle["state"] == "discarded" and bundle.get("discard_preview") == digest(
            plan
        ):
            return {
                "bundle_id": bundle["bundle_id"],
                "state": "discarded",
                "already_discarded": True,
            }
        if plan != discard_preview(root, bundle["bundle_id"]):
            raise ArtifactError(
                "Artifact discard preview changed; inspect the current bundle."
            )
        # No workspace rollback or immediate content deletion. Other references
        # and pins still protect the immutable manifest and bytes through GC.
        bundle.update(
            state="discarded", discarded_at=time.time(), discard_preview=digest(plan)
        )
        store._save(bundle)
        return {
            "bundle_id": bundle["bundle_id"],
            "state": "discarded",
            "workspace_files_unchanged": True,
        }
