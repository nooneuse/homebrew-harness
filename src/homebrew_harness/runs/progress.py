"""Persist only strong repeated-failure and ineffective-mutation signals."""

import json
import time

from homebrew_harness.security.data_boundary import redact
from homebrew_harness.security.safe_files import SafeFile
from homebrew_harness.storage.storage_gate import storage_gate
from .requests import digest
from .store import owner_guard


class ProgressTracker:
    def __init__(self, store):
        self.store = store

    def get(self, run_id):
        return self.store.runtime.get("run_progress", run_id)

    def _new(self, control):
        return {
            "run_id": control.id,
            "checkpoint": "lead:" + control.state["session_id"],
            "revision": 0,
            "intent": digest(control.session.intent.to_dict()),
            "signature": None,
            "count": 0,
            "replans": 0,
            "blocked": False,
            "feedback": None,
            "files": {},
        }

    def ensure_intent(self, control):
        state = self.get(control.id)
        identity = digest(control.session.intent.to_dict())
        if state is None or state["intent"] != identity:
            fresh = self._new(control)
            fresh["revision"] = state["revision"] + 1 if state else 0
            self.save(control, fresh)
            return fresh
        return state

    def save(self, control, state):
        db = self.store.runtime.backend()
        with storage_gate(self.store.root), db.write() as conn:
            owner_guard(db, conn, control.header)
            db._put(conn, "run_progress", control.id, state)

    def record(self, control, signature, description):
        state = self.ensure_intent(control)
        value = {
            **state,
            "signature": signature,
            "count": state["count"] + 1 if state["signature"] == signature else 1,
            "revision": state["revision"] + 1,
        }
        limit = control.spec.repeat_limit
        if value["count"] >= limit * (value["replans"] + 1):
            value["blocked"] = value["replans"] >= control.spec.replan_limit
            if not value["blocked"]:
                value["replans"] += 1
            value["feedback"] = {
                "revision": value["revision"],
                "count": value["count"],
                "fact": redact(description)[:600],
                "blocked": value["blocked"],
                "action": "Report the blocker; host intervention is required."
                if value["blocked"]
                else "Change the local approach using this repeated execution fact; avoid repeating the same action.",
            }
        self.save(control, value)

    def observe(self, control, call, workdir, output):
        try:
            result, args = json.loads(output), json.loads(call.arguments)
        except (ValueError, TypeError):
            return
        if not isinstance(result, dict) or result.get("outcome") in {
            "unknown",
            "partial",
        }:
            return
        if result.get("reason_code") == "approval_required":
            return
        failure = (
            result.get("error")
            or (
                call.name == "run_tests"
                and result.get("status") in {"failed", "timeout"}
            )
            or (call.name == "bash" and result.get("exit_code", 0) != 0)
        )
        if failure:
            category = {
                k: result.get(k)
                for k in ("error", "status", "exit_code", "reason_code")
            }
            self.record(
                control,
                digest({"tool": call.name, "args": args, "failure": category}),
                "Repeated "
                + call.name
                + " failure: "
                + json.dumps(category, ensure_ascii=False),
            )
            return
        if call.name not in {"write_file", "edit_file", "run_tests"}:
            return
        state = self.ensure_intent(control)
        if call.name == "run_tests":
            value = {
                **state,
                "count": 0,
                "signature": None,
                "replans": 0,
                "blocked": False,
                "feedback": None,
                "revision": state["revision"] + 1,
            }
            self.save(control, value)
            return
        with SafeFile(workdir, args["path"]) as target:
            raw, _, _ = target.snapshot()
        file_hash = digest({"content": __import__("hashlib").sha256(raw).hexdigest()})
        path = str((workdir / args["path"]).resolve().relative_to(self.store.root))
        seen = state["files"].get(path, [])
        if file_hash in seen:
            kind = "unchanged" if seen[-1] == file_hash else "cycle"
            self.record(
                control,
                digest({"path": path, "kind": kind}),
                "File " + path + " repeatedly has an " + kind + " state.",
            )
            state = self.get(control.id)
        else:
            state = {
                **state,
                "count": 0,
                "signature": None,
                "replans": 0,
                "blocked": False,
                "feedback": None,
            }
        paths = {**state["files"], path: [*seen, file_hash][-16:]}
        if len(paths) > 64:
            paths.pop(next(iter(paths)))
        self.save(control, {**state, "files": paths, "revision": state["revision"] + 1})

    def reset(self, run_id, revision, reason):
        if (
            type(revision) is not int
            or not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > 1000
        ):
            raise ValueError(
                "Progress reset requires its current revision and a reason."
            )
        db = self.store.runtime.backend()
        with storage_gate(self.store.root), db.write() as conn:
            current = db._get(conn, "run_progress", run_id)
            if not current or current["revision"] != revision:
                raise ValueError("Progress state changed; refresh its revision.")
            value = {
                **current,
                "revision": revision + 1,
                "count": 0,
                "signature": None,
                "replans": 0,
                "blocked": False,
                "feedback": None,
                "files": {},
                "reset": {"reason": redact(reason), "at": time.time()},
            }
            db._put(conn, "run_progress", run_id, value)
        return value
