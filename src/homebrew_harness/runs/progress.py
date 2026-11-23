"""Durable, obstruction-local observations for the Run's existing model feedback."""

import hashlib
import json
import time
from pathlib import Path

from homebrew_harness.runtime.limits import current_limits
from homebrew_harness.security.data_boundary import redact
from homebrew_harness.security.safe_files import SafeFile
from homebrew_harness.storage.storage_gate import storage_gate
from .requests import digest
from .store import owner_guard

_FILE_TOOLS = {"write_file", "edit_file", "delete_file"}
_EMPTY = {"signature": None, "count": 0, "replans": 0, "blocked": False, "feedback": None}
_REPLAN = (
    "Replan before another unchanged attempt. State a different approach and what it would establish, "
    "or wait for a specific external change (wait_for_source when available), or use report_run_blocker "
    "with the obstacle and required action when no available approach can advance the work. "
    "A controlled retry is allowed if you state what it is testing and what result would change your next step."
)


def _failure(result):
    info = result.get("error_info")
    code = result.get("reason_code") or (info.get("code") if isinstance(info, dict) else None)
    category = {key: result[key] for key in ("status", "exit_code") if result.get(key) is not None}
    if code:
        # Structured codes survive request IDs, timings and other volatile error details.
        category["code"] = code
    elif result.get("error"):
        category["error"] = result["error"]
    return category


def _old_failure_signature(call, args, result):
    return digest({"tool": call.name, "args": args, "failure": {
        key: result.get(key) for key in ("error", "status", "exit_code", "reason_code")
    }})


def _test_observation(result):
    """Use execution's stable receipt basis, not request args, timestamps or run IDs."""
    basis = result.get("verification_basis")
    receipt = result.get("run_id")
    status = result.get("status")
    if (
        result.get("source_stable") is not True
        or status not in ("passed", "failed", "timeout", "error")
        or not isinstance(receipt, str) or not receipt
        or not isinstance(result.get("recipe"), str)
        or not isinstance(result.get("cwd"), str)
        or not isinstance(basis, dict)
        or basis.get("test_scope") not in ("full", "selected")
        or "selection" not in basis
        or not all(isinstance(basis.get(key), str) and basis[key]
                   for key in ("inputs", "contract", "backend", "role"))
        or status == "passed" and (result.get("exit_code") != 0 or result.get("error"))
    ):
        return None
    target = {
        "recipe": result["recipe"], "cwd": result["cwd"],
        **{key: basis[key] for key in ("role", "test_scope", "selection")},
    }
    return target, digest(basis), receipt


class ProgressTracker:
    def __init__(self, store):
        self.store = store

    def get(self, run_id):
        return self.store.runtime.get("run_progress", run_id)

    def _new(self, control, intent, revision):
        return {
            "run_id": control.id,
            "checkpoint": "lead:" + control.state["session_id"],
            "revision": revision,
            "intent": intent,
            **_EMPTY,
            "files": {},
            "observations": {},
        }

    def ensure_intent(self, control):
        state = self.get(control.id)
        if state is not None and state["intent"] == digest(control.session.intent.to_dict()):
            return state
        return self._change(control)

    def _change(self, control, apply=None):
        # A slow tool may finish while another observation is being committed. Read,
        # count and write under the same owner-guarded transaction.
        db = self.store.runtime.backend()
        with storage_gate(self.store.root), db.write() as conn:
            owner_guard(db, conn, control.header)
            state = db._get(conn, "run_progress", control.id)
            intent = digest(control.session.intent.to_dict())
            fresh = state is None or state["intent"] != intent
            if fresh:
                state = self._new(control, intent, state["revision"] + 1 if state else 0)
            if apply is not None and not state["blocked"]:
                # Preserve pre-ledger counters so a matching old obstruction can
                # continue its explicit budget after an upgrade or restart.
                if "observations" not in state:
                    state["observations"] = {}
                    if state["signature"]:
                        feedback = state["feedback"] or {}
                        state["observations"]["legacy:" + state["signature"]] = {
                            "signature": state["signature"], "kind": "legacy",
                            "count": state["count"], "replans": state["replans"],
                            "revision": state["revision"], "replan_at": feedback.get("revision"),
                            "fact": feedback.get("fact"), "evidence": {}, "blocked": False,
                        }
                if apply(state):
                    state["revision"] += 1
                    self._project(state)
                    fresh = True
            if fresh:
                db._put(conn, "run_progress", control.id, state)
        return state

    @staticmethod
    def _project(state):
        entries = state["observations"]
        # Bound warnings and baselines separately: unrelated fresh evidence must
        # neither evict an active warning nor be crowded out by old warnings.
        for warned in (False, True):
            keys = sorted(
                (key for key, row in entries.items() if (row["replan_at"] is not None) == warned),
                key=lambda key: entries[key]["revision"],
            )
            for key in keys[:-current_limits().progress_paths]:
                entries.pop(key)
        warnings = sorted(
            (row for row in entries.values() if row["replan_at"] is not None),
            key=lambda row: (row["blocked"], row["replan_at"]), reverse=True,
        )
        latest = warnings[0] if warnings else max(
            (row for row in entries.values() if row["count"]), key=lambda row: row["revision"], default=None,
        )
        state.update(_EMPTY)
        if latest is not None:
            state.update({key: latest[key] for key in ("signature", "count", "replans", "blocked")})
        if warnings:
            def summary(row):
                return {key: row[key] for key in ("signature", "kind", "count", "fact", "evidence")}

            state["feedback"] = {
                **summary(latest), "revision": latest["replan_at"], "blocked": latest["blocked"],
                "enforcement": "hard" if latest["blocked"] else "advisory",
                "decision": "report_remaining_work" if latest["blocked"] else "replan_required",
                "action": (
                    "The explicitly configured repetition budget is exhausted; report remaining work."
                    if latest["blocked"] else
                    "Reuse the existing passing receipt unless a controlled rerun would add evidence. " + _REPLAN
                    if latest["kind"] == "redundant_verification" else _REPLAN
                ),
            }
            if len(warnings) > 1:
                state["feedback"]["other_obstructions"] = [summary(row) for row in warnings[1:4]]

    @staticmethod
    def _record(state, spec, key, signature, kind, fact, evidence, *, legacy=None):
        entries = state["observations"]
        previous = entries.get(key)
        if legacy:
            old = entries.pop("legacy:" + legacy, None)
            previous = previous or old
        if previous and previous["kind"] != "legacy" and previous["signature"] != signature:
            previous = None
        receipt = evidence.get("receipt_ref")
        if receipt:
            prior = previous["evidence"] if previous else {}
            if receipt == prior.get("receipt_ref"):
                return False  # Observing the same execution is not a new attempt.
            evidence = {**evidence, "first_receipt_ref": prior.get("first_receipt_ref", receipt)}
        redundant = kind == "redundant_verification"
        revision = state["revision"] + 1
        row = {
            "signature": signature, "kind": kind, "fact": redact(fact)[:600], "evidence": redact(evidence),
            "count": previous["count"] + 1 if previous else (0 if redundant else 1),
            "replans": previous["replans"] if previous else 0,
            "replan_at": previous["replan_at"] if previous else None,
            "revision": revision, "blocked": False,
        }
        # A passing recheck may be a deliberate flake/stress experiment; the
        # established hard budget still applies to failures and ineffective edits.
        limit = spec.repeat_warning if redundant else (spec.repeat_limit or spec.repeat_warning)
        if limit is not None and row["count"] >= limit * (row["replans"] + 1):
            row["blocked"] = (not redundant and spec.repeat_limit is not None and spec.replan_limit is not None
                              and row["replans"] >= spec.replan_limit)
            row["replan_at"] = revision
            if not row["blocked"]:
                row["replans"] += 1
        entries[key] = row
        return True

    def record(self, control, signature, description):
        self._change(control, lambda state: self._record(
            state, control.spec, signature, signature, "run_obstruction", description, {}, legacy=signature,
        ))

    def observe(self, control, call, workdir, output):
        try:
            result, args = json.loads(output), json.loads(call.arguments)
        except (ValueError, TypeError):
            return
        if (not isinstance(result, dict) or not isinstance(args, dict)
                or result.get("outcome") in ("unknown", "partial") or result.get("execution_pending")
                or result.get("reason_code") == "approval_required"):
            return
        failure = (result.get("error") or (call.name == "run_tests" and result.get("status") in ("failed", "timeout"))
                   or (call.name == "bash" and result.get("exit_code", 0) != 0))
        # Execution wrapper settings change observation, not the underlying operation.
        try:
            workdir = Path(workdir).resolve()
            call_key = digest({"cwd": str(workdir), "tool": call.name, "args": {
                key: value for key, value in args.items() if key != "execution"
            }})
        except (OSError, ValueError):
            return
        if call.name == "run_tests":
            if self._observe_test(control, call_key, workdir, result, call, args):
                return
            if result.get("status") in ("passed", "changed", "cancelled"):
                return  # An incomplete/unstable observation cannot clear an obstruction.
        if failure:
            category = _failure(result)
            signature = digest({"call": call_key, "failure": category})
            self._change(control, lambda state: self._record(
                state, control.spec, call_key, signature, "tool_failure",
                "Repeated " + call.name + " failure: " + json.dumps(category, ensure_ascii=False),
                {"tool": call.name, "call_ref": str(call.id)[:128]},
                legacy=_old_failure_signature(call, args, result),
            ))
        elif call.name in _FILE_TOOLS:
            self._observe_file(control, call_key, workdir, result)
        else:
            state = self.get(control.id)
            if state and call_key in state.get("observations", {}):
                self._change(control, lambda state: state["observations"].pop(call_key, None) is not None)

    def _observe_test(self, control, call_key, workdir, result, call, args):
        observation = _test_observation(result)
        if observation is None:
            return False
        # A live execution can settle after the accepted Run intent changes.
        scope = result.get("scope")
        if isinstance(scope, dict) and scope != control.evidence_scope():
            return True
        target, basis, receipt = observation
        key = digest({"cwd": str(workdir), "test": target})
        category = _failure(result)
        signature = digest({"test": key, "basis": basis, "outcome": category})
        passed = result["status"] == "passed"
        kind = "redundant_verification" if passed else "test_failure"
        test = target["recipe"] + " (" + target["test_scope"] + ")"
        fact = ("Passing " + test + " again on the same verification basis and selection."
                if passed else "Repeated " + test + " test outcome on the same verification basis: "
                + json.dumps(category, ensure_ascii=False))

        def apply(state):
            changed = self._record(
                state, control.spec, key, signature, kind, fact,
                {"recipe": target["recipe"], "cwd": target["cwd"], "test_scope": target["test_scope"],
                 "basis": basis, "receipt_ref": receipt[:128], "status": result["status"]},
                legacy=None if passed else _old_failure_signature(call, args, result),
            )
            # A settled receipt supersedes a pre-execution error for this exact call.
            return state["observations"].pop(call_key, None) is not None or changed

        self._change(control, apply)
        return True

    def _observe_file(self, control, call_key, workdir, result):
        if not isinstance(result.get("path"), str):
            return
        try:
            with SafeFile(workdir, result["path"]) as target:
                raw, _, _ = target.snapshot()
                path = (target.root.joinpath(*target.parts)).relative_to(self.store.root).as_posix()
        except (OSError, ValueError):
            return  # The mutation finished; the post-call snapshot was not reliable.
        file_hash = digest({"content": hashlib.sha256(raw).hexdigest() if raw is not None else None})
        key = digest({"mutation": path})

        def apply(state):
            entries = state["observations"]
            entries.pop(call_key, None)
            seen = state["files"].pop(path, [])
            if file_hash in seen or result.get("already_absent") is True:
                kind = "unchanged" if not seen or seen[-1] == file_hash else "cycle"
                self._record(
                    state, control.spec, key, key, "ineffective_mutation",
                    "File " + path + " repeatedly has an " + kind + " state.",
                    {"path": path, "content_state": file_hash, "pattern": kind},
                    legacy=digest({"path": path, "kind": kind}),
                )
            else:
                entries.pop(key, None)
                for kind in ("unchanged", "cycle"):
                    entries.pop("legacy:" + digest({"path": path, "kind": kind}), None)
            state["files"][path] = [*seen, file_hash][-current_limits().progress_versions:]
            if len(state["files"]) > current_limits().progress_paths:
                state["files"].pop(next(iter(state["files"])))
            return True

        self._change(control, apply)

    def reset(self, run_id, revision, reason):
        if (
            type(revision) is not int
            or not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > current_limits().input_text_chars
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
                **_EMPTY,
                "revision": revision + 1,
                "files": {},
                "observations": {},
                "reset": {"reason": redact(reason), "at": time.time()},
            }
            db._put(conn, "run_progress", run_id, value)
        return value
