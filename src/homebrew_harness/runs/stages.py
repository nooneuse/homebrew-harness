"""Host-verified milestones on the shared Task DAG, with immutable handoffs."""

import copy
import json
import re
import time
import uuid
from dataclasses import replace

from homebrew_harness.execution.test_runner import digest
from homebrew_harness.orchestration.hard_conditions import HardVerifier, validate_spec
from homebrew_harness.security.data_boundary import redact
from homebrew_harness.storage.control_boundary import check_guard
from .store import owner_guard

STAGE_TOOLS = {"list_tasks", "get_task", "claim_task", "resume_task", "complete_task"}


def validate_stages(values):
    if not isinstance(values, (tuple, list)) or len(values) > 32:
        raise ValueError("Run stages must contain at most 32 definitions.")
    result, keys = [], set()
    for raw in values:
        if not isinstance(raw, dict) or set(raw) - {
            "key",
            "subject",
            "description",
            "depends_on",
            "acceptance",
            "executor",
            "isolated",
            "write_paths",
            "integrates",
            "publish",
            "inputs",
            "source_paths",
        }:
            raise ValueError("Invalid managed stage fields.")
        key = raw.get("key")
        if (
            not isinstance(key, str)
            or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", key)
            or key in keys
        ):
            raise ValueError("Stage keys must be valid and unique.")
        subject, description = raw.get("subject"), raw.get("description", "")
        if (
            not isinstance(subject, str)
            or not 1 <= len(subject.strip()) <= 500
            or not isinstance(description, str)
            or len(description) > 8000
        ):
            raise ValueError("Stage subject and description must be bounded text.")
        dependencies = raw.get("depends_on", [])
        if (
            not isinstance(dependencies, list)
            or any(not isinstance(k, str) for k in dependencies)
            or len(set(dependencies)) != len(dependencies)
        ):
            raise ValueError("Stage dependencies must be unique keys.")
        acceptance = validate_spec(raw.get("acceptance"))
        if acceptance["judge"] or any(
            p["workspace"] != "base" for p in acceptance["predicates"]
        ):
            raise ValueError(
                "Managed stage acceptance uses base workspace hard predicates and judge=false."
            )
        from pathlib import PurePosixPath

        executor = raw.get("executor", "lead")
        paths = raw.get("write_paths", ["."])
        integrates = raw.get("integrates", [])
        if (
            not isinstance(executor, str)
            or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,39}", executor)
            or type(raw.get("isolated", False)) is not bool
            or type(raw.get("publish", False)) is not bool
            or not isinstance(paths, (list, tuple))
            or not 1 <= len(paths) <= 32
            or any(
                not isinstance(path, str)
                or not path
                or PurePosixPath(path).is_absolute()
                or ".." in PurePosixPath(path).parts
                or "\\" in path
                for path in paths
            )
            or not isinstance(integrates, (list, tuple))
            or any(not isinstance(v, str) for v in integrates)
            or not set(integrates) <= set(dependencies)
        ):
            raise ValueError("Invalid stage ownership or integration policy.")
        if executor != "lead" and (not raw.get("isolated") or "." in paths):
            raise ValueError(
                "Member stages need isolated worktrees and explicit write prefixes."
            )
        if (
            integrates
            and (executor != "lead" or not raw.get("isolated"))
            or raw.get("publish")
            and not integrates
        ):
            raise ValueError(
                "Integration and publication require an isolated Lead integration stage."
            )
        read_paths = raw.get("source_paths", [])
        if (
            not isinstance(read_paths, (list, tuple))
            or len(read_paths) > 32
            or any(
                not isinstance(path, str)
                or not path
                or len(path) > 1000
                or PurePosixPath(path).is_absolute()
                or ".." in PurePosixPath(path).parts
                or "\\" in path
                for path in read_paths
            )
        ):
            raise ValueError("Stage source_paths must be bounded relative prefixes.")
        inputs = raw.get("inputs", [])
        if not isinstance(inputs, (list, tuple)) or len(inputs) > 16:
            raise ValueError("Stage inputs must be a bounded source list.")
        normalized_inputs = []
        for item in inputs:
            if (
                not isinstance(item, dict)
                or set(item) != {"stage", "paths"}
                or item["stage"] not in dependencies
            ):
                raise ValueError(
                    "Stage inputs must reference direct declared predecessors."
                )
            source_paths = item["paths"]
            if (
                not isinstance(source_paths, (list, tuple))
                or not 1 <= len(source_paths) <= 32
                or any(
                    not isinstance(path, str)
                    or not path
                    or PurePosixPath(path).is_absolute()
                    or ".." in PurePosixPath(path).parts
                    or "\\" in path
                    for path in source_paths
                )
            ):
                raise ValueError("Stage input paths must be bounded relative prefixes.")
            if any(
                PurePosixPath(path).is_relative_to(PurePosixPath(write))
                or PurePosixPath(write).is_relative_to(PurePosixPath(path))
                for path in source_paths
                for write in paths
            ):
                raise ValueError(
                    "Read-only stage inputs cannot overlap writable output prefixes."
                )
            normalized_inputs.append(
                {"stage": item["stage"], "paths": list(source_paths)}
            )
        if normalized_inputs and (not raw.get("isolated") or integrates):
            raise ValueError(
                "Explicit inputs require an isolated non-integration consumer."
            )
        result.append(
            {
                "key": key,
                "subject": subject.strip(),
                "description": description,
                "depends_on": list(dependencies),
                "acceptance": acceptance,
                "executor": executor,
                "isolated": raw.get("isolated", False),
                "write_paths": list(paths),
                "integrates": list(integrates),
                "publish": raw.get("publish", False),
                "inputs": normalized_inputs,
                "source_paths": list(dict.fromkeys(read_paths)),
            }
        )
        keys.add(key)
    from .transfers import allowed

    definitions = {value["key"]: value for value in result}
    for value in result:
        for item in value["inputs"]:
            producer = definitions.get(item["stage"])
            if producer is None or any(
                not allowed(path, producer["write_paths"]) for path in item["paths"]
            ):
                raise ValueError(
                    "Stage input exceeds the producer's declared output scope."
                )
    graph = {v["key"]: v["depends_on"] for v in result}
    for key, edges in graph.items():
        if set(edges) - keys:
            raise ValueError("Stage dependency does not exist.")
        pending, seen = list(edges), set()
        while pending:
            value = pending.pop()
            if value == key:
                raise ValueError("Stage dependency cycle detected.")
            if value not in seen:
                seen.add(value)
                pending.extend(graph[value])
    if redact(result) != result:
        raise ValueError("Stage definitions contain private connection values.")
    return tuple(result)


def validate_task_stage(stage):
    if not isinstance(stage, dict) or set(stage) != {
        "run_id",
        "key",
        "acceptance",
        "version",
        "submission",
        "handoff",
        "input_manifest",
    }:
        raise ValueError("Invalid managed Task stage record.")
    from .store import run_id

    run_id(stage["run_id"])
    if digest(stage["acceptance"]) != stage["version"]:
        raise ValueError("Managed stage acceptance version changed.")
    validate_spec(stage["acceptance"])


def create_tasks(db, conn, run, stages):
    """Called inside Run creation; no separate partially installed stage plan."""
    from homebrew_harness.orchestration.task_store import Task

    identities = {}
    for item in stages:
        while True:
            identity = "task_" + uuid.uuid4().hex[:8]
            if (
                identity not in identities.values()
                and db._get(conn, "tasks", identity) is None
            ):
                break
        identities[item["key"]] = identity
    for item in stages:
        task = Task(
            identities[item["key"]],
            item["subject"],
            item["description"],
            "pending",
            None,
            tuple(identities[k] for k in item["depends_on"]),
            stage={
                "run_id": run["id"],
                "key": item["key"],
                "acceptance": item["acceptance"],
                "version": digest(item["acceptance"]),
                "submission": None,
                "handoff": None,
                "input_manifest": None,
            },
        )
        db._put(conn, "tasks", task.id, task.to_dict())


def stage_records(store, run_id):
    store.get(run_id)
    return [
        r
        for r in store.runtime.items("tasks").values()
        if (r.get("stage") or {}).get("run_id") == run_id
    ]


def completion_guard(db, conn, run, expected):
    """Final Run commit checks the exact DAG records that were verified."""
    rows = conn.execute(
        "SELECT key,value FROM records WHERE section='tasks'"
    ).fetchall()
    current = {
        key: json.loads(raw)
        for key, raw in rows
        if (json.loads(raw).get("stage") or {}).get("run_id") == run["id"]
    }
    if (
        not expected
        or current != expected
        or any(v["status"] != "completed" for v in current.values())
    ):
        from homebrew_harness.storage.control_boundary import ProposalSuperseded

        raise ProposalSuperseded(
            "Managed stages changed or remain unverified before completion."
        )


class StageController:
    def __init__(self, control):
        self.control = control
        self.session = control.session
        self.tasks = self.session.workspace.store
        self.tasks.stage_control = self
        self.verifier = self.session.goal.hard_verifier
        self.test_runner = self.session.tests
        self.session.goal.hard_verifier = self
        self.last_verified = None

    def records(self):
        records = {
            v["id"]: v for v in stage_records(self.control.store, self.control.id)
        }
        keys = {v["stage"]["key"]: v["id"] for v in records.values()}
        if len(keys) != len(records) or set(keys) != {
            s["key"] for s in self.control.spec.stages
        }:
            raise ValueError("Managed stage definitions are missing or duplicated.")
        for spec in self.control.spec.stages:
            record = records[keys[spec["key"]]]
            if (
                record["stage"]["acceptance"] != spec["acceptance"]
                or record["stage"]["version"] != digest(spec["acceptance"])
                or set(record["blockedBy"]) != {keys[k] for k in spec["depends_on"]}
            ):
                raise ValueError("Managed stage binding changed.")
        return records

    def definition(self, task):
        return next(
            value
            for value in self.control.spec.stages
            if value["key"] == task.stage["key"]
        )

    def ready(self, task):
        records = self.records()
        return all(
            records[key]["status"] == "completed" and self.valid(self.tasks.load(key))
            for key in task.blockedBy
        )

    def prepare_claim(self, task_id, *, actor="lead", allow_prepare=True, resume=False):
        task = self.tasks.load(task_id)
        definition = self.definition(task)
        if task.status != "pending" and not (resume and task.status == "in_progress"):
            raise ValueError("Stage is not pending for a new claim.")
        if definition["executor"] != actor or task.stage["run_id"] != self.control.id:
            raise ValueError("Stage claim does not match its declared executor.")
        if not self.ready(task):
            raise ValueError("Stage is blocked: dependencies are not accepted yet.")
        if definition["inputs"] and not self.control.transfers.inputs_current(task):
            if not allow_prepare:
                raise ValueError(
                    "Host input preparation changed before task acceptance."
                )
            self.control.transfers.prepare_inputs(task)

    def guard(self, task, *, dependencies=True, actor="lead"):
        if self.definition(task)["executor"] != actor:
            raise ValueError("This stage belongs to its declared executor.")
        if (
            task.stage["run_id"] != self.control.id
            or self.session.workspace.store is not self.tasks
        ):
            raise ValueError("Task belongs to another managed Run.")
        self.control.safe_point()
        records = self.records()
        if self.definition(task)[
            "inputs"
        ] and not self.control.transfers.inputs_current(task):
            raise ValueError("Stage input preparation is required before execution.")
        if not dependencies:
            return
        pending, seen = list(task.blockedBy), set()
        while pending:
            key = pending.pop()
            if key in seen:
                continue
            seen.add(key)
            prior = self.tasks._load(key)
            if prior.status != "completed" or not self.valid(prior):
                raise ValueError("Task is blocked by unverified predecessor: " + key)
            pending.extend(records[key]["blockedBy"])

    def scope(self):
        return self.control.evidence_scope()

    def _check(self, task):
        outer = self

        class Tests:
            def verify(self, name, cwd, **_kwargs):
                return outer.session.tests.verify(name, cwd, task_id=task.id)

            def authorization_available(self, name, cwd, _task=None):
                return outer.session.tests.authorization_available(name, cwd, task.id)

        return HardVerifier(self.tasks.workspace(task), test_runner=Tests()).check(
            task.stage["acceptance"]
        )

    def complete(
        self, task, *, summary="", next_steps="", actor="lead", actor_store=None
    ):
        owner_store = actor_store or self.tasks
        for value in (summary, next_steps):
            if (
                not isinstance(value, str)
                or len(value) > 8000
                or redact(value) != value
            ):
                raise ValueError(
                    "Stage submission text must be bounded and contain no private connection values."
                )
        self.guard(task, actor=actor)
        if any(
            v.get("run_id") == self.control.id
            and v.get("state") in {"started", "unknown", "partial"}
            for v in self.control.store.runtime.items("run_transfers").values()
        ):
            raise ValueError("Stage acceptance requires transfer inspection.")
        ignored = {"goal"} | (
            {"team", "team_events", "team_busy"} if actor != "lead" else set()
        )
        pending = [v for v in self.control.pending_work() if v not in ignored]
        # This complete_task operation itself is allowed; unknown prior operations are not.
        from homebrew_harness.storage.durable_runtime import _OPERATION

        operation = _OPERATION.get()
        uncertain = [
            r
            for r in self.session.recovery_operations()
            if (not operation or r["operation_id"] != operation[1])
            and not (
                actor != "lead"
                and r.get("active")
                and r.get("state") == "started"
                and r.get("checkpoint", "").startswith("member:")
            )
        ]
        if pending or self.session._recovery_blocked or uncertain:
            raise ValueError(
                "Stage acceptance awaits pending work or operation inspection."
            )
        token = (
            self.control.team_control.proposal_token(actor)
            if actor != "lead"
            else self.session.inputs.token()
        )
        checked = self._check(task)
        submission_id = "submission_" + uuid.uuid4().hex
        handoff_id = "handoff_" + uuid.uuid4().hex
        records = self.records()
        output_manifest = (
            self.control.transfers.freeze_outputs(task, submission_id)
            if checked["ok"] and self.control.transfers
            else {"files": {}}
        )
        submission = {
            "id": submission_id,
            "run_id": self.control.id,
            "task_id": task.id,
            "checkpoint": self.session.checkpoint_key,
            "at": time.time(),
            "scope": self.scope(),
            "acceptance_version": task.stage["version"],
            "verification": checked,
            "outputs": output_manifest,
            "input_manifest": task.stage["input_manifest"],
            "proposal": {"summary": summary, "next_steps": next_steps},
        }
        completed = replace(
            task,
            status="completed",
            stage={**task.stage, "submission": submission_id, "handoff": handoff_id},
        )
        from homebrew_harness.storage.retention_graph import references

        history = [
            value["id"]
            for value in self.control.store.runtime.items("stage_submissions").values()
            if value["task_id"] == task.id and value["run_id"] == self.control.id
        ]
        handoff = {
            "id": handoff_id,
            "run_id": self.control.id,
            "task_id": task.id,
            "checkpoint": self.session.checkpoint_key,
            "submission": submission_id,
            "task_intent": self.session.intent.to_dict(),
            "stage": task.to_dict(),
            "git_head": self.control.state["git_head"],
            "verification": checked,
            "proposal": submission["proposal"],
            "predecessors": [records[k]["stage"]["handoff"] for k in task.blockedBy],
            "submission_history": history,
            "archive_refs": sorted(references(self.session.messages)),
        }
        db = self.control.store.runtime.backend()
        try:
            with db.write() as conn:
                owner_guard(db, conn, self.control.header)
                check_guard(db, conn, token)
                if db._get(conn, "tasks", task.id) != task.to_dict():
                    raise ValueError("Task changed before acceptance.")
                if any(
                    db._get(conn, "tasks", key) != value
                    for key, value in records.items()
                ):
                    raise ValueError("Stage DAG changed before acceptance.")
                db._put(conn, "stage_submissions", submission_id, submission)
                if checked["ok"]:
                    # Recheck observed file/test identities at the last host boundary.
                    if self._check(task) != checked or (
                        self.control.transfers
                        and not self.control.transfers.outputs_current(
                            task, output_manifest
                        )
                    ):
                        raise ValueError("Stage evidence changed before acceptance.")
                    db._put(conn, "stage_handoffs", handoff_id, handoff)
                    db._put(conn, "tasks", task.id, completed.to_dict())
        except Exception:
            if (
                db.get("stage_submissions", submission_id) != submission
                or checked["ok"]
                and db.get("tasks", task.id) != completed.to_dict()
            ):
                raise
        if not checked["ok"]:
            raise ValueError(
                "Stage acceptance failed; dependencies remain blocked: "
                + json.dumps(checked)
            )
        owner_store._release_execution(task.id)
        updated = {k: self.tasks.load(k) for k in records}
        before_ready = {
            k
            for k, v in records.items()
            if v["status"] == "pending"
            and all(records[d]["status"] == "completed" for d in v["blockedBy"])
        }
        unblocked = [
            v
            for k, v in updated.items()
            if v.status == "pending"
            and k not in before_ready
            and not self.tasks._incomplete(v, updated)
        ]
        return completed, unblocked

    def valid(self, task):
        submission = self.control.store.runtime.get(
            "stage_submissions", task.stage["submission"] or ""
        )
        return bool(
            submission
            and submission["scope"] == self.scope()
            and submission["acceptance_version"] == task.stage["version"]
            and submission["input_manifest"] == task.stage["input_manifest"]
            and (
                not self.control.transfers
                or (
                    self.control.transfers.inputs_current(task)
                    and self.control.transfers.outputs_current(
                        task, submission["outputs"]
                    )
                )
            )
            and submission["verification"]["ok"]
            and self._check(task) == submission["verification"]
        )

    def reconcile(self):
        from homebrew_harness.context.tracked_history import set_context

        def invalidated(records):
            invalid = (
                set()
                if self.session.tests.active.is_set()
                else {
                    key
                    for key, value in records.items()
                    if value["status"] == "completed"
                    and not self.valid(self.tasks._load(key))
                }
            )
            while True:
                dependents = {
                    key
                    for key, value in records.items()
                    if set(value["blockedBy"]) & invalid
                }
                if dependents <= invalid:
                    return invalid
                invalid |= dependents

        with self.tasks._lock():
            records = self.records()
            invalid = invalidated(records)
        # Member cleanup itself needs the task lock. Join only after releasing it.
        if invalid and self.control.team_control is not None:
            self.control.team_control.stop_tasks(invalid)
        with self.tasks._lock():
            records = self.records()
            invalid = invalidated(records)
            if invalid:
                db = self.control.store.runtime.backend()
                with db.write() as conn:
                    owner_guard(db, conn, self.control.header)
                    for key in invalid:
                        if db._get(conn, "tasks", key) != records[key]:
                            raise ValueError(
                                "Stage changed during evidence invalidation."
                            )
                        db._put(
                            conn,
                            "tasks",
                            key,
                            {
                                **records[key],
                                "status": "pending",
                                "owner": None,
                                "stage": {
                                    **records[key]["stage"],
                                    "submission": None,
                                    "handoff": None,
                                },
                            },
                        )
                for key in invalid:
                    self.tasks._release_execution(key)
                if (
                    self.session.workspace.lead_assignment
                    and self.session.workspace.lead_assignment.id in invalid
                ):
                    self.session.workspace.lead_assignment = None
            records = self.records()
        summaries = [
            {
                "task_id": k,
                "key": v["stage"]["key"],
                "status": v["status"],
                "blockedBy": v["blockedBy"],
                "handoff": v["stage"]["handoff"],
                "executor": self.definition(self.tasks._load(k))["executor"],
                "write_paths": self.definition(self.tasks._load(k))["write_paths"],
                "inputs": self.definition(self.tasks._load(k))["inputs"],
                "source_paths": self.definition(self.tasks._load(k))["source_paths"],
            }
            for k, v in records.items()
        ]
        set_context(
            self.session.messages,
            "run_stages",
            {
                "role": "user",
                "name": "run_stages",
                "content": "Host stages: read get_task before claim/resume. complete_task submits hard acceptance; only host success unlocks dependents. Read predecessor get_handoff before continuing. Invalidated stages must be claimed and verified again. "
                + json.dumps({"tasks": summaries, "invalidated": sorted(invalid)}),
            },
        )

    def check(self, spec, *, since=0):
        resolved = copy.deepcopy(spec)
        for predicate in resolved["predicates"]:
            if predicate.get("workspace", "").startswith("stage:"):
                key = predicate["workspace"][6:]
                predicate["workspace"] = next(
                    record["id"]
                    for record in self.records().values()
                    if record["stage"]["key"] == key
                )
        result = self.verifier.check(resolved, since=since)
        records = self.records()
        checks = [
            {
                "id": "stage_" + v["stage"]["key"],
                "type": "stage",
                "ok": v["status"] == "completed" and self.valid(self.tasks.load(k)),
                "task_id": k,
            }
            for k, v in records.items()
        ]
        if self.control.transfers is not None:
            if all(item["ok"] for item in checks):
                checks.extend(self.control.transfers.boundary_checks())
            checks.extend(self.control.transfers.publication_checks())
        self.last_verified = (
            copy.deepcopy(records) if checks and all(c["ok"] for c in checks) else None
        )
        return {
            "ok": result["ok"] and bool(checks) and all(c["ok"] for c in checks),
            "results": result["results"] + checks,
        }

    def read_member_handoff(self, member, *, workdir, handoff_id):
        if member.assignment is None or workdir.resolve() != member.cwd():
            raise ValueError("Handoff read requires the member's assigned workspace.")
        task = self.tasks.load(member.assignment.id)
        manifest = self.control.store.runtime.get(
            "stage_inputs", task.stage["input_manifest"] or "", {}
        )
        value = self.control.store.runtime.get("stage_handoffs", handoff_id)
        if (
            not value
            or value["run_id"] != self.control.id
            or manifest.get("parents", {}).get(value["task_id"]) != value["submission"]
        ):
            raise ValueError("Member can read only its delivered predecessor handoffs.")
        return self.read_handoff(workdir=workdir, handoff_id=handoff_id)

    def read_handoff(self, *, workdir, handoff_id):
        value = self.control.store.runtime.get("stage_handoffs", handoff_id)
        if not value or value["run_id"] != self.control.id:
            raise ValueError("Handoff does not belong to this Run.")
        history = [
            self.control.store.runtime.get("stage_submissions", key)
            for key in value["submission_history"]
        ]
        if any(
            row is None
            or row["run_id"] != self.control.id
            or row["task_id"] != value["task_id"]
            for row in history
        ):
            raise ValueError("Handoff submission history is incomplete.")
        return json.dumps(
            {**value, "previous_submissions": history}, ensure_ascii=False
        )


class MemberStageAccess:
    def __init__(self, controller, actor, store):
        self.controller, self.actor, self.store = controller, actor, store

    def prepare_claim(self, task_id):
        return self.controller.prepare_claim(
            task_id, actor=self.actor, allow_prepare=False
        )

    def guard(self, task, *, dependencies=True):
        return self.controller.guard(task, dependencies=dependencies, actor=self.actor)

    def complete(self, task, **kwargs):
        return self.controller.complete(
            task, actor=self.actor, actor_store=self.store, **kwargs
        )
