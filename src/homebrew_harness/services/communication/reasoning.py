"""Lead assessment and bounded drafting inside the existing discussion job."""

import copy
import re

from homebrew_harness.services.protocol import fingerprint
from homebrew_harness.services.project_context import file_version
from .spec import validate_proposal, validate_plan, open_questions



def assessment(store, job, raw, *, require_recipient=True):
    if not isinstance(raw, dict):
        raise ValueError("Assessment must be a complete proposal object.")
    value = copy.deepcopy(raw)
    required = value.pop("reply_required", True)
    if type(required) is not bool or value.get("communication") is not None:
        raise ValueError("Lead assessment cannot propose publication; reply_required must be Boolean.")
    value.pop("communication", None)
    for field in ("facts", "questions", "findings", "draft"):
        value.setdefault(field, copy.deepcopy(job["input"]["projection"].get(field, [])))
    if not require_recipient:
        for q in value.get("questions", []):
            q.setdefault("recipient", "requester")
    if any("recipient" not in q for q in value.get("questions", [])):
        raise ValueError("Lead assessment must explicitly classify each question recipient.")
    validate_proposal(value, store.policy)
    store.communication.validate_evidence(job, value)
    return {"proposal": value, "reply_required": required}


def reply(plan, raw, store, job):
    if not isinstance(raw, dict):
        raise ValueError("Reply must be an object containing communication.")
    value = copy.deepcopy(plan["proposal"])
    for key, item in raw.items():
        if key != "communication" and (key not in value or value[key] != item):
            raise ValueError("Drafting cannot change the Lead assessment; submit only communication.")
    value["communication"] = raw.get("communication")
    if value["communication"] is not None:
        validate_plan(value["communication"])
        questions = open_questions(value, "requester")
        if questions and value["communication"].get("intent") == "clarify" and "question_ids" not in value["communication"]:
            raise ValueError("Draft clarification must name the requester question IDs it asks.")
    validate_proposal(value, store.policy)
    store.communication.validate_evidence(job, value)
    return value


def public(plan):
    value = plan["proposal"]
    return {"next_step": value["next_step"], "facts": value.get("facts", []),
            "draft": value.get("draft"), "public_questions": open_questions(value, "requester")}


def signature(job, project, binding):
    return {"input": fingerprint(job["input"]), "project": project.get("version"),
            "model": binding.identity(), "policy": job["input"]["policy_hash"],
            "prompts": job.get("discussion_prompts", {}).get("signature")}


def valid(record, project, binding, root):
    if not record or not project.get("version"):
        return False
    if record["signature"]["project"] != project["version"] or record["signature"]["model"] != binding.identity():
        return False
    try:
        return all(digest and file_version(root, path) == digest for path, digest in record.get("evidence", {}).items())
    except (OSError, ValueError):
        return False


def acknowledgement(store, job):
    events = job["input"].get("events", [])
    if not events:
        return False
    for identity in events:
        row = store.get("events", identity)
        body = row.get("content", {}).get("body", "")
        canonical = re.sub(r"[\s.!！。]+", "", body.casefold())
        if row.get("object", {}).get("kind") != "comment" or canonical not in {
            "thanks", "thankyou", "ok", "received", "谢谢", "感谢", "收到",
        }:
            return False
    return True


def same_projection(left, right):
    for key in ("facts", "questions", "findings", "draft"):
        before, after = copy.deepcopy(left.get(key)), copy.deepcopy(right.get(key))
        if key == "questions":
            for values in (before, after):
                for question in values or []:
                    question.setdefault("recipient", "requester")
        if before != after:
            return False
    return True


def run(agent, job, project, deadline):
    store = agent.store
    job = {**job, "discussion_prompts": agent.discussion_prompts}
    draft = (agent.binding, agent.gateway)
    lead = agent.model_control.assessment()
    sig = signature(job, project, lead[0])
    current = store.job_get(job["id"])
    saved = current.get("discussion_assessment")
    root = agent.code_root()
    if valid(saved, project, lead[0], root) and saved["signature"] == sig:
        plan = saved["plan"]
    else:
        prior_id = job["input"].get("assessment_job_id")
        prior = None
        if prior_id:
            try:
                prior_job = store.job_get(prior_id)
            except ValueError:
                pass  # Optional cached history may have been retired.
            else:
                if prior_job["kind"] == "discussion" and prior_job["target"] == job["target"]:
                    prior = prior_job.get("discussion_assessment")
        if valid(prior, project, lead[0], root) and acknowledgement(store, job):
            previous = prior["plan"]["proposal"]
            same = (prior["signature"]["policy"] == sig["policy"]
                    and prior["signature"].get("prompts") == sig["prompts"]
                    and prior.get("epoch") == job["input"]["epoch"]
                    and same_projection(previous, job["input"]["projection"]))
            if same:
                result = copy.deepcopy(previous)
                result.pop("communication", None)
                result.update(next_step="operator" if open_questions(result, "maintainer") else "wait",
                              reason="Confirmation received; waiting for substantive input or maintainer decision.",
                              progress=True, requirements_changed=False)
                record = {**prior, "signature": sig, "plan": {"proposal": result, "reply_required": False}}
                store.prepare_job(job, discussion_assessment=record)
                return result
        if lead[0].identity() == agent.binding.identity():
            result = agent._execute(job, project=project, deadline=deadline)
            raw = copy.deepcopy({k: v for k, v in result.items() if k != "communication"})
            for question in raw.get("questions", []):
                question.setdefault("recipient", "requester")
            plan = assessment(store, job, raw, require_recipient=False)
            record = {"signature": sig, "plan": plan, "evidence": agent.code_evidence,
                      "epoch": job["input"]["epoch"]}
            store.prepare_job(job, discussion_assessment=record)
            return result
        plan = agent._execute(job, project=project, stage="assessment", deadline=deadline, selected=lead)
        record = {"signature": sig, "plan": plan, "evidence": agent.code_evidence,
                  "epoch": job["input"]["epoch"]}
        store.prepare_job(job, discussion_assessment=record)
    if not plan["reply_required"]:
        return plan["proposal"]
    return agent._execute(job, project=project, stage="reply", deadline=deadline, plan=plan, selected=draft)
