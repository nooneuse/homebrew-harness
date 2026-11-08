"""Task-scoped fixed-test grants backed by the Run acceptance transaction."""

import copy
import math
import os
import time

from homebrew_harness.execution.test_runner import Grant, digest as input_digest
from homebrew_harness.execution.source_snapshot import snapshot
from homebrew_harness.storage.storage_gate import storage_gate
from .requests import digest
from .store import owner_guard


def scope(control, name, cwd, execution):
    recipe = control.session.tests._recipe(name)
    task = (
        control.session.workspace.store.load(execution.task_id)
        if execution.task_id
        else None
    )
    member = (
        control.stages.definition(task)["executor"]
        if task and control.stages
        else "lead"
    )
    if (
        execution.role not in recipe.roles
        or execution.role != ("lead" if member == "lead" else "teammate")
        or task
        and (
            task.status != "in_progress"
            or task.owner != ("agent" if member == "lead" else member)
        )
    ):
        raise ValueError("Test authorization must match the actual task executor.")
    expected_cwd = (
        control.store.root / ".worktrees" / task.worktree
        if task and task.worktree
        else control.store.root
    )
    if cwd != expected_cwd:
        raise ValueError("Test workspace must belong to the actual assigned task.")
    info = os.stat(cwd, follow_symlinks=False)
    intent = control.session.intent.to_dict()
    if not intent.get("original"):
        intent = {"original": control.spec.prompt, "amendments": []}
    return {
        "spec": digest(control.spec.data()),
        "control": control.state.get("control_identity"),
        "member": member,
        "role": execution.role,
        "task_id": execution.task_id,
        "cwd": str(cwd.relative_to(control.store.root)),
        "workspace_identity": [info.st_dev, info.st_ino],
        "intent": input_digest(intent),
        "epoch": control.session.inputs.token()[control.session.inputs.recipient],
        "input_manifest": task.stage.get("input_manifest")
        if task and task.stage
        else None,
        "test": control.session.tests.approval_identity(name, cwd),
        "locked_globs": list(recipe.locked_globs),
        "snapshot_limits": recipe.snapshot_limits,
        "recipe": name,
        "max_runs": recipe.max_runs,
        "approval_seconds": recipe.approval_seconds,
    }


def valid(store, run, ctx, *, conn=None, db=None, scheduling=False):
    db = db or store.runtime.backend()
    get = (
        (lambda section, key, default=None: db._get(conn, section, key, default))
        if conn
        else store.runtime.get
    )
    if run["status"] in {"completed", "cancelled", "failed"} or ctx["spec"] != digest(
        run["spec"]
    ):
        return False
    if ctx["control"] != run.get("control_identity") or ctx["epoch"] != get(
        "input_epochs", "user_" + run["id"], 0
    ):
        return False
    intent = get("run_input_applied", run["id"], {}).get("intent")
    if intent and ctx["intent"] != intent:
        return False
    task = get("tasks", ctx["task_id"]) if ctx["task_id"] else None
    if task and (
        not (
            task["status"] == "in_progress"
            and task["owner"] == ("agent" if ctx["member"] == "lead" else ctx["member"])
            or scheduling
            and task["status"] == "pending"
            and task["owner"] is None
        )
        or (task.get("stage") or {}).get("run_id") != run["id"]
        or (task.get("stage") or {}).get("input_manifest") != ctx["input_manifest"]
    ):
        return False
    if ctx["task_id"] and task is None:
        return False
    if task:
        expected = ".worktrees/" + task["worktree"] if task["worktree"] else "."
        if ctx["cwd"] != expected:
            return False
        definition = next(
            (
                item
                for item in run["spec"]["stages"]
                if item["key"] == task["stage"]["key"]
            ),
            None,
        )
        if (
            not definition
            or definition["executor"] != ctx["member"]
            or ctx["role"] != ("lead" if ctx["member"] == "lead" else "teammate")
        ):
            return False
    root = store.root / ctx["cwd"]
    try:
        if (
            root.is_symlink()
            or root.resolve() != root
            or not root.is_relative_to(store.root)
        ):
            return False
        info = os.stat(root, follow_symlinks=False)
        return [info.st_dev, info.st_ino] == ctx["workspace_identity"]
    except OSError:
        return False


def locked_current(store, ctx):
    return {
        p: v["sha256"]
        for p, v in snapshot(
            store.root / ctx["cwd"],
            tuple(ctx["locked_globs"]),
            **ctx["snapshot_limits"],
        ).items()
    } == ctx["test"]["locked"]


class TestAuthorizations:
    def __init__(self, control):
        self.control = control
        self.spent = set()

    def grant(self, recipe, cwd, execution):
        from homebrew_harness.storage.durable_runtime import current_operation

        c = self.control
        key = c.test_grant_key(recipe.name, cwd)
        state = c.store.get(c.id)
        grant = state["test_grants"].get(key)
        if not grant:
            return None
        ctx = scope(c, recipe.name, cwd, execution)
        if grant["identity"] != ctx["test"]:
            return None
        if grant.get("context") is None:
            db = c.store.runtime.backend()
            with storage_gate(c.store.root), db.write() as conn:
                state = owner_guard(db, conn, c.header)
                grant = copy.deepcopy(state["test_grants"][key])
                if grant.get("context") is None:
                    grant["context"] = ctx
                    db._put(
                        conn,
                        "runs",
                        c.id,
                        {
                            **state,
                            "test_grants": {**state["test_grants"], key: grant},
                            "revision": state["revision"] + 1,
                        },
                    )
        if grant.get("context") != ctx or not valid(c.store, state, ctx):
            return None
        operation = current_operation()
        record = (
            c.store.runtime.get("operations", operation[1], {})
            if operation and operation[0] == str(c.store.root)
            else {}
        )
        reserved = (
            record.get("state") == "started"
            and record.get("test_grant_id") == grant["id"]
            and record.get("test_grant_context") == ctx
            and operation[1] not in self.spent
        )
        remaining = grant["max_runs"] - grant["used"]
        if not reserved and (remaining <= 0 or grant["expires_at"] <= time.time()):
            return None
        return Grant(
            recipe.name,
            grant["identity"]["fingerprint"],
            grant["identity"]["locked"],
            (execution.role,),
            str(cwd),
            execution.task_id,
            time.monotonic()
            + (
                max(1, c.deadline().remaining())
                if reserved
                else grant["expires_at"] - time.time()
            ),
            1 if reserved else remaining,
        )

    def available(self, recipe, cwd, task_id=None):
        c = self.control
        state = c.store.get(c.id)
        grant = state["test_grants"].get(c.test_grant_key(recipe.name, cwd))
        if (
            not grant
            or state["status"] in {"completed", "cancelled", "failed"}
            or grant["used"] >= grant["max_runs"]
            or grant["expires_at"] <= time.time()
        ):
            return False
        if grant["identity"] != c.session.tests.approval_identity(recipe.name, cwd):
            return False
        ctx = grant.get("context")
        if ctx is not None:
            if task_id is not None and ctx["task_id"] != task_id:
                return False
            return valid(c.store, state, ctx, scheduling=True)
        if task_id is None and c.stages:
            task_id = next(
                (
                    row["id"]
                    for row in c.stages.records().values()
                    if row["worktree"]
                    and c.store.root / ".worktrees" / row["worktree"] == cwd
                ),
                None,
            )
        if task_id:
            task = c.session.workspace.store.load(task_id)
            expected = (
                c.store.root / ".worktrees" / task.worktree
                if task.worktree
                else c.store.root
            )
            role = (
                "lead"
                if c.stages.definition(task)["executor"] == "lead"
                else "teammate"
            )
            return (
                cwd == expected
                and role in recipe.roles
                and task.status in {"pending", "in_progress"}
            )
        return cwd == c.store.root and "lead" in recipe.roles

    def consume_projection(self):
        from homebrew_harness.storage.durable_runtime import current_operation

        operation = current_operation()
        if operation:
            if operation[1] in self.spent:
                raise ValueError("A test reservation cannot execute twice.")
            self.spent.add(operation[1])

    def operation_fields(self, name, cwd):
        key = self.control.test_grant_key(name, cwd)
        grant = self.control.store.get(self.control.id)["test_grants"].get(key)
        return (
            {"test_grant_id": grant["id"], "test_grant_context": grant["context"]}
            if grant
            else {}
        )

    def request(self, name, cwd, execution):
        c = self.control
        if (
            name not in c.spec.allow_tests
            or execution.role != "teammate"
            or not execution.task_id
        ):
            raise ValueError(
                "Renewal is limited to the member's registered fixed tests."
            )
        task = c.session.workspace.store.load(execution.task_id)
        if not any(
            p.get("recipe") == name
            for p in c.stages.definition(task)["acceptance"]["predicates"]
        ):
            raise ValueError("Test recipe is outside the assigned stage.")
        ctx = scope(c, name, cwd, execution)
        from homebrew_harness.storage.control_boundary import ProposalSuperseded

        if (
            c.team_control.proposal_token(ctx["member"])[c.session.inputs.recipient]
            != ctx["epoch"]
        ):
            raise ProposalSuperseded(
                "User input superseded the member's authorization proposal."
            )
        return c.requests.create(
            c,
            "test_grant",
            tool="run_tests",
            arguments={"name": name},
            reason="Member test authorization expired or exhausted",
            grant_context=ctx,
        )


def decide(store, run_id, identity, revision, action, *, runs=None, seconds=None):
    from .effects import enqueue

    request = store.runtime.get("run_requests", identity)
    ctx = request["context"]
    runs = 1 if runs is None else runs
    seconds = ctx["approval_seconds"] if seconds is None else seconds
    if (
        action not in {"approve", "deny"}
        or type(runs) is not int
        or not 1 <= runs <= ctx["max_runs"]
        or type(seconds) not in {int, float}
        or not math.isfinite(seconds)
        or not 0 < seconds <= ctx["approval_seconds"]
    ):
        raise ValueError(
            "Renewal must remain within the registered run and time limits."
        )
    decision = {"action": action, "answer": None, "runs": runs, "seconds": seconds}
    if request.get("decision") == decision and request.get("decision_from") == revision:
        return request
    if not locked_current(store, ctx):
        raise ValueError("Protected test inputs changed before renewal.")
    db = store.runtime.backend()
    try:
        with storage_gate(store.root), db.write() as conn:
            run = db._get(conn, "runs", run_id)
            request = db._get(conn, "run_requests", identity)
            if (
                request.get("decision") == decision
                and request.get("decision_from") == revision
            ):
                return request
            if (
                request["state"] != "pending"
                or request["revision"] != revision
                or time.time() > request["expires_at"]
                or not valid(store, run, ctx, conn=conn)
            ):
                raise ValueError(
                    "Test renewal changed, expired or belongs to stale work."
                )
            if conn.execute(
                "SELECT count(*) FROM inbox WHERE recipient=?", ("user_" + run_id,)
            ).fetchone()[0]:
                raise ValueError(
                    "Apply pending user revisions before renewing an old test request."
                )
            control = db._get(conn, "run_controls", run_id, {"action": "run"})
            if control["action"] in {"pause", "cancel"}:
                raise ValueError("Resolve the host control before granting tests.")
            updated = {
                **request,
                "state": "consumed" if action == "approve" else "denied",
                "revision": revision + 1,
                "decision": decision,
                "decision_from": revision,
                "decided_at": time.time(),
            }
            db._put(conn, "run_requests", identity, updated)
            db._put(
                conn,
                "run_decisions",
                identity,
                {
                    "request_id": identity,
                    "run_id": run_id,
                    "checkpoint": request["checkpoint"],
                    "revision": revision + 1,
                    **decision,
                },
            )
            if action == "approve":
                task = db._get(conn, "tasks", ctx["task_id"])
                key = (
                    ctx["recipe"] + "@" + task["id"]
                    if task["worktree"]
                    else ctx["recipe"]
                )
                grant = {
                    "id": identity,
                    "identity": ctx["test"],
                    "context": ctx,
                    "used": 0,
                    "max_runs": runs,
                    "expires_at": min(run["deadline_at"], time.time() + seconds),
                }
                db._put(
                    conn,
                    "runs",
                    run_id,
                    {
                        **run,
                        "test_grants": {**run["test_grants"], key: grant},
                        "revision": run["revision"] + 1,
                    },
                )
            db._send(
                conn,
                ctx["member"],
                {
                    "id": identity + ":decision",
                    "from": "lead",
                    "to": ctx["member"],
                    "type": "test_grant_decision",
                    "content": "Fixed-test authorization decision recorded; recheck the task and grant before execution.",
                    "ts": time.time(),
                    "metadata": {
                        "origin": "managed_test_authorization",
                        "run_id": run_id,
                        "request_id": identity,
                    },
                },
            )
            enqueue(
                db,
                conn,
                run,
                "wake",
                {"request_id": identity, "decision_revision": revision + 1},
                identity,
            )
    except Exception:
        saved = store.runtime.get("run_requests", identity, {})
        if saved.get("decision") != decision or saved.get("decision_from") != revision:
            raise
        return saved
    return updated


def accept(db, conn, state, record):
    from .spec import RunAuthorizationLimit

    name = record["test_recipe"]
    key = record.get("test_grant_key", name)
    grant = copy.deepcopy(state["test_grants"].get(key))
    attempts = state.get("test_attempts", {})
    if sum(attempts.values()) >= state["test_limits"]["max_runs"]:
        raise RunAuthorizationLimit("Run's fixed-test execution limit is exhausted.")
    if not record.get("approval_id"):
        if (
            not grant
            or grant["id"] != record.get("test_grant_id")
            or grant["context"] != record.get("test_grant_context")
            or grant["expires_at"] <= time.time()
            or grant["used"] >= grant["max_runs"]
        ):
            raise RunAuthorizationLimit("Fixed-test grant changed or is exhausted.")
        # Validation reads the current task/epoch under the same acceptance transaction.
        from types import SimpleNamespace

        if not valid(
            SimpleNamespace(root=db.workdir), state, grant["context"], conn=conn, db=db
        ):
            raise RunAuthorizationLimit("Fixed-test grant belongs to stale task input.")
        grant["used"] += 1
    db._put(
        conn,
        "runs",
        state["id"],
        {
            **state,
            "revision": state["revision"] + 1,
            "test_grants": {**state["test_grants"], **({key: grant} if grant else {})},
            "test_attempts": {**attempts, name: attempts.get(name, 0) + 1},
        },
    )
