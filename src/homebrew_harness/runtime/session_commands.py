"""Host-owned interactive controls reuse Session and tool implementations."""

import copy
import json

from homebrew_harness.execution.model_gateway import ModelGateway
from homebrew_harness.runtime.model_selection import choose_binding, model_status, validate_choice
from homebrew_harness.runtime.turns import run_subagent
from homebrew_harness.security.data_boundary import redact
from homebrew_harness.tools.queries import diff_snapshot

HELP = """/model [ALIAS or same-connection MODEL]  Show or choose the Lead model
/reasoning [default|none|minimal|low|medium|high|xhigh|on|off]  Show or choose reasoning
/compact  Compact complete history, retaining requirements and archives
/new  Start a blank chat after current work ends, keeping model choice
/diff [--base REF] [PATH ...]  Inspect working changes and untracked text
/review [--base REF] [PATH ...]  Review a fixed snapshot with read-only tools
/skills  List skills; /skills show NAME; /skills use NAME TASK
/worktree [list|show NAME|create NAME --task TASK_ID|remove NAME]  Task worktrees
/memory [status|list|show NAME|correct NAME TEXT|delete NAME]  Cross-session knowledge
/resume [list|ID]  Continue current work or restore retained context
/pwd  Show the actual execution directory
/ps [show ID]  Inspect owned work and resources
/stop [ID]  Request cancellation, retaining unfinished work
/hooks [show EVENT]  Inspect registered lifecycle callbacks
/agents [show NAME_OR_ID]  Inspect Lead, teammates and subagents
/quit; /exit  Save and close the interface
/help  Show commands
/goal [CONDITION|clear|resume|spec JSON]  Manage the current Goal
/inputs  Inspect accepted input; /recovery  Inspect interrupted operations
/usage  Inspect model usage; /storage  Inspect storage
/tests [approve|revoke NAME]  Manage fixed test grants
/mcp [close CONNECTION_ID]  Inspect MCP connections
/queue TEXT; /steer TEXT; /interrupt TEXT; /steer-to MEMBER TEXT
/approve ID [REVISION] yes/no  Decide an exact request
Default entry also supports /status, /pause, /cancel, /upgrade, /detach
q, quit, exit  Close the interface"""


def visible_history(messages):
    history = copy.deepcopy(messages)
    for message in history:
        for name in ("_protocol_state", "_replay", "reasoning_content"):
            message.pop(name, None)
    return history


def busy(session):
    return bool(session.gateway.inflight or session.inputs.active_id or session.tests.active.is_set()
        or set(session.pending_work()) - {"goal", "user_inputs", "user_input"})


def select(session, choice, *, restore=False):
    choice = validate_choice(choice)
    if not restore and session.run_control is not None:
        raise ValueError("This Run has a fixed model binding; use its declared model handoff.")
    if not restore and busy(session):
        raise ValueError("Finish active model/tool work before changing the model.")
    binding = choose_binding(session.models, session._model_source_client, session._model_source_name, choice,
        context_chars=session._model_defaults[0], output_tokens=session._model_defaults[1])
    client = session.gateway.bind(binding.client)
    if binding.identity() == session.lead_binding.identity():
        return model_status(session.models, session.lead_binding)
    proposed = visible_history(session.messages)
    compactor = copy.copy(session.compactor)
    compactor.client, compactor.model, compactor.context_chars = client, binding.model, binding.context_chars
    tools = session.tool_pool().tools
    compactor.validate_fixed_budget(proposed, session.user_request, session.todo.render(), tools)
    if not restore and compactor.estimate_chars(proposed, tools) >= binding.context_chars:
        raise ValueError("The target context is too small; use /compact or /new first.")
    if restore:
        transcript = None
    else:
        transcript = session.compactor.write_transcript(session.messages)
        proposed.append({"role": "user", "name": "model_transition", "content":
            "Host model selection changed. Existing readable history remains reference; original requirements and authorizations remain in force. Original protocol state is archived at " + transcript})
    if not restore and compactor.estimate_chars(proposed, tools) >= binding.context_chars:
        raise ValueError("The target context is too small; use /compact or /new first.")
    previous_revision = session.model_revision
    previous = (session.client, session.model, session.lead_binding, copy.deepcopy(session.messages), session.model_selection)
    session.client, session.model, session.lead_binding = client, binding.model, binding
    session.compactor.client, session.compactor.model = client, binding.model
    session.compactor.context_chars = binding.context_chars
    session.compactor.worker_output_tokens = binding.max_output_tokens
    if session._memory_tracks_model and session.memory:
        session.memory.client, session.memory.model = client, binding.model
    if session._goal_tracks_model:
        session.goal.evaluator.client, session.goal.evaluator.model = client, binding.model
    session.model_selection = choice
    if not restore:
        session.model_revision += 1
        session.messages[:] = proposed
        try:
            session._save_checkpoint()
        except Exception:
            saved = session.runtime_store.get("checkpoints", session.checkpoint_key, {})
            if saved.get("model_binding") != binding.identity():
                old_client, old_model, old_binding, old_messages, old_choice = previous
                session.client, session.model, session.lead_binding = old_client, old_model, old_binding
                session.messages[:] = old_messages
                session.model_selection = old_choice
                session.model_revision = previous_revision
                session.compactor.client, session.compactor.model = old_client, old_model
                session.compactor.context_chars = old_binding.context_chars
                session.compactor.worker_output_tokens = old_binding.max_output_tokens
                if session._memory_tracks_model and session.memory:
                    session.memory.client, session.memory.model = old_client, old_model
                if session._goal_tracks_model:
                    session.goal.evaluator.client, session.goal.evaluator.model = old_client, old_model
                raise
            session._committed_model_selection = choice
            session._committed_model_revision = session.model_revision
            raise ValueError("Model selection was saved, but confirmation failed; inspect /model.") from None
    session._committed_model_selection = choice
    if not restore:
        session._committed_model_revision = session.model_revision
    session.gateway.reset_failures()
    session._quota_blocked = session._automatic_failed = False
    return model_status(session.models, binding)


def save_history(session, before):
    try:
        session._save_checkpoint()
    except Exception:
        saved = session.runtime_store.get("checkpoints", session.checkpoint_key, {})
        if saved.get("messages") == redact(session.messages):
            raise ValueError("Context was saved, but confirmation failed; inspect the retained checkpoint.") from None
        session.messages[:] = before
        raise


def run(session, command):
    if command.name == "help":
        return HELP
    if command.name in {"model", "reasoning"}:
        if command.action == "status":
            return json.dumps(model_status(session.models, session.lead_binding), ensure_ascii=False, indent=2)
        choice = dict(session.model_selection)
        if command.name == "model":
            choice = {"profile": command.argument if session.models.specs else None,
                "model": None if session.models.specs else command.argument, "reasoning": None}
        else:
            choice["reasoning"] = None if command.argument == "default" else command.argument
        return json.dumps(select(session, choice), ensure_ascii=False, indent=2)
    if command.name == "new":
        if session.pending_work() or session.inputs.active_id or session.inputs.pending():
            raise ValueError("Finish or cancel accepted work before /new.")
        return json.dumps({"action": "new_chat", "model_selection": session.model_selection})
    if command.name == "compact":
        if busy(session):
            raise ValueError("Compaction requires a complete model/tool boundary.")
        before = copy.deepcopy(session.messages)
        compacted = session.compactor.compact_history(before, session.user_request, session.todo.render(), keep_recent=6)
        session.messages[:] = compacted
        save_history(session, before)
        return json.dumps({"compacted": True, "messages": len(compacted)}, ensure_ascii=False)
    if command.name == "skills":
        if command.action == "list":
            return session.skills.catalog()
        skill = session.skills.skills.get(command.argument)
        if skill is None:
            raise ValueError("Unknown skill; use /skills for available names.")
        if command.action == "show":
            return json.dumps(redact({"name": skill.name, "content": skill.content}), ensure_ascii=False, indent=2)
        from homebrew_harness.context.tracked_history import set_context
        set_context(session.messages, "selected_skill", {"role": "user", "name": "selected_skill",
            "content": "User-selected skill guidance, not additional permission:\n" + skill.content})
        return None
    if command.name in {"diff", "review"}:
        base, paths = command.values
        snapshot = diff_snapshot(workdir=session.workspace.lead_cwd(), base=base, paths=list(paths) or None)
        if command.name == "diff":
            return json.dumps(snapshot, ensure_ascii=False, indent=2)
        if busy(session):
            raise ValueError("Finish active model/tool work before /review.")
        if snapshot["truncated"]:
            raise ValueError("Review snapshot is truncated; select a smaller set of paths.")
        if not snapshot["paths"]:
            return "No changes to review."
        prompt = "Review this fixed Git snapshot for concrete correctness/security issues. Read relevant current files as needed. Report findings with paths and evidence; do not edit, execute tests, or claim unseen code was verified. Binary files are not reviewed.\n" + json.dumps(snapshot, ensure_ascii=False)
        result = run_subagent(prompt, session.workspace.lead_cwd(), client=session.client, model=session.model,
            hooks=session.hooks, skill_loader=session.skills, parent_intent=session.intent,
            context_chars=session.compactor.context_chars, max_turns=10,
            tool_allowlist=frozenset({"read_file", "glob", "search_text", "git_status", "git_diff", "load_skill", "compact"}),
            stop_event=session.stop_event, worker_output_tokens=session.lead_binding.max_output_tokens)
        current = diff_snapshot(workdir=session.workspace.lead_cwd(), base=snapshot["base"], paths=list(paths) or None)
        output = {"review": result, "snapshot": snapshot["fingerprint"],
            "stale": current["fingerprint"] != snapshot["fingerprint"], "binary_not_reviewed": snapshot["binary"]}
        before = copy.deepcopy(session.messages)
        session.messages.append({"role": "user", "name": "review_result", "content":
            "Read-only review of the labelled snapshot; reference findings, not completion or authorization:\n" + json.dumps(output, ensure_ascii=False)})
        save_history(session, before)
        return json.dumps(output, ensure_ascii=False)
    raise ValueError("Unsupported interactive command.")
