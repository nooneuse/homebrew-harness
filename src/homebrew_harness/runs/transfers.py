"""Declared workspaces, frozen integration manifests and explicit publication."""

import json
import threading
import copy
import time
import uuid
from pathlib import Path

from homebrew_harness.execution.execution_control import control_scope
from homebrew_harness.execution.execution_leases import file_update_lease, lease_scope
from homebrew_harness.execution.source_snapshot import (
    snapshot,
    metadata_boundary,
    prefix_patterns,
    content_digest,
    matches,
)
from homebrew_harness.security.safe_files import SafeFile
from homebrew_harness.storage.control_boundary import check_guard
from homebrew_harness.tools.tool_outcome import ToolExecutionError
from .spec import RunStopped
from .store import owner_guard


def content_identity(value):
    return None if value is None else (value["sha256"], value["executable"])


def allowed(path, prefixes):
    return any(Path(path).is_relative_to(Path(prefix)) for prefix in prefixes)


class StageTransfers:
    def __init__(self, control):
        self.control = control
        self.store = control.store
        self.tasks = control.session.workspace.store
        self.input_locks = {}

    def key(self, task_id):
        return self.control.id + ":" + task_id

    def baseline(self, task_id):
        value = self.store.runtime.get("run_workspaces", self.key(task_id))
        if not value or value["state"] != "ready":
            raise ValueError("Workspace preparation requires host inspection.")
        return value

    def scope_patterns(self, task=None):
        definitions = (
            self.control.spec.stages
            if task is None
            else (self.control.stages.definition(task),)
        )
        prefixes, patterns = [], []
        for definition in definitions:
            for own in definition["write_paths"]:
                for global_path in self.control.spec.write_paths:
                    a, b = Path(own), Path(global_path)
                    if a.is_relative_to(b):
                        prefixes.append(own)
                    elif b.is_relative_to(a):
                        prefixes.append(global_path)
            prefixes.extend(definition["source_paths"])
            prefixes.extend(
                path for item in definition["inputs"] for path in item["paths"]
            )
            for predicate in definition["acceptance"]["predicates"]:
                if "path" in predicate:
                    prefixes.append(predicate["path"])
                if predicate["type"] == "tests_passed":
                    recipe = self.control.session.tests._recipe(predicate["recipe"])
                    patterns.extend((*recipe.input_globs, *recipe.locked_globs))
        if task is None:
            prefixes.extend(self.control.spec.write_paths)
            for predicate in (self.control.spec.goal or {}).get("predicates", []):
                if "path" in predicate:
                    prefixes.append(predicate["path"])
                if predicate["type"] == "tests_passed":
                    recipe = self.control.session.tests._recipe(predicate["recipe"])
                    patterns.extend((*recipe.input_globs, *recipe.locked_globs))
        return tuple(sorted(set((*prefix_patterns(prefixes), *patterns))))

    def prepare_snapshot(self, root, task=None):
        patterns = self.scope_patterns(task)
        if task is not None:
            declared = self.control.stages.definition(task)["source_paths"]
            for prefix in declared:
                if not (root / prefix).exists():
                    raise ValueError(
                        "Declared source dependency is missing from the task worktree: "
                        + prefix
                    )
            if declared:
                watch = prefix_patterns(declared)
                baseline = snapshot(
                    self.store.root, watch, **self.control.spec.snapshot_limits
                )
                copied = snapshot(root, watch, **self.control.spec.snapshot_limits)
                if content_digest(baseline) != content_digest(copied):
                    raise ValueError(
                        "Declared source dependency differs from the clean HEAD worktree."
                    )
        return {
            "scope": list(patterns),
            "snapshot": snapshot(root, patterns, **self.control.spec.snapshot_limits),
            "boundary": metadata_boundary(
                root,
                patterns,
                max_paths=self.control.spec.snapshot_limits["max_paths"],
                git=self.control.session.workspace.worktrees,
            ),
        }

    def current_snapshot(self, task):
        root = self.tasks.workspace(task)
        prepared = self.baseline(task.id if task.worktree else "base")
        patterns = tuple(prepared["scope"])
        expected = self.scope_patterns(task if task.worktree else None)
        if patterns != expected:
            raise ValueError(
                "Prepared snapshot scope changed; inspect the host configuration."
            )
        boundary = metadata_boundary(
            root,
            patterns,
            max_paths=self.control.spec.snapshot_limits["max_paths"],
            git=self.control.session.workspace.worktrees,
        )
        if boundary != prepared["boundary"]:
            raise ValueError(
                "Workspace changed outside its declared snapshot scope or Git baseline."
            )
        return snapshot(root, patterns, **self.control.spec.snapshot_limits)

    def prepare(self):
        session = self.control.session
        session.workspace.acquire_session(self.control.deadline().expires_at)
        with control_scope(self.control.deadline()):
            base_key = self.key("base")
            if not self.store.runtime.get("run_workspaces", base_key):
                self.store.runtime.put(
                    "run_workspaces",
                    base_key,
                    {
                        "run_id": self.control.id,
                        "task_id": "base",
                        "state": "ready",
                        **self.prepare_snapshot(self.store.root),
                        "checkpoint": session.checkpoint_key,
                    },
                )
            for raw in self.control.stages.records().values():
                task = self.tasks.load(raw["id"])
                definition = self.control.stages.definition(task)
                if not definition["isolated"]:
                    continue
                name = "r" + self.control.id[4:12] + "-" + definition["key"]
                key = self.key(task.id)
                prepared = self.store.runtime.get("run_workspaces", key)
                if prepared and prepared["state"] == "ready":
                    if task.worktree != name:
                        raise ValueError("Prepared stage workspace binding changed.")
                    session.workspace.worktrees.resolve(name)
                    continue
                if prepared and task.worktree != name:
                    raise RunStopped("workspace_preparation_required")
                if not prepared:
                    self.store.runtime.put(
                        "run_workspaces",
                        key,
                        {
                            "run_id": self.control.id,
                            "task_id": task.id,
                            "state": "preparing",
                            "checkpoint": session.checkpoint_key,
                        },
                    )
                    session.workspace.worktrees.create(name, task.id)
                task = self.tasks.load(task.id)
                workspace = self.tasks.workspace(task)
                self.store.runtime.put(
                    "run_workspaces",
                    key,
                    {
                        "run_id": self.control.id,
                        "task_id": task.id,
                        "state": "ready",
                        "worktree": name,
                        **self.prepare_snapshot(workspace, task),
                        "checkpoint": session.checkpoint_key,
                    },
                )

    def _task(self, task_id):
        task = self.tasks.load(task_id)
        if not task.stage or task.stage["run_id"] != self.control.id:
            raise ValueError("Integration task does not belong to this Run.")
        spec = self.control.stages.definition(task)
        if spec["executor"] != "lead" or not spec["isolated"] or not spec["integrates"]:
            raise ValueError(
                "Only a declared isolated integration stage can transfer files."
            )
        return task, spec

    def _stopped(self, tasks):
        if self.control.team_control:
            self.control.team_control.stop_tasks({task.id for task in tasks})
        for task in tasks:
            if task.status != "completed" or not self.control.stages.valid(task):
                raise ValueError(
                    "Integration requires current accepted predecessor evidence."
                )

    def _changes(self, task):
        workspace = self.tasks.workspace(task)
        prepared = self.baseline(task.id if task.worktree else "base")
        original = copy.deepcopy(prepared["snapshot"])
        if task.stage.get("input_manifest"):
            manifest = self.store.runtime.get(
                "stage_inputs", task.stage["input_manifest"], {}
            )
            for path, info in manifest.get("files", {}).items():
                if info is None:
                    original.pop(path, None)
                else:
                    original[path] = info
        current = self.current_snapshot(task)
        spec = self.control.stages.definition(task)
        changed = sorted(
            path
            for path in original.keys() | current.keys()
            if content_identity(original.get(path))
            != content_identity(current.get(path))
        )
        if task.worktree and any(
            not allowed(path, spec["write_paths"]) for path in changed
        ):
            raise ValueError(
                "Stage produced changes outside its declared file ownership."
            )
        changed = [path for path in changed if allowed(path, spec["write_paths"])]
        return workspace, current, changed

    def integrate(self, *, workdir, task_id):
        task, spec = self._task(task_id)
        self.tasks.assert_execution(task.id)
        if task.status != "in_progress" or task.owner != self.tasks.owner:
            raise ValueError("Claim the integration stage before applying its inputs.")
        records = self.control.stages.records()
        sources = [
            self.tasks.load(value["id"])
            for value in records.values()
            if value["stage"]["key"] in spec["integrates"]
        ]
        self._stopped(sources)
        target = self.tasks.workspace(task)
        baseline = self.baseline(task.id)["snapshot"]
        changes = {}
        for source in sources:
            root, current, paths = self._changes(source)
            for path in paths:
                if path in changes:
                    raise ValueError(
                        "Multiple predecessors changed the same file; explicit conflict resolution is required."
                    )
                if not allowed(path, spec["write_paths"]):
                    raise ValueError(
                        "Predecessor output exceeds the integration write scope."
                    )
                changes[path] = (root, current.get(path))
        return self._apply("integrate", task, target, baseline, changes)

    def publish(self, *, workdir, task_id):
        task, spec = self._task(task_id)
        if not spec["publish"]:
            raise ValueError("This Run did not authorize publishing this stage.")
        self._stopped([task])
        root, current, paths = self._changes(task)
        if any(not allowed(path, self.control.spec.write_paths) for path in paths):
            raise ValueError("Published files exceed Run write prefixes.")
        return self._apply(
            "publish",
            task,
            self.store.root,
            self.baseline("base")["snapshot"],
            {path: (root, current.get(path)) for path in paths},
        )

    def _apply(self, kind, task, target, baseline, changes):
        self.control.safe_point()
        if any(
            row.get("run_id") == self.control.id
            and row.get("state") in {"started", "unknown", "partial"}
            for row in self.store.runtime.items("run_transfers").values()
        ):
            raise RunStopped("transfer_recovery_required")
        token = self.control.session.inputs.token()
        current = snapshot(
            target, prefix_patterns(changes), **self.control.spec.snapshot_limits
        )
        live = []
        total = 0
        for path, (source, identity) in sorted(changes.items()):
            from homebrew_harness.security.permissions import check_rules
            from homebrew_harness.execution.source_snapshot import matches

            if check_rules(
                "write_file", {"path": path}, target
            ).action != "allow" or any(
                matches(path, recipe.locked_globs)
                for recipe in self.control.session.tests.recipes.values()
            ):
                raise ValueError(
                    "Transfer target is protected by host file or test policy: " + path
                )
            if content_identity(current.get(path)) != content_identity(
                baseline.get(path)
            ) and content_identity(current.get(path)) != content_identity(identity):
                raise ValueError("Target changed from the approved baseline: " + path)
            try:
                with SafeFile(target, path) as item:
                    _, version, mode = item.snapshot()
            except FileNotFoundError:
                version, mode = None, 0o600
            data = None
            if identity is not None:
                with SafeFile(source, identity.get("reference", path)) as item:
                    data, _, source_mode = item.snapshot(max_bytes=8_000_000 - total)
                import hashlib

                if (
                    data is None
                    or hashlib.sha256(data).hexdigest() != identity["sha256"]
                ):
                    raise ValueError(
                        "Source changed while freezing integration output."
                    )
                total += len(data)
                mode = (
                    (0o555 if identity["executable"] else 0o444)
                    if identity.get("reference")
                    else source_mode
                )
            live.append((path, data, version, mode, identity))
        record = {
            "id": "transfer_" + uuid.uuid4().hex,
            "run_id": self.control.id,
            "task_id": task.id,
            "kind": kind,
            "state": "started",
            "revision": 0,
            "target": str(target.relative_to(self.store.root)),
            "before": {path: current.get(path) for path in changes},
            "checkpoint": self.control.session.checkpoint_key,
            "submission": task.stage["submission"],
            "at": time.time(),
            "files": {path: identity for path, _, _, _, identity in live},
            "applied": [],
        }
        db = self.store.runtime.backend()
        with db.write() as conn:
            owner_guard(db, conn, self.control.header)
            check_guard(db, conn, token)
            db._put(conn, "run_transfers", record["id"], record)
        try:
            with lease_scope(self.control.session.workspace.leases):
                for path, data, version, mode, identity in live:
                    self.control.safe_point()
                    if self.control.session.inputs.token() != token:
                        raise ValueError("New input superseded the transfer.")
                    if content_identity(current.get(path)) == content_identity(
                        identity
                    ):
                        continue
                    with (
                        file_update_lease(target, path),
                        SafeFile(target, path, create_parents=True) as item,
                    ):
                        if data is None:
                            item.unlink(version)
                        else:
                            item.publish(data, version, mode)
                    record["applied"].append(path)
                    record["revision"] += 1
                    self.store.runtime.put("run_transfers", record["id"], record)
            record["state"] = "done"
            record["revision"] += 1
            self.store.runtime.put("run_transfers", record["id"], record)
        except Exception as error:
            record["state"] = "partial" if record["applied"] else "unknown"
            record["revision"] += 1
            self.store.runtime.put("run_transfers", record["id"], record)
            raise ToolExecutionError(
                "Transfer interrupted; inspect the frozen manifest and applied paths before continuing.",
                outcome=record["state"],
                details={"transfer_id": record["id"], "applied": record["applied"]},
            ) from error
        return json.dumps(
            {
                "transfer_id": record["id"],
                "kind": kind,
                "files": sorted(changes),
                "state": "done",
            }
        )

    def freeze_outputs(self, task, submission_id):
        """Keep exact submitted bytes for declared consumers and changed outputs."""
        import hashlib

        root, current, changed = self._changes(task)
        definition = self.control.stages.definition(task)
        needed = [
            path
            for consumer in self.control.spec.stages
            for item in consumer["inputs"]
            if item["stage"] == definition["key"]
            for path in item["paths"]
        ]
        selected = set(changed) | {path for path in current if allowed(path, needed)}
        files = {}
        total = 0
        for path in sorted(selected):
            info = current.get(path)
            if info is None:
                files[path] = None
                continue
            with SafeFile(root, path) as item:
                data, _, _ = item.snapshot(max_bytes=8_000_000 - total)
            if hashlib.sha256(data).hexdigest() != info["sha256"]:
                raise ValueError("Stage output changed while freezing its submission.")
            total += len(data)
            reference = ".harness/stage-outputs/blobs/" + info["sha256"]
            with SafeFile(self.store.root, reference, create_parents=True) as blob:
                prior, version, _ = blob.snapshot(max_bytes=8_000_000)
                if prior is None:
                    blob.publish(data, version, 0o400)
                elif prior != data:
                    raise ValueError("Immutable stage output was modified.")
            files[path] = {
                "sha256": info["sha256"],
                "executable": info["executable"],
                "bytes": len(data),
                "reference": reference,
            }
        patterns = self.scope_patterns(task)
        observed = {
            path: info for path, info in current.items() if matches(path, patterns)
        }
        return {"files": files, "observation": content_digest(observed)}

    def outputs_current(self, task, manifest):
        root = self.tasks.workspace(task)
        try:
            current = snapshot(
                root, self.scope_patterns(task), **self.control.spec.snapshot_limits
            )
            if manifest.get("observation") != content_digest(current):
                return False
            return all(
                content_identity(file_identity(root, path)) == content_identity(info)
                for path, info in manifest["files"].items()
            )
        except (OSError, ValueError):
            return False

    def inputs_current(self, task):
        definition = self.control.stages.definition(task)
        if not definition["inputs"]:
            return True
        manifest = self.store.runtime.get(
            "stage_inputs", task.stage.get("input_manifest") or ""
        )
        if (
            not manifest
            or manifest["run_id"] != self.control.id
            or manifest["task_id"] != task.id
            or manifest["scope"] != self.control.evidence_scope()
        ):
            return False
        records = self.control.stages.records()
        if any(
            records.get(key, {}).get("stage", {}).get("submission") != version
            for key, version in manifest["parents"].items()
        ):
            return False
        try:
            root = self.tasks.workspace(task)
            return all(
                content_identity(file_identity(root, path)) == content_identity(info)
                for path, info in manifest["files"].items()
            )
        except (OSError, ValueError):
            return False

    def prepare_inputs(self, task):
        from homebrew_harness.execution.execution_control import acquired

        definition = self.control.stages.definition(task)
        with (
            control_scope(self.control.deadline()),
            acquired(self.input_locks.setdefault(task.id, threading.Lock())),
        ):
            task = self.tasks.load(task.id)
            if self.inputs_current(task):
                return
            records = self.control.stages.records()
            by_key = {record["stage"]["key"]: record for record in records.values()}
            sources = [
                self.tasks.load(by_key[item["stage"]]["id"])
                for item in definition["inputs"]
            ]
            self._stopped(sources)
            changes = {}
            parents = {}
            for item, source in zip(definition["inputs"], sources):
                submission = self.store.runtime.get(
                    "stage_submissions", source.stage["submission"]
                )
                parents[source.id] = source.stage["submission"]
                selected = {
                    path: info
                    for path, info in submission["outputs"]["files"].items()
                    if allowed(path, item["paths"])
                }
                if any(
                    not any(allowed(path, [prefix]) for path in selected)
                    for prefix in item["paths"]
                ):
                    raise ValueError(
                        "Declared predecessor input was not included in its submission."
                    )
                for path, info in selected.items():
                    if path in changes:
                        raise ValueError(
                            "Predecessor inputs conflict on a target path."
                        )
                    changes[path] = (self.store.root, info)
            target = self.tasks.workspace(task)
            baseline = copy.deepcopy(self.baseline(task.id)["snapshot"])
            prior = self.store.runtime.get(
                "stage_inputs", task.stage.get("input_manifest") or "", {}
            )
            for path, info in prior.get("files", {}).items():
                if info is None:
                    baseline.pop(path, None)
                else:
                    baseline[path] = info
            transfer = json.loads(
                self._apply("inputs", task, target, baseline, changes)
            )
            manifest = {
                "id": "inputs_" + uuid.uuid4().hex,
                "run_id": self.control.id,
                "task_id": task.id,
                "checkpoint": self.control.session.checkpoint_key,
                "scope": self.control.evidence_scope(),
                "parents": parents,
                "transfer": transfer["transfer_id"],
                "files": {path: file_identity(target, path) for path in changes},
            }
            if any(
                content_identity(manifest["files"][path]) != content_identity(info)
                for path, (_, info) in changes.items()
            ):
                raise ValueError(
                    "Delivered stage input changed before its metadata commit."
                )
            db = self.store.runtime.backend()
            try:
                with db.write() as conn:
                    owner_guard(db, conn, self.control.header)
                    if db._get(conn, "tasks", task.id) != task.to_dict() or any(
                        db._get(conn, "tasks", key)["stage"]["submission"] != version
                        for key, version in parents.items()
                    ):
                        raise ValueError("Stage changed before input readiness commit.")
                    db._put(conn, "stage_inputs", manifest["id"], manifest)
                    db._put(
                        conn,
                        "tasks",
                        task.id,
                        {
                            **task.to_dict(),
                            "stage": {**task.stage, "input_manifest": manifest["id"]},
                        },
                    )
            except Exception:
                current = self.store.runtime.get("tasks", task.id, {})
                if (
                    current.get("stage", {}).get("input_manifest") != manifest["id"]
                    or self.store.runtime.get("stage_inputs", manifest["id"])
                    != manifest
                ):
                    raise

    def boundary_checks(self):
        result = []
        for task_id in (
            "base",
            *(
                row["id"]
                for row in self.control.stages.records().values()
                if row["worktree"]
            ),
        ):
            prepared = self.baseline(task_id)
            root = (
                self.store.root
                if task_id == "base"
                else self.tasks.workspace(self.tasks.load(task_id))
            )
            try:
                current = metadata_boundary(
                    root,
                    tuple(prepared["scope"]),
                    max_paths=self.control.spec.snapshot_limits["max_paths"],
                    git=self.control.session.workspace.worktrees,
                )
                ok = current == prepared["boundary"]
            except (ValueError, OSError):
                ok = False
            result.append(
                {"id": "boundary_" + task_id, "type": "workspace_boundary", "ok": ok}
            )
        return result

    def publication_checks(self):
        checks = []
        for value in self.control.stages.records().values():
            task = self.tasks.load(value["id"])
            if not self.control.stages.definition(task)["publish"]:
                continue
            rows = [
                r
                for r in self.store.runtime.items("run_transfers").values()
                if r["run_id"] == self.control.id
                and r["task_id"] == task.id
                and r["kind"] == "publish"
                and r["state"] == "done"
                and r["submission"] == task.stage["submission"]
            ]
            latest = max(rows, key=lambda r: r["at"], default=None)
            current = snapshot(
                self.store.root,
                prefix_patterns(latest["files"] if latest else ()),
                **self.control.spec.snapshot_limits,
            )
            ok = latest is not None and all(
                content_identity(current.get(path)) == content_identity(identity)
                for path, identity in latest["files"].items()
            )
            checks.append(
                {
                    "id": "publish_" + task.stage["key"],
                    "type": "publication",
                    "ok": ok,
                    "task_id": task.id,
                }
            )
        return checks


def resolve_transfer(store, run_id, identity, revision, decision):
    """Confirm observed files after host inspection; never replay a transfer."""
    from homebrew_harness.execution.execution_leases import ExecutionLeases
    from homebrew_harness.storage.storage_admin import maintenance
    from .supervisor import RunSupervisor

    if decision not in {"applied", "aborted"}:
        raise ValueError("Choose applied or aborted after inspecting every target.")
    leases = ExecutionLeases(store.root)
    try:
        with (
            leases.use("managed-supervisor"),
            leases.use("managed-services"),
            maintenance(store.root),
        ):
            run = store.get(run_id)
            if not run["cleanup_complete"]:
                raise ValueError("Finish execution cleanup before transfer inspection.")
            supervisor = RunSupervisor(store)
            try:
                if supervisor.worker_alive(run):
                    raise ValueError("The old worker is still alive.")
            finally:
                supervisor.close()
            record = store.runtime.get("run_transfers", identity)
            if (
                not record
                or record["run_id"] != run_id
                or record["revision"] != revision
                or record["state"] not in {"unknown", "partial", "started"}
            ):
                raise ValueError(
                    "Transfer identity, revision or inspection state changed."
                )
            target = store.root / record["target"]
            current = snapshot(
                target,
                prefix_patterns(record["files"]),
                **run["spec"]["snapshot_limits"],
            )
            expected = record["files"] if decision == "applied" else record["before"]
            if any(
                content_identity(current.get(path)) != content_identity(value)
                for path, value in expected.items()
            ):
                raise ValueError(
                    "Actual target files do not match the selected resolution."
                )
            result = {
                **record,
                "state": "done" if decision == "applied" else "aborted",
                "revision": revision + 1,
                "resolution": decision,
            }
            db = store.runtime.backend()
            with db.write() as conn:
                if db._get(conn, "run_transfers", identity) != record:
                    raise ValueError("Transfer changed before resolution commit.")
                db._put(conn, "run_transfers", identity, result)
            return {
                "resolved": True,
                "state": result["state"],
                "next_step": "Inspect any associated unknown tool operation before resuming the Run.",
            }
    finally:
        leases.close()


def file_identity(root, path):
    import hashlib

    try:
        with SafeFile(root, path) as item:
            data, _, mode = item.snapshot(max_bytes=8_000_000)
    except FileNotFoundError:
        return None
    if data is None:
        return None
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "executable": bool(mode & 0o111),
    }
