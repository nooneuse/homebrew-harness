"""Narrow service reasoning on the existing prepared Agent Loop."""

import json
import copy
from pathlib import Path

from homebrew_harness.context.skill_loader import SkillLoader
from homebrew_harness.execution.model_connections import HostModels, read_environment
from homebrew_harness.execution.model_gateway import (
    ModelGateway,
    GatewayLimits,
    Deadline,
)
from homebrew_harness.runtime.assembly import _prepare_loop
from homebrew_harness.runtime.capabilities import Capabilities
from homebrew_harness.runtime.loop_components import LoopComponents
from homebrew_harness.runtime.output import output_scope
from .maintenance_spec import number
from .job_contracts import JobContract
from .protocol import fingerprint

SERVICE_TOOLS = frozenset({"service_read", "service_propose", "service_read_code"})


class ServiceAgent:
    def __init__(self, runtime, store, stop, *, model_profile=None):
        self.runtime, self.store, self.stop = runtime, store, stop
        settings = runtime.core.config.settings
        environment = (
            read_environment(settings.env_file, runtime.store.root)
            if settings.env_file
            else None
        )
        from homebrew_harness.execution.providers.backends import backend_scope

        with backend_scope(getattr(runtime, "saved_model_bindings", None)):
            self.models = HostModels(
                runtime.store.root,
                config=settings.models_config,
                alias=model_profile
                or store.policy["model_profile"]
                or settings.lead_model,
                environment=environment,
            )
        self.binding = self.models.open(
            context_chars=settings.context_chars, output_tokens=settings.output_tokens
        )
        self.gateway = ModelGateway(
            self.binding.client,
            GatewayLimits(request_timeout=settings.request_seconds),
            workdir=runtime.store.root,
        ).with_options(timeout=settings.request_seconds)
        self.models.track_gateway(self.gateway)
        self.base_binding = self.binding
        self.skills = SkillLoader(Path(__file__).parent / "skills")
        self.job = None
        self.proposal = None
        self.stage = None
        self.assessment_plan = None
        self.project_context = None
        self.phase_counts = {}
        self.phase_feedback = None
        self.phase_key = None
        self.phase_signature = None
        self.selected_evidence = None
        self.used_phases = set()
        self.code_evidence = {}
        from .communication.prompts import defaults, identity
        self.builtin_prompts = defaults()
        self.discussion_prompts = None
        self.prompt_identity = identity(runtime.spec.communication.get("prompt_files", {}), self.builtin_prompts)
        self.override_bindings = {}
        from .model_control import ServiceModels

        self.model_control = ServiceModels(self)
        for alias in {
            runtime.spec.operations.get("audit_model"),
            (runtime.spec.delivery or {}).get("review_model"),
            (runtime.spec.communication or {}).get("model_profile"),
            (runtime.spec.communication or {}).get("review_model"),
            (runtime.spec.communication or {}).get("assessment_model"),
        } - {None, self.models.alias}:
            selected = self.models.registry.select(
                alias,
                context_chars=settings.context_chars,
                max_output_tokens=settings.output_tokens,
            )
            self.override_bindings[alias] = selected.identity()
            self.model_control.add(alias, selected)
        self.model_control.load()

    def identity(self):
        from .protocol import fingerprint

        return {
            "model": self.base_binding.identity(),
            "catalog": self.models.registry.manifest(),
            "overrides": self.override_bindings,
            "prompts": self.prompt_identity,
            "skills": fingerprint(
                {k: v.content for k, v in self.skills.skills.items()}
            ),
        }

    def describe_tools(self, definitions):
        tools = copy.deepcopy(definitions)
        for definition in tools:
            function = definition.get("function", {})
            properties = function.get("parameters", {}).get("properties", {})
            # Keep validation in the dispatch contract; model-facing string schemas
            # need not rely on regex generation in compatible provider tool decoders.
            for value in properties.values():
                value.pop("pattern", None)
            if function.get("name") != "service_read" or not self.job or self.job["kind"] != "discussion":
                continue
            properties["kind"]["enum"] = [
                "discussions", "discussion_events", "events", "rounds", "review_snapshots",
                "review_operations", "communication_actions", "decisions", "requirements",
                "work", "policy", "job", "project_context",
            ]
            properties["identity"]["description"] = "Use the exact current job/topic ID or an ID returned by the frozen evidence directory. Never invent placeholders."
            ids = {"", self.job["id"], self.job["target"]}
            ids.update(self.job["input"].get("events", []))
            for field in ("review_id", "work_id"):
                if self.job["input"].get(field):
                    ids.add(self.job["input"][field])
            for field in ("facts", "questions", "findings"):
                for item in self.job["input"]["projection"].get(field, []):
                    ids.update(item.get("sources", []))
            with self.store.db.connection() as conn:
                count = conn.execute("SELECT count(*) FROM records WHERE section='service_events' AND owner=? AND json_extract(value,'$.discussion_id')=? AND json_extract(value,'$.sequence')<=?", ("service:" + self.store.id, self.job["target"], self.job["input"]["seq"])).fetchone()[0]
            if count <= 32 and len(ids) <= 32:
                with self.store.db.connection() as conn:
                    ids.update(row[0] for row in conn.execute("SELECT key FROM records WHERE section='service_events' AND owner=? AND json_extract(value,'$.discussion_id')=? AND json_extract(value,'$.sequence')<=?", ("service:" + self.store.id, self.job["target"], self.job["input"]["seq"])))
                for section in ("service_decisions", "service_requirements", "service_communication_actions", "service_review_operations", "service_discussion_rounds"):
                    with self.store.db.connection() as conn:
                        rows = conn.execute("SELECT key FROM records WHERE section=? AND owner=? AND json_extract(value,'$.discussion_id')=? LIMIT 33", (section, "service:" + self.store.id, self.job["target"])).fetchall()
                    ids.update(row[0] for row in rows)
                if len(ids) <= 32:
                    properties["identity"]["enum"] = sorted(ids)
        return tools

    def handlers(self):
        return {
            "service_read": self.read,
            "service_propose": self.propose,
            "service_read_code": self.read_code,
            "glob": self.glob,
            "search_text": self.search,
        }

    def read(self, kind, identity="", offset=0, limit=4000, workdir=None):
        contract = JobContract(self.store, self.job)
        contract.authorize(kind, identity)
        number(offset, "offset", 0, 2**63 - 1, integer=True)
        number(limit, "limit", 1, 16000, integer=True)
        if kind == "project_context":
            raw = json.dumps(self.project_context, ensure_ascii=False)
            return json.dumps({"text": raw[offset:offset + limit], "length": len(raw),
                               "next_offset": offset + limit if offset + limit < len(raw) else None})
        override = contract.override(kind, identity)
        if override is not None:
            raw = json.dumps(override, ensure_ascii=False)
            return json.dumps(
                {
                    "text": raw[offset : offset + limit],
                    "length": len(raw),
                    "next_offset": offset + limit
                    if offset + limit < len(raw)
                    else None,
                },
                ensure_ascii=False,
            )
        if (
            getattr(self.runtime, "maintenance", None)
            and self.runtime.maintenance.communication
        ):
            communication = self.runtime.maintenance.communication.store
            from .communication.store import SECTIONS

            if kind in SECTIONS:
                return json.dumps(
                    communication.read(kind, identity, offset, limit),
                    ensure_ascii=False,
                )
            if kind == "discussion_events":
                return json.dumps(
                    contract.events(offset, limit),
                    ensure_ascii=False,
                )
        number(offset, "offset", 0, 2**63 - 1, integer=True)
        number(limit, "limit", 1, 16000, integer=True)
        if kind == "health":
            value = self.runtime.operations.health_snapshot()
        elif kind == "policy":
            value = self.store.policy
        elif kind == "work":
            value = self.store.store.work(identity, self.store.id)
        elif kind == "work_list":
            value = (
                contract.work_directory(offset, limit)
                if self.job["kind"] == "conversation"
                else self.store.store.page(
                    self.store.id, cursor=offset, limit=min(limit, 20)
                )
            )
            return json.dumps(value, ensure_ascii=False)
        elif kind == "job":
            value = self.store.job_get(identity)
        elif kind in {
            "events",
            "messages",
            "reports",
            "decisions",
            "sources",
            "evidence",
            "changesets",
            "actions",
            "batches",
            "audits",
            "incidents",
            "upgrade_plans",
            "operations",
            "requirements",
        }:
            return json.dumps(
                self.store.read(kind, identity, offset=offset, limit=limit),
                ensure_ascii=False,
            )
        elif kind == "history":
            return json.dumps(contract.history(offset), ensure_ascii=False)
        else:
            raise ValueError("Unsupported service read kind.")
        raw = json.dumps(value, ensure_ascii=False)
        return json.dumps(
            {
                "text": raw[offset : offset + limit],
                "length": len(raw),
                "next_offset": offset + limit if offset + limit < len(raw) else None,
            },
            ensure_ascii=False,
        )

    def code_root(self):
        if self.job and not JobContract(self.store, self.job).handler.code:
            raise ValueError(
                "This job reads stored evidence rather than workspace code."
            )
        if (
            self.job
            and self.job["kind"] == "discussion"
            and self.job["input"].get("subject", {}).get("kind") == "change_request"
            and not self.job["input"].get("review_id")
        ):
            raise ValueError("PR code reads require a fixed review snapshot.")
        if (
            self.job
            and self.job["kind"] == "discussion"
            and self.job["input"].get("review_id")
        ):
            snapshot = self.runtime.maintenance.communication.store.get(
                "review_snapshots", self.job["input"]["review_id"]
            )
            from .communication.snapshots import verify

            verify(snapshot, self.runtime.spec.communication)
            return Path(snapshot["workspace"])
        if self.job and self.job["kind"] == "upgrade_assessment":
            plan = self.store.get("upgrade_plans", self.job["target"])
            return Path(
                self.runtime.spec.operations["upgrade_candidates"][plan["alias"]][
                    "workspace"
                ]
            )
        if self.job and self.job["kind"] == "audit":
            return Path(self.store.get("audits", self.job["target"])["workspace"])
        if self.job and self.job["kind"] in {"review", "ci_triage"}:
            return Path(
                self.store.get("changesets", self.job["target"])["candidate"][
                    "workspace"
                ]
            )
        return Path(self.runtime.spec.workspace)

    def glob(self, pattern, workdir=None):
        from homebrew_harness.tools.files import run_glob
        from homebrew_harness.security.file_visibility import file_access_scope

        with file_access_scope(restrict_archives=True):
            return run_glob(pattern, self.code_root())

    def search(self, text, pattern="**/*", limit=20, workdir=None):
        from homebrew_harness.tools.queries import search_text
        from homebrew_harness.security.file_visibility import file_access_scope

        with file_access_scope(restrict_archives=True):
            return search_text(
                text,
                pattern=pattern,
                limit=limit,
                workdir=self.code_root(),
            )

    def read_code(self, path, offset=0, limit=100, workdir=None):
        from homebrew_harness.tools.files import run_read
        from homebrew_harness.security.file_visibility import (
            model_parts,
            host_managed_parts,
        )

        root = self.code_root()
        if path == "project_context" and not (root / path).is_file():
            raise ValueError("project_context is JSON evidence, not a file. Use service_read(kind='project_context') or a real file path from its documents/subcommands.")
        if path.startswith("@base/"):
            if (
                not self.job
                or self.job["kind"] != "discussion"
                or not self.job["input"].get("review_id")
            ):
                raise ValueError("Base files require a fixed PR snapshot.")
            root = root.parent / "base"
            path = path[6:]
        _, parts = model_parts(root, path)
        if host_managed_parts(parts) or ".worktrees" in parts:
            raise ValueError(
                "Service reasoning cannot read runtime or credential directories."
            )
        number(offset, "offset", 0, 10000000, integer=True)
        number(limit, "limit", 1, 500, integer=True)
        if self.stage != "reply":
            from .project_context import file_version
            try:
                self.code_evidence[path] = file_version(root, path)
            except (OSError, ValueError):
                self.code_evidence[path] = None
        return run_read(path=path, offset=offset, limit=limit, workdir=root)

    def propose(self, proposal, workdir=None):
        self.proposal = None
        raw = json.loads(proposal)
        selected = raw.pop("evidence_paths", None) if isinstance(raw, dict) else None
        allowed = set(self.code_evidence)
        allowed.update(d["path"] for d in (self.project_context or {}).get("documents", []))
        if selected is not None and (not isinstance(selected, list) or len(selected) > 32
                or any(not isinstance(path, str) or path not in allowed for path in selected)):
            raise ValueError("evidence_paths must select provided or read project files.")
        self.selected_evidence = selected
        if self.stage == "assessment":
            from .communication.reasoning import assessment
            self.proposal = assessment(self.store, self.job, raw)
        elif self.stage == "reply":
            from .communication.reasoning import reply
            self.proposal = reply(self.assessment_plan, raw, self.store, self.job)
        else:
            self.proposal = JobContract(self.store, self.job).validate(raw)
        return json.dumps(
            {"candidate_ready": True, "persisted": False, "authorized": False}
        )

    def model_attempt(self):
        count = self.phase_counts.get(self.phase_key, 0)
        if count >= self.store.policy["job_turns"]:
            raise ValueError("Service phase model-call limit reached.")
        self.phase_counts[self.phase_key] = count + 1

    def complete_phase(self, calls, messages):
        from .phase_output import overview_signature, versions
        from .project_context import build
        if len(calls) != 1 or calls[0].name != "service_propose":
            if any(call.name == "service_propose" for call in calls):
                self.proposal = None
            messages.append({"role": "user", "content":
                "Read the necessary results, then call service_propose by itself with the final phase candidate. Free text does not complete this phase."})
            return False
        if self.proposal is None:
            return False
        basis = dict(self.code_evidence)
        for document in (self.project_context or {}).get("documents", []):
            if self.selected_evidence is not None and document["path"] in self.selected_evidence:
                basis.setdefault(document["path"], document["sha256"])
        if self.selected_evidence is not None:
            basis = {path: value for path, value in basis.items() if path in self.selected_evidence}
        root = self.code_root() if self.project_context is not None else None
        current = build(root, self.runtime.spec.communication or {}) if root is not None else None
        observed = versions(root, basis)
        changed = [{"path": path, "previous": value, "current": observed[path]}
                   for path, value in basis.items() if value != observed[path]]
        if overview_signature(current) != overview_signature(self.project_context) or changed:
            self.project_context = current
            self.code_evidence.update(observed)
            self.proposal = None
            messages.append({"role": "user", "content": json.dumps({
                "project_evidence_update": current, "changed_files": changed,
                "instruction": "These are current repository observations, not new user requirements. Re-read relevant files, ignore irrelevant evidence explicitly with evidence_paths, or revise your candidate; then submit a current final phase candidate."}, ensure_ascii=False)})
            return False
        saved = dict(self.store.job_get(self.job["id"]).get("phase_outputs", {}))
        saved[self.phase_key] = {"signature": self.phase_signature,
            "overview": overview_signature(current), "candidate": self.proposal,
            "evidence": observed, "calls": self.phase_counts.get(self.phase_key, 0)}
        self.store.prepare_job(self.job, phase_outputs=saved)
        self.code_evidence = observed
        return True

    def run(self, job):
        with self.model_control.lock:
            self.binding, self.gateway = self.model_control.select(job)
            try:
                return self._run(job)
            finally:
                self.model_control.save()

    def _run(self, job):
        from homebrew_harness.execution.execution_control import control_scope
        from .project_context import build

        self.job, self.stage, self.code_evidence = job, None, {}
        self.phase_counts, self.used_phases, self.phase_feedback = {}, set(), None
        deadline = Deadline.after(self.store.policy["job_seconds"])
        with control_scope(deadline=deadline, cancel_events=(self.stop,)):
            context = build(self.code_root(), self.runtime.spec.communication or {}) if JobContract(self.store, job).handler.code else None
            preparation = {"project_context": context}
            if job["kind"] == "discussion":
                from .communication.prompts import load
                self.discussion_prompts = self.store.job_get(job["id"]).get("discussion_prompts")
                if self.discussion_prompts is None:
                    self.discussion_prompts = load(
                        self.runtime.spec.communication.get("prompt_files", {}),
                        builtin=self.builtin_prompts,
                        roots=(self.runtime.store.root, self.runtime.spec.workspace),
                    )
                    preparation["discussion_prompts"] = self.discussion_prompts
            self.store.prepare_job(job, **preparation)
            from .phase_output import overview_signature, versions
            while True:
                self.used_phases = set()
                if job["kind"] == "discussion" and not job["input"].get("review_id"):
                    from .communication.reasoning import run
                    result = run(self, job, context, deadline)
                else:
                    result = self._execute(job, project=context, deadline=deadline)
                after = build(self.code_root(), self.runtime.spec.communication or {}) if context is not None else None
                outputs = self.store.job_get(job["id"]).get("phase_outputs", {})
                stale = [phase for phase in self.used_phases if phase in outputs and (
                    outputs[phase]["overview"] != overview_signature(after)
                    or outputs[phase]["evidence"] != versions(self.code_root() if context is not None else None, outputs[phase]["evidence"]))]
                assessment = self.store.job_get(job["id"]).get("discussion_assessment")
                if assessment and (assessment["signature"]["project"] != overview_signature(after)
                        or assessment.get("evidence", {}) != versions(self.code_root(), assessment.get("evidence", {}))):
                    stale.append("assessment")
                if not stale:
                    return result
                self.phase_feedback = {"changed_phases": stale,
                    "instruction": "Repository observations changed after phase preparation. Saved candidates are available in this job's phase_outputs. Re-evaluate affected evidence; keep user requirements and fixed execution objects unchanged."}
                context = after

    def _execute(self, job, *, project=None, stage=None, deadline=None, selected=None, plan=None):
        self.job, self.proposal = job, None
        self.project_context = project
        self.stage, self.assessment_plan, self.code_evidence = stage, plan, {}
        if selected is not None:
            self.binding, self.gateway = selected
        kind = job["kind"]
        contract = JobContract(self.store, job)
        primary = contract.primary()
        if kind in {"review", "ci_triage"}:
            self.store.delivery.git.verify(primary["candidate"])
        if kind == "audit":
            from .operations.audit import verify

            verify(self.runtime, primary)
        from .phase_output import identity, valid
        self.phase_key = stage or ("combined" if kind == "discussion" else kind)
        self.used_phases.add(self.phase_key)
        self.phase_signature = {**identity(job, self.phase_key, self.binding, self.discussion_prompts, plan),
                                "primary": fingerprint(primary)}
        cached = self.store.job_get(job["id"]).get("phase_outputs", {}).get(self.phase_key)
        root = self.code_root() if project is not None else None
        if valid(cached, self.phase_signature, project, root):
            try:
                if stage == "assessment":
                    from .communication.reasoning import assessment
                    candidate = assessment(self.store, job, {**cached["candidate"]["proposal"],
                        "reply_required": cached["candidate"]["reply_required"]})
                elif stage == "reply":
                    from .communication.reasoning import reply
                    candidate = reply(plan, cached["candidate"], self.store, job)
                else:
                    candidate = contract.validate(cached["candidate"])
            except (ValueError, KeyError, TypeError):
                self.phase_feedback = {"instruction": "Saved phase output no longer matches the current contract; submit a current candidate."}
            else:
                self.code_evidence = dict(cached["evidence"])
                self.proposal = candidate
                return self.proposal
        preview = {
            k: v
            for k, v in primary.items()
            if k
            not in {
                "run_spec",
                "result",
                "facts",
                "candidate",
                "baseline",
                "files",
                "workspace",
                "plans",
                "integration",
                "integration_quality",
                "merge_rules",
                "candidate_history",
            }
        }
        if primary.get("candidate") and kind in {"review", "ci_triage"}:
            preview["candidate"] = {
                k: primary["candidate"][k] for k in ("commit", "tree", "delta")
            }
        elif kind == "upgrade_assessment":
            preview["candidate"] = {
                k: primary["candidate"][k] for k in ("runtime", "package_hash", "proof")
            }
        directory = (
            self.runtime.store.root / ".harness" / "service_reasoning" / job["id"]
        )
        directory.mkdir(parents=True, exist_ok=True)
        instruction = contract.instruction(self.skills)
        if stage in {"assessment", "reply"}:
            from .job_contracts import COMMON
            name = "assessment" if stage == "assessment" else "draft"
            instruction = COMMON + self.discussion_prompts["texts"][name]
        elif kind == "discussion":
            instruction += "\n" + self.discussion_prompts["texts"]["assessment"]
        if kind == "discussion":
            budget, sources = 6000, []
            for identity in reversed(job["input"].get("events", [])):
                if budget == 0:
                    break
                contract.authorize("events", identity)
                event = self.store.get("events", identity)
                body = event.get("content", {}).get("body", "")
                text = body[:budget]
                sources.append({"id": identity, "text": text, "body_length": len(body),
                                "truncated": len(text) < len(body)})
                budget -= len(text)
            preview["source_previews"] = list(reversed(sources))
        if project:
            preview["project_context"] = project
        if plan:
            from .communication.reasoning import public
            preview["lead_assessment"] = public(plan)
        messages = [
            {"role": "system", "content": instruction},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "job": {**{k: job[k] for k in ("id", "kind", "target", "revision")},
                                **({"phase": stage} if stage else {})},
                        "policy": contract.policy(),
                        "input": preview,
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        if self.phase_feedback:
            messages.append({"role": "user", "content": json.dumps(self.phase_feedback, ensure_ascii=False)})
        components = LoopComponents(
            skill_loader=self.skills,
            capabilities=Capabilities(),
            service_control=self,
            completion_check=self.complete_phase,
            model_attempt_hook=self.model_attempt,
            stop_event=self.stop,
            tool_allowlist=frozenset(
                {"service_read", "service_propose", "load_skill", "compact"}
            )
            | (
                {"service_read_code", "glob", "search_text"}
                if contract.handler.code and stage != "reply"
                else set()
            ),
            context_chars=self.binding.context_chars,
            worker_output_tokens=self.binding.max_output_tokens,
        )
        with (
            output_scope(),
            self.gateway.scope(
                role="service",
                context_id=job["id"],
                deadline=deadline or Deadline.after(self.store.policy["job_seconds"]),
                cancel_event=self.stop,
            ),
            _prepare_loop(
                self.gateway, self.binding.model, directory, components
            ) as loop,
        ):
            loop.run(
                messages,
                max(1, self.store.policy["job_turns"] - self.phase_counts.get(self.phase_key, 0)),
                active_request=json.dumps(preview, ensure_ascii=False),
            )
        if self.proposal is None:
            raise ValueError("Service reasoning did not submit a structured proposal.")
        if kind in {"review", "ci_triage"}:
            self.store.delivery.git.verify(primary["candidate"])
        if kind == "audit":
            verify(self.runtime, primary)
        return self.proposal

    def close(self):
        self.model_control.close()
        while not self.models.close_when_idle(0.1):
            pass
