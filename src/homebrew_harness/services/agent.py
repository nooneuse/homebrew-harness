"""Narrow service reasoning on the existing prepared Agent Loop."""

import json
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
        )
        self.models.track_gateway(self.gateway)
        self.base_binding = self.binding
        self.skills = SkillLoader(Path(__file__).parent / "skills")
        self.job = None
        self.proposal = None
        self.override_bindings = {}
        from .model_control import ServiceModels

        self.model_control = ServiceModels(self)
        for alias in {
            runtime.spec.operations.get("audit_model"),
            (runtime.spec.delivery or {}).get("review_model"),
            (runtime.spec.communication or {}).get("model_profile"),
            (runtime.spec.communication or {}).get("review_model"),
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
            "skills": fingerprint(
                {k: v.content for k, v in self.skills.skills.items()}
            ),
        }

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
        return run_read(path=path, offset=offset, limit=limit, workdir=root)

    def propose(self, proposal, workdir=None):
        raw = json.loads(proposal)
        self.proposal = JobContract(self.store, self.job).validate(raw)
        return json.dumps(
            {"candidate_ready": True, "persisted": False, "authorized": False}
        )

    def run(self, job):
        with self.model_control.lock:
            self.binding, self.gateway = self.model_control.select(job)
            try:
                return self._run(job)
            finally:
                self.model_control.save()

    def _run(self, job):
        self.job, self.proposal = job, None
        kind = job["kind"]
        contract = JobContract(self.store, job)
        primary = contract.primary()
        if kind in {"review", "ci_triage"}:
            self.store.delivery.git.verify(primary["candidate"])
        if kind == "audit":
            from .operations.audit import verify

            verify(self.runtime, primary)
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
        messages = [
            {"role": "system", "content": instruction},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "job": {
                            k: job[k] for k in ("id", "kind", "target", "revision")
                        },
                        "policy": contract.policy(),
                        "input": preview,
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        components = LoopComponents(
            skill_loader=self.skills,
            capabilities=Capabilities(),
            service_control=self,
            stop_event=self.stop,
            tool_allowlist=frozenset(
                {"service_read", "service_propose", "load_skill", "compact"}
            )
            | (
                {"service_read_code", "glob", "search_text"}
                if contract.handler.code
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
                deadline=Deadline.after(self.store.policy["job_seconds"]),
                cancel_event=self.stop,
            ),
            _prepare_loop(
                self.gateway, self.binding.model, directory, components
            ) as loop,
        ):
            loop.run(
                messages,
                self.store.policy["job_turns"],
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
