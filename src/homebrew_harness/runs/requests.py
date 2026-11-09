"""Durable human requests; approval is consumed with operation acceptance."""

import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from contextlib import contextmanager
from contextvars import ContextVar

from homebrew_harness.execution.source_snapshot import snapshot
from homebrew_harness.security.data_boundary import redact
from homebrew_harness.security.safe_files import SafeFile
from homebrew_harness.storage.storage_gate import storage_gate
from .effects import enqueue, notice
from .spec import TERMINAL
from .store import owner_guard

_APPROVAL = ContextVar("managed_exact_approval", default=None)
ID = re.compile(r"req_[0-9a-f]{32}\Z")


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def current_approval():
    return _APPROVAL.get()


def workspace_digest(root, watch):
    if watch["kind"] == "patterns":
        return digest(snapshot(root, tuple(watch["values"]), **watch.get("limits", {})))
    values = {}
    for name in watch["values"]:
        target = root / Path(name)
        if target.is_symlink() or not target.resolve().is_relative_to(root):
            raise ValueError(
                "Approval scope cannot follow a symlink or leave the workspace."
            )
        if target.is_dir():
            values[name] = snapshot(target)
        elif target.is_file():
            with SafeFile(root, name) as file:
                raw, version, _ = file.snapshot()
            values[name] = {
                "sha256": hashlib.sha256(raw).hexdigest(),
                "version": version,
            }
        else:
            values[name] = {"missing": True}
    return digest(values)


@contextmanager
def approval_scope(request):
    token = _APPROVAL.set(request)
    try:
        yield
    finally:
        _APPROVAL.reset(token)


def context(control, tool=None, arguments=None):
    s = control.session
    state = control.store.get(control.id)
    value = {
        "spec": digest(state["spec"]),
        "bindings": digest(state["bindings"]) if tool else None,
        "control": state.get("control_identity"),
        "intent": digest(s.intent.to_dict()),
        "epoch": control.store.runtime.input_guard([s.inputs.recipient])[
            s.inputs.recipient
        ],
        "workspace_identity": state["workspace_identity"],
    }
    if tool:
        cwd = s.workspace.lead_cwd()
        assignment = s.workspace.lead_assignment
        value["cwd"] = str(cwd.relative_to(control.store.root))
        value["task_id"] = assignment.id if assignment else None
        watch = {"kind": "paths", "values": list(control.spec.write_paths)}
        if tool == "run_tests":
            watch = {
                "kind": "patterns",
                "values": list(s.tests._recipe(arguments["name"]).input_globs),
                "limits": s.tests._recipe(arguments["name"]).snapshot_limits,
            }
            value["test"] = s.tests.approval_identity(arguments["name"], cwd)
            recipe = s.tests._recipe(arguments["name"])
            value["test_review"] = redact(
                {
                    "argv": list(recipe.argv),
                    "timeout": recipe.timeout,
                    "output_bytes": recipe.output_bytes,
                    "roles": list(recipe.roles),
                    "input_globs": list(recipe.input_globs),
                    "locked_globs": list(recipe.locked_globs),
                    "backend": s.tests.backend.name,
                }
            )
        if tool == "publish_artifacts":
            value["artifact"] = s.artifacts.approval(
                arguments["bundle_id"], workdir=cwd, task_id=value["task_id"]
            )
            watch = {
                "kind": "paths",
                "values": [r["target"] for r in value["artifact"]["files"]],
            }
        if tool.startswith("mcp__"):
            value["mcp"] = s.mcp.descriptor(tool, workdir=cwd, task_id=value["task_id"])
            watch = {"kind": "paths", "values": []}
        value["watch"] = watch
        value["workspace"] = workspace_digest(cwd, watch)
    return value


def matches_record(db, conn, request, run):
    ctx = request["context"]
    if request["kind"] == "test_grant":
        from .test_authorization import valid
        from types import SimpleNamespace

        return valid(SimpleNamespace(root=db.workdir), run, ctx, conn=conn, db=db)
    mcp = ctx.get("mcp")
    if mcp is not None and (
        mcp["binding"] != (run.get("mcp_manifest") or {}).get(request["tool"])
        or mcp["declaration"] != (run.get("bindings") or {}).get("mcp")
    ):
        return False
    return (
        request["run_id"] == run["id"]
        and run["status"] not in TERMINAL
        and time.time() <= request["expires_at"]
        and time.time() >= run["last_clock"] - 1
        and ctx["epoch"] == db._get(conn, "input_epochs", "user_" + run["id"], 0)
        and (
            request["kind"] == "attention" or ctx["bindings"] == digest(run["bindings"])
        )
        and ctx["spec"] == digest(run["spec"])
        and ctx["control"] == run.get("control_identity")
    )


def consume(db, conn, operation, run):
    identity = operation.get("approval_id")
    request = db._get(conn, "run_requests", identity)
    if (
        not request
        or request["state"] != "approved"
        or request["kind"] != "tool"
        or request["revision"] != operation.get("approval_revision")
        or digest(request["context"]) != operation.get("approval_context")
        or request["tool"] != operation["name"]
        or digest(request["arguments"]) != operation.get("approval_arguments")
        or not matches_record(db, conn, request, run)
    ):
        from homebrew_harness.storage.control_boundary import ProposalSuperseded

        raise ProposalSuperseded(
            "Exact managed approval is expired, changed or already consumed."
        )
    db._put(
        conn,
        "run_requests",
        identity,
        {
            **request,
            "state": "consumed",
            "revision": request["revision"] + 1,
            "operation_id": operation["operation_id"],
        },
    )


class HumanRequests:
    def __init__(self, store):
        self.store = store

    def records(self, run_id):
        run = self.store.get(run_id)
        return sorted(
            (
                r
                for sid in run["session_refs"]
                for r in self.store.runtime.items(
                    "run_requests", owner="lead:" + sid
                ).values()
                if r["run_id"] == run_id
            ),
            key=lambda r: (r["created_at"], r["id"]),
        )

    def get(self, run_id, identity):
        if not isinstance(identity, str) or not ID.fullmatch(identity):
            raise ValueError("Invalid human request ID.")
        value = self.store.runtime.get("run_requests", identity)
        if not value or value["run_id"] != run_id:
            raise ValueError("Human request does not belong to this Run.")
        return value

    def create(
        self,
        control,
        kind,
        *,
        tool=None,
        arguments=None,
        call_id=None,
        question=None,
        reason="",
        impact="",
        independent_work="",
        grant_context=None,
    ):
        if kind not in {"tool", "attention", "test_grant"}:
            raise ValueError("Unknown human request kind.")
        mcp_tool = (
            kind == "tool"
            and isinstance(tool, str)
            and tool.startswith("mcp__")
            and tool in control.spec.effective_tools()
            and control.session.mcp is not None
        )
        if mcp_tool:
            descriptor = control.session.mcp.descriptor(
                tool,
                workdir=control.session.workspace.lead_cwd(),
                task_id=control.session.workspace.lead_assignment.id
                if control.session.workspace.lead_assignment
                else None,
            )
            if descriptor["binding"]["policy"]["action"] != "confirm":
                raise ValueError("MCP approval requires a verified confirm policy.")
        if (
            kind == "tool"
            and tool not in {"bash", "run_tests", "publish_artifacts"}
            and not mcp_tool
        ):
            raise ValueError(
                "Managed approval covers exact registered Bash, test and publication calls."
            )
        content = {
            "tool": tool,
            "arguments": arguments,
            "question": question,
            "reason": reason,
            "impact": impact,
            "independent_work": independent_work,
        }
        raw = json.dumps(content, ensure_ascii=False)
        if len(raw) > 64000 or redact(raw) != raw:
            raise ValueError(
                "Human requests must not contain private connection values."
            )
        ctx = (
            grant_context if kind == "test_grant" else context(control, tool, arguments)
        )
        if kind == "test_grant" and (tool != "run_tests" or not isinstance(ctx, dict)):
            raise ValueError("Test renewal requires its runtime task context.")
        identity = "req_" + uuid.uuid4().hex
        state = control.store.get(control.id)
        request = {
            "id": identity,
            "run_id": control.id,
            "checkpoint": "lead:" + state["session_id"],
            "kind": kind,
            **content,
            "call_id": call_id,
            "context": ctx,
            "created_at": time.time(),
            "expires_at": min(
                state["deadline_at"],
                time.time()
                + (
                    control.spec.approval_seconds
                    if kind == "tool"
                    else control.spec.total_seconds
                ),
            ),
            "revision": 0,
            "state": "pending",
            "decision": None,
        }
        db = self.store.runtime.backend()
        with storage_gate(self.store.root), db.write() as conn:
            run = owner_guard(db, conn, control.header)
            if not matches_record(db, conn, request, run):
                raise ValueError("Task changed before creating its human request.")
            records = [
                json.loads(row[0])
                for row in conn.execute(
                    "SELECT value FROM records WHERE section='run_requests' AND owner=?",
                    (request["checkpoint"],),
                ).fetchall()
            ]
            prior = [
                r
                for r in records
                if r["kind"] == kind
                and r["tool"] == tool
                and r["arguments"] == arguments
                and r["question"] == question
                and r["context"] == ctx
                and r["state"] in {"pending", "approved", "denied", "answered"}
            ]
            if prior:
                return prior[-1]
            if sum(r["state"] in {"pending", "approved"} for r in records) >= 32:
                raise ValueError("Run human request queue is full.")
            db._put(conn, "run_requests", identity, request)
            notice(
                db,
                conn,
                run,
                "approval_required"
                if kind in {"tool", "test_grant"}
                else "attention_required",
                identity,
            )
        return request

    def decide(
        self,
        run_id,
        identity,
        revision,
        action,
        answer=None,
        *,
        runs=None,
        seconds=None,
    ):
        request = self.get(run_id, identity)
        if request["kind"] == "test_grant":
            if answer is not None:
                raise ValueError("Test grants contain no task answer.")
            from .test_authorization import decide

            return decide(
                self.store,
                run_id,
                identity,
                revision,
                action,
                runs=runs,
                seconds=seconds,
            )
        if runs is not None or seconds is not None:
            raise ValueError(
                "Run/time renewal options apply only to fixed-test grants."
            )
        if type(revision) is not int or action not in {"approve", "deny", "answer"}:
            raise ValueError("Invalid human decision.")
        requested = {"action": action, "answer": answer}
        self.get(run_id, identity)
        db = self.store.runtime.backend()
        with storage_gate(self.store.root), db.write() as conn:
            run = db._get(conn, "runs", run_id)
            request = db._get(conn, "run_requests", identity)
            if (
                request["decision"] == requested
                and request.get("decision_from") == revision
            ):
                return request
            if (
                request["state"] != "pending"
                or request["revision"] != revision
                or not matches_record(db, conn, request, run)
            ):
                raise ValueError(
                    "Human request changed, expired or already has a decision."
                )
            if (action == "answer") != (request["kind"] == "attention"):
                raise ValueError(
                    "Task answers and tool approvals use different requests."
                )
            if action == "answer":
                if (
                    not isinstance(answer, str)
                    or not answer.strip()
                    or len(answer) > 64000
                ):
                    raise ValueError("An attention answer must be nonempty text.")
            elif answer is not None:
                raise ValueError("Tool decisions contain no task answer.")
            recipient = "user_" + run_id
            if conn.execute(
                "SELECT count(*) FROM inbox WHERE recipient=?", (recipient,)
            ).fetchone()[0]:
                raise ValueError(
                    "Apply pending user revisions before answering this old request."
                )
            saved = db._get(conn, "checkpoints", request["checkpoint"], {})
            intent = saved.get("intent")
            if intent is None and run.get("handoff_from"):
                intent = db._get(conn, "run_handoffs", run["handoff_from"], {}).get(
                    "intent"
                )
            if request["context"]["intent"] != digest(intent or {}):
                raise ValueError("The original task checkpoint changed.")
            if request["kind"] == "tool" and request["context"][
                "workspace"
            ] != workspace_digest(
                self.store.root / request["context"].get("cwd", "."),
                request["context"]["watch"],
            ):
                raise ValueError("Workspace changed; review a new exact request.")
            updated = {
                **request,
                "state": {
                    "approve": "approved",
                    "deny": "denied",
                    "answer": "answered",
                }[action],
                "revision": revision + 1,
                "decision": requested,
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
                    **requested,
                },
            )
            if action == "answer":
                mid = "answer_" + identity
                db._send(
                    conn,
                    recipient,
                    {
                        "id": mid,
                        "from": "user",
                        "to": recipient,
                        "type": "user_input",
                        "content": "Answer to "
                        + identity
                        + " ("
                        + request["question"]
                        + "): "
                        + answer,
                        "ts": time.time(),
                        "metadata": {
                            "origin": "host_user",
                            "mode": "steer",
                            "target": "lead",
                            "question_id": identity,
                            "turn_id": db._get(conn, "input_turns", recipient),
                        },
                    },
                )
            enqueue(
                db,
                conn,
                run,
                "wake",
                {"request_id": identity, "decision_revision": updated["revision"]},
                identity,
            )
        return updated

    def invalidate_mcp(self, control):
        db = self.store.runtime.backend()
        with storage_gate(self.store.root), db.write() as conn:
            owner_guard(db, conn, control.header)
            for row in self.records(control.id):
                if row.get("context", {}).get("mcp") and row["state"] in {
                    "pending",
                    "approved",
                }:
                    db._put(
                        conn,
                        "run_requests",
                        row["id"],
                        {**row, "state": "stale", "revision": row["revision"] + 1},
                    )

    def reconcile(self, control):
        rows = self.records(control.id)
        db = self.store.runtime.backend()
        for request in rows:
            if request["state"] not in {"pending", "approved"}:
                continue
            if request["kind"] == "test_grant":
                from .test_authorization import scope
                from homebrew_harness.execution.test_runner import TestContext

                try:
                    ctx = request["context"]
                    fresh = scope(
                        control,
                        ctx["recipe"],
                        self.store.root / ctx["cwd"],
                        TestContext(ctx["role"], ctx["task_id"]),
                    )
                except (ValueError, OSError):
                    fresh = None
            else:
                fresh = context(control, request["tool"], request["arguments"])
            status = (
                "expired"
                if time.time() > request["expires_at"]
                else "stale"
                if fresh != request["context"]
                else None
            )
            if status:
                with storage_gate(self.store.root), db.write() as conn:
                    owner_guard(db, conn, control.header)
                    current = db._get(conn, "run_requests", request["id"])
                    if current["revision"] == request["revision"]:
                        db._put(
                            conn,
                            "run_requests",
                            request["id"],
                            {
                                **current,
                                "state": status,
                                "revision": current["revision"] + 1,
                            },
                        )
        return self.records(control.id)

    def approved(self, control, identity, available):
        request = self.get(control.id, identity)
        if (
            request["state"] != "approved"
            or request["kind"] != "tool"
            or request["tool"] not in available
            or request["tool"] not in control.spec.effective_tools()
            or time.time() > request["expires_at"]
            or request["context"]
            != context(control, request["tool"], request["arguments"])
        ):
            raise ValueError(
                "Approval cannot execute: request, task, policy or workspace changed."
            )
        return request
