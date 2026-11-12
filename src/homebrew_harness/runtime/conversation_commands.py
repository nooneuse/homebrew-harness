"""Planning and project initialization over the existing Session loop."""

import copy
import difflib
import hashlib
import json
import os
from contextvars import ContextVar
from pathlib import Path

from homebrew_harness.context.project_guidance import ProjectGuidance
from homebrew_harness.context.tracked_history import set_context
from homebrew_harness.runtime.turns import run_subagent
from homebrew_harness.security.data_boundary import redact
from homebrew_harness.security.safe_files import SafeFile, FilePublicationError

READ_TOOLS = frozenset({"read_file", "glob", "search_text", "git_status", "git_diff", "load_skill", "compact"})
PLAN_TOOLS = READ_TOOLS | {"todo_write", "task"}
_EVIDENCE = ContextVar("planning_evidence", default=None)

def active_evidence():
    return _EVIDENCE.get()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def initial_state():
    return {"mode": "normal", "revision": 0, "draft": None, "init": None}


def validate_state(value):
    if not isinstance(value, dict) or value.get("mode") not in {"normal", "plan"} or type(value.get("revision")) is not int or value["revision"] < 0:
        raise ValueError("Invalid saved conversation control state.")
    return copy.deepcopy(value)


def quiescent(session, *, fork=False):
    if session._closed:
        raise ValueError("Context cleanup must finish before this operation.")
    if session.run_control:
        raise ValueError("This managed Run keeps its fixed execution contract.")
    pending = set(session.pending_work(include_dispatch=False)) - {"goal", "user_input"}
    if session.gateway.inflight or pending or session.inputs.active_id:
        raise ValueError("Finish active model, tool and assigned work before changing conversation controls.")
    if fork and (session.goal.pending or session.inputs.pending() or session._execution_paused):
        raise ValueError("Finish or cancel accepted work and Goal before /fork.")


def save_state(session, before):
    try:
        session._save_checkpoint()
    except Exception:
        saved = session.runtime_store.get("checkpoints", session.checkpoint_key, {})
        if saved.get("conversation") != session.conversation:
            session.conversation = before
        raise


def set_mode(session, mode):
    quiescent(session)
    previous = copy.deepcopy(session.conversation)
    session.conversation["mode"] = mode
    save_state(session, previous)
    if mode == "normal":
        set_context(session.messages, "planning_mode", None)
    elif session.goal.enabled:
        session.goal.suspend("suspended", "User entered read-only planning; completion remains pending.")
        session._goal_ready = False
    return {"mode": mode, "draft": session.conversation.get("draft")}


class PlanEvidence:
    def __init__(self, workdir):
        self.workdir = Path(workdir)
        self.files = {}
        self.overflow = False
        self.before_reads = {}
        self.head = self.git_head()
        self.add("AGENTS.md")

    def git_head(self):
        if not any((root / ".git").exists() for root in (self.workdir, *self.workdir.parents)):
            return None
        from homebrew_harness.orchestration.worktrees import WorktreeManager
        return WorktreeManager(self.workdir, None)._git(["rev-parse", "HEAD"]).stdout.strip()

    def signature(self, path):
        with SafeFile(self.workdir, path) as file:
            try:
                with file.open_read() as stream:
                    stat = os.fstat(stream.fileno())
                    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
            except FileNotFoundError:
                return None

    def add(self, path):
        if len(self.files) >= 128:
            self.overflow = True
            return
        self.files[path] = self.signature(path)

    def before(self, call, cwd):
        if call.name != "read_file":
            return
        try:
            path = json.loads(call.arguments)["path"]
            selected = str((Path(cwd) / path).relative_to(self.workdir))
            self.before_reads[call.id] = (selected, self.signature(selected))
        except (ValueError, OSError, KeyError, TypeError):
            return

    def observe(self, call, cwd, output):
        if call.name not in READ_TOOLS:
            return
        try:
            value = json.loads(output)
            if not isinstance(value, dict) or value.get("error"):
                return
            observed = self.before_reads.pop(call.id, None)
            if observed and self.signature(observed[0]) != observed[1]:
                self.overflow = True
            paths = ([value["path"]] if isinstance(value.get("path"), str) else []) + value.get("paths", [])
            paths += [row["path"] for row in value.get("matches", []) if isinstance(row, dict) and isinstance(row.get("path"), str)]
            for path in paths:
                selected = str((Path(cwd) / path).relative_to(self.workdir))
                self.add(selected)
        except (ValueError, OSError, TypeError):
            self.overflow = True

    def state(self):
        return {"head": self.head, "files": dict(self.files), "overflow": self.overflow}

    @classmethod
    def valid(cls, workdir, state):
        current = cls(workdir)
        return not state.get("overflow") and current.head == state["head"] and all(current.signature(path) == version for path, version in state["files"].items())


def plan_turn(session, query):
    session.intent.update(query)
    session.user_request = session.intent.original
    if session.goal.pending:
        session.goal.state["task_intent"] = session.intent.to_dict()
        session.goal._persist()
    evidence = PlanEvidence(session.workdir)
    session._plan_evidence = evidence
    evidence_token = _EVIDENCE.set(evidence)
    set_context(session.messages, "planning_mode", {"role": "system", "name": "planning_mode", "content":
        "Host planning mode is active. Investigate and discuss a plan only. Preserve user requirements. Include steps, validation, assumptions and unresolved questions. Todo is a draft, never completion evidence. Execution requires the user's acceptance of a specific plan revision."}, position=1)
    from homebrew_harness.runtime.turns import run_user_turn
    try:
        with session.inputs.turn():
            result = run_user_turn(session.client, session.model, session.messages, query, session.workdir,
                components=session.loop_components(), memory_runtime=session.memory, max_turns=session.max_turns,
                goal_controller=None, collect_background=False, stop_event=session.stop_event)
    finally:
        session._plan_evidence = None
        _EVIDENCE.reset(evidence_token)
    previous = copy.deepcopy(session.conversation)
    session.conversation["revision"] += 1
    session.conversation["draft"] = {"revision": session.conversation["revision"], "content": result,
        "intent": digest(session.intent.to_dict()), "evidence": evidence.state(), "accepted": False}
    session.last_goal_result = result
    save_state(session, previous)
    return result + "\n\nPlan revision " + str(session.conversation["revision"]) + "; /plan apply " + str(session.conversation["revision"]) + " executes after review."


def accept_plan(session, revision):
    quiescent(session)
    draft = session.conversation.get("draft")
    if not draft or draft["revision"] != revision or draft.get("accepted"):
        raise ValueError("Choose an unaccepted current plan revision from /plan status.")
    if draft["intent"] != digest(session.intent.to_dict()) or not PlanEvidence.valid(session.workdir, draft["evidence"]):
        raise ValueError("Requirements or observed files changed; update the plan before applying it.")
    previous = copy.deepcopy(session.conversation)
    intent = session.intent.to_dict()
    history = copy.deepcopy(session.messages)
    text = "User accepted plan revision " + str(revision) + " as execution guidance. Original requirements and host permissions remain in force:\n" + draft["content"]
    session.intent.update(text)
    set_context(session.messages, "accepted_plan", {"role": "user", "name": "accepted_plan", "content": text})
    set_context(session.messages, "planning_mode", None)
    draft["accepted"] = True
    session.conversation["mode"] = "normal"
    try:
        session._save_checkpoint()
    except Exception:
        saved = session.runtime_store.get("checkpoints", session.checkpoint_key, {})
        if saved.get("conversation") != session.conversation or saved.get("intent") != session.intent.to_dict():
            session.conversation = previous
            session.intent.restore(intent)
            session.messages[:] = history
        raise
    if session.goal.pending:
        session.goal.state["task_intent"] = session.intent.to_dict()
        session.goal._persist()
    return "Execute the accepted plan while preserving the original user requirements and revisions."


def init_draft(session, *, preview=False):
    quiescent(session)
    before = session.project_guidance.snapshot()
    result = run_subagent("Read the project README, contribution guide, source layout, build/test configuration and CI using read-only tools. Produce ONLY concise AGENTS.md markdown in the project's documentation language (English by default). Describe purpose, modules, build/test commands, conventions and contribution workflow. Preserve useful existing guidance; identify facts still unknown. Do not claim tests were executed. Never include credentials, private endpoints, machine paths, personal identities, or development review records. Existing AGENTS.md:\n" + before["content"],
        session.workdir, client=session.client, model=session.model, hooks=session.hooks, skill_loader=session.skills,
        parent_intent=session.intent, context_chars=session.compactor.context_chars, max_turns=10,
        tool_allowlist=READ_TOOLS, stop_event=session.stop_event, worker_output_tokens=session.lead_binding.max_output_tokens)
    result = json.loads(result)
    if result.get("status") != "completed" or not result.get("result", "").strip():
        raise ValueError("Initialization did not produce a completed draft; inspect its retained subagent record.")
    content = result["result"].strip() + "\n"
    if content.startswith("```markdown\n") and content.endswith("```\n"):
        content = content[len("```markdown\n"):-4]
    session.project_guidance.validate_text(content)
    previous = copy.deepcopy(session.conversation)
    session.conversation["revision"] += 1
    draft = {"revision": session.conversation["revision"], "content": content, "base": before["sha256"],
        "diff": "".join(difflib.unified_diff(before["content"].splitlines(keepends=True), content.splitlines(keepends=True), fromfile="AGENTS.md", tofile="AGENTS.md")), "published": False}
    session.conversation["init"] = draft
    save_state(session, previous)
    if before["sha256"] is None and not preview and session.conversation["mode"] != "plan":
        return publish_init(session, draft["revision"])
    return {"draft": draft, "next": "/init apply " + str(draft["revision"])}


def publish_init(session, revision):
    quiescent(session)
    if session.conversation["mode"] == "plan":
        raise ValueError("Leave planning mode before publishing AGENTS.md.")
    draft = session.conversation.get("init")
    if not draft or draft["revision"] != revision:
        raise ValueError("Choose a current initialization revision from /init status.")
    current = session.project_guidance.snapshot()
    if current["sha256"] == hashlib.sha256(draft["content"].encode()).hexdigest():
        draft["published"] = True
        session.project_guidance.refresh(session.messages)
        session._save_checkpoint()
        return {"path": "AGENTS.md", "sha256": current["sha256"], "revision": revision, "published": True, "already_present": True}
    try:
        value = session.project_guidance.publish(draft["content"], draft["base"])
    except FilePublicationError:
        current = session.project_guidance.snapshot()
        if current["sha256"] != hashlib.sha256(draft["content"].encode()).hexdigest():
            raise
        draft["published"] = True
        session._save_checkpoint()
        raise ValueError("AGENTS.md was published, but durability confirmation failed; inspect /init status.") from None
    draft["published"] = True
    session.project_guidance.refresh(session.messages)
    session._save_checkpoint()
    return {**value, "revision": revision, "published": True}


def fork_check(session):
    quiescent(session, fork=True)
    session._save_checkpoint()
    return {"parent": session.session_id, "fingerprint": fork_fingerprint(session._snapshot())}


def fork_fingerprint(snapshot):
    return digest({key: snapshot.get(key) for key in ("messages", "intent", "model_binding", "model_registry", "conversation", "goal_id", "lead_task")})


def seed_fork(session, source):
    sid = source["parent"]
    meta = session.runtime_store.get("session_meta", sid, {})
    saved = session.runtime_store.get("checkpoints", "lead:" + sid, {})
    if not meta.get("close_complete") or saved.get("goal_id") or saved.get("managed_run") or saved.get("lead_task") or source["fingerprint"] != fork_fingerprint(saved):
        raise ValueError("Fork source changed or still owns unfinished execution.")
    if saved.get("model_registry") != session.models.manifest() or saved.get("model_binding") != session.lead_binding.identity() or saved.get("bash_policy") != session.bash_policy.identity() or saved.get("capabilities") != session.capabilities.manifest():
        raise ValueError("Fork requires its original model, capabilities and execution policy.")
    from homebrew_harness.runtime.session_commands import visible_history
    history = visible_history(saved["messages"])
    session.messages[:] = history
    session.intent.restore(saved["intent"])
    session.user_request = session.intent.original
    session.compactor.store.inherit_checkpoint(saved["archives"])
    session.conversation = initial_state()
    session.conversation["mode"] = saved.get("conversation", {}).get("mode", "normal")
    session.fork_source = {"parent": sid, "fingerprint": source["fingerprint"]}
    session.messages.append({"role": "user", "name": "fork_context", "content":
        "Host forked readable conversation from Session " + sid + ". Earlier results and plans are reference only; no active execution, authorizations or current verification evidence was copied. Parent conversation remains separate."})
    session.compactor.validate_fixed_budget(session.messages, session.intent.original, tools=session.tool_pool().tools)
