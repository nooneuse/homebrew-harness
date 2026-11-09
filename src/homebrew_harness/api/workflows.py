"""Nonblocking fixed workflows with native execution locks and saved results."""

import threading
import uuid

from homebrew_harness.orchestration.workflow_runtime import (
    WorkflowRuntime,
    ModelWorkflowRunner,
    WorkflowStore,
    WorkflowLimits,
)
from homebrew_harness.execution.model_gateway import GatewayLimits, ModelGateway
from homebrew_harness.runtime.runtime_control import Shutdown
from homebrew_harness.runtime.output import output_scope
from homebrew_harness.storage.api_state import stable_id, digest
from .base import Handle, host_operation
from .types import (
    ExecutionRef,
    ExecutionSnapshot,
    ExecutionResult,
    SubmissionReceipt,
    StaleReference,
    SDKError,
)


class WorkflowHandle(Handle):
    def __init__(self, core, identity, name, *, runtime=None, owner=None):
        super().__init__(
            core, ExecutionRef("workflow", identity), owned=owner is not None
        )
        self.name, self.runtime, self.owner = name, runtime, owner
        self.shutdown = Shutdown()
        self.models = None
        self.closing = False

    def submission(self):
        row = self.core.workflows.record(self.id)
        return SubmissionReceipt(
            self.ref,
            row["request_id"],
            True,
            "startup_pending"
            if self.snapshot().state == "startup_pending"
            else "started",
        )

    def snapshot(self):
        if self.owner is not None:
            value = self.owner.response()
        else:
            try:
                value = self._saved()
            except (ValueError, RuntimeError):
                return ExecutionSnapshot(
                    self.ref,
                    "startup_pending",
                    reason="Inspect the accepted workflow launch before retrying.",
                    persistence_confirmed=True,
                    execution_pending=True,
                )
        task = value.get("task", {})
        state = task.get("status", "starting")
        result = value.get("result")
        confirmed = value.get("persistence_confirmed", False)
        pending = value.get("execution_pending", False)
        if self.owner is not None and not self.owner.result.done():
            state = (
                "pausing"
                if self.owner.reason
                else "running"
                if self.owner.view is not None
                else "starting"
            )
            pending = True
            confirmed = False
        elif self.owner is not None:
            confirmed = bool(
                self.owner.snapshot
                and task.get("cleanup", {}).get("state") == "complete"
                and self.runtime.store.confirmed(self.id, self.owner.snapshot)
            )
            if not confirmed:
                state = "blocked" if self.owner.snapshot else "startup_pending"
                pending = False
                task = {**task, "inspection_required": True}
        return ExecutionSnapshot(
            self.ref,
            state,
            int(task.get("progress", {}).get("calls", 0)),
            task.get("error", ""),
            output_available=confirmed and result is not None,
            persistence_confirmed=confirmed,
            execution_pending=pending,
            cleanup=task.get("cleanup", {}),
            details={
                "progress": task.get("progress", {}),
                "inspection_required": task.get("inspection_required", False),
            },
        )

    def result(self):
        self._require_result()
        value = self.owner.response() if self.owner else self._saved()
        result = value.get("result")
        return ExecutionResult(
            self.ref,
            value["task"]["status"],
            text=result if isinstance(result, str) else None,
            value=result,
            error=result.get("error") if isinstance(result, dict) else None,
            evidence={"workflow_id": self.id},
        )

    def _saved(self):
        store = self.core.workflows.reader()
        snapshot = store.inspect(self.id)
        confirmed = snapshot["task"].get("cleanup", {}).get(
            "state"
        ) == "complete" and store.confirmed(self.id, snapshot)
        result = (
            store.read(self.id, ".output.json", object_only=False)
            if confirmed
            else None
        )
        return {
            "task": snapshot["task"],
            "result": result,
            "execution_pending": not confirmed,
            "persistence_confirmed": confirmed,
        }

    @host_operation
    def pause(self):
        if self.owner is None:
            raise SDKError(
                "Only the live owner can pause this in-process workflow.",
                code="not_owned",
                ref=self.ref,
            )
        self.owner.request_stop("Workflow paused by its host.")
        return self.snapshot()

    @host_operation
    def resume(self):
        snapshot = self.snapshot()
        if snapshot.execution_pending or snapshot.state != "paused":
            raise SDKError(
                "Workflow resume requires a paused and settled execution.",
                code="recovery_required",
                ref=self.ref,
            )
        row = self.core.workflows.record(self.id)
        self.close()
        if not self.wait_closed(self.core.config.settings.shutdown_seconds):
            raise SDKError(
                "Previous workflow resources remain pending.",
                code="cleanup_pending",
                ref=self.ref,
            )
        return self.core.workflows._start(
            self.name,
            None,
            identity=self.id,
            resume=True,
            model_profile=row.get("model_profile"),
            expected_binding=row.get("binding"),
        )

    def close(self):
        if self.owner is not None and not self.owner.result.done():
            self.owner.request_stop("Workflow paused by its host.")
        if self.runtime is not None:
            self.runtime.close(0)
        self.closing = True

        def finish():
            if self.runtime is not None:
                self.runtime.wait_closed(None)
            if self.models is not None:
                self.models.close()

        self.shutdown.request(finish, 0)

    def wait_closed(self, timeout=5):
        if self.closing:
            return self.shutdown.finished.wait(timeout)
        return self.runtime is None or self.runtime.wait_closed(timeout)


class Workflows:
    def __init__(self, core):
        self.core, self.live, self.lock = core, {}, threading.RLock()
        self._reader = None

    def _resources(self, model_profile=None):
        from dataclasses import replace

        settings = (
            replace(self.core.config.settings, lead_model=model_profile)
            if model_profile
            else self.core.config.settings
        )
        with output_scope():
            builder = self.core.config.builder(self.core.workdir, settings=settings)
        models = None
        try:
            models = builder.models()
            binding = models.open(
                context_chars=settings.context_chars,
                output_tokens=settings.output_tokens,
            )
            gateway = ModelGateway(
                binding.client,
                GatewayLimits(request_timeout=settings.request_seconds),
                workdir=self.core.workdir,
            )
            models.track_gateway(gateway)
            runner = ModelWorkflowRunner(binding, gateway=gateway)
            runtime = WorkflowRuntime(
                self.core.workdir,
                runner,
                builder.registry,
                limits=WorkflowLimits(
                    timeout_seconds=settings.total_seconds,
                    max_output_tokens=min(settings.output_tokens, 32000),
                ),
            )
            return runtime, models
        except BaseException:
            if models is not None:
                models.close()
            raise
        finally:
            builder.close()

    def reader(self):
        if self._reader is None:
            self._reader = WorkflowStore(self.core.workdir)
        return self._reader

    @host_operation
    def start(self, name, args=None, *, request_id=None, model_profile=None):
        self.core.check_open()
        request_id = request_id or "sdk_" + uuid.uuid4().hex
        key = stable_id("workflow", str(self.core.workdir), request_id)
        payload = digest([name, args, model_profile, self.core.config.identity()])
        with self.lock:
            prior = self.core.store.get("api_submissions", key)
            if prior:
                if prior["payload"] != payload:
                    raise StaleReference(
                        "Submission ID has another workflow/configuration."
                    )
                return self.get(prior["id"])
            runtime, models = self._resources(model_profile)
            try:
                runtime.validate_submission(name, args)
                db = self.core.store.backend()
                with db.write() as conn:
                    prior = db._get(conn, "api_submissions", key)
                    if prior:
                        if prior["payload"] != payload:
                            raise StaleReference(
                                "Submission ID has another workflow/configuration."
                            )
                        identity = prior["id"]
                    else:
                        identity = "wf_" + stable_id(
                            "workflow-execution", str(self.core.workdir), request_id
                        )
                        db._put(
                            conn,
                            "api_submissions",
                            key,
                            {
                                "kind": "workflow",
                                "id": identity,
                                "name": name,
                                "args": args,
                                "payload": payload,
                                "request_id": request_id,
                                "model_profile": model_profile,
                                "binding": runtime.runner.identity,
                                "state": "accepted",
                            },
                        )
                if prior:
                    return self.get(identity)
                return self._start(
                    name, args, identity=identity, resources=(runtime, models)
                )
            except Exception:
                known = self.core.store.get("api_submissions", key)
                if known and known["payload"] == payload:
                    if known["id"] in self.live:
                        return self.live[known["id"]]
                    return WorkflowHandle(self.core, known["id"], name)
                raise
            finally:
                if not any(h.runtime is runtime for h in self.live.values()):
                    runtime.close()
                    models.close()

    def _start(
        self,
        name,
        args,
        *,
        identity,
        resume=False,
        model_profile=None,
        resources=None,
        expected_binding=None,
    ):
        self.core.check_open()
        with self.lock:
            if (
                identity in self.live
                and self.live[identity].snapshot().execution_pending
            ):
                raise SDKError(
                    "Workflow already has an active owner.", code="workspace_busy"
                )
            runtime, models = resources or self._resources(model_profile)
            try:
                if (
                    expected_binding is not None
                    and runtime.runner.identity != expected_binding
                ):
                    raise StaleReference("Saved workflow model binding changed.")
                with output_scope():
                    owner = runtime.start(
                        name,
                        args,
                        resume_from_run_id=identity if resume else None,
                        identity=identity,
                    )
            except BaseException:
                runtime.close()
                models.close()
                raise
            handle = WorkflowHandle(
                self.core, identity, name, runtime=runtime, owner=owner
            )
            handle.models = models
            self.live[identity] = handle
            return handle

    def record(self, identity):
        row = next((r for r in self.list() if r["id"] == identity), None)
        if row is None:
            raise StaleReference("Unknown workflow.")
        return row

    @host_operation
    def resume(self, identity):
        return self.get(identity).resume()

    def get(self, identity):
        if identity in self.live:
            return self.live[identity]
        row = self.record(identity)
        return WorkflowHandle(self.core, identity, row["name"])

    def list(self):
        return tuple(
            r
            for r in self.core.store.items("api_submissions").values()
            if r.get("kind") == "workflow"
        )
