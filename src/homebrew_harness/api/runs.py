"""Run handles use the existing supervisor, durable controls and worker entry."""

import threading
import uuid
from dataclasses import replace

from homebrew_harness.runs.spec import RunSpec
from homebrew_harness.runs.supervisor import RunSupervisor
from homebrew_harness.runs.detached import launch_detached, detached_status
from homebrew_harness.runtime.runtime_control import Workers, WorkAccepted
from homebrew_harness.runtime.output import output_scope
from homebrew_harness.runtime.task_routing import TaskRoute
from homebrew_harness.storage.api_state import digest, stable_id
from .base import Handle, host_operation
from .types import (
    ExecutionRef,
    ExecutionSnapshot,
    ExecutionResult,
    SubmissionReceipt,
    SDKError,
    StaleReference,
    UnsupportedOperation,
)


class _Replayed(Exception):
    def __init__(self, ref):
        self.ref = ref


class RunHandle(Handle):
    def __init__(
        self, core, identity, *, owned=False, control=False, lifetime="attached"
    ):
        super().__init__(core, ExecutionRef("run", identity), owned=owned)
        self.control_enabled = control or owned
        self.lifetime = lifetime
        self.supervisor = None
        self.workers = Workers()
        self.error = None
        self.start_lock = threading.RLock()
        self.closing = False
        self.closed_cache = False

    def submission(self):
        row = next(
            (
                r
                for r in self.core.store.items("api_submissions").values()
                if r.get("kind") == "run" and r["id"] == self.id
            ),
            None,
        )
        if row is None:
            raise StaleReference("Run was not submitted through the SDK.", ref=self.ref)
        value = self.core.run_store.get(self.id)
        return SubmissionReceipt(
            self.ref,
            row["request_id"],
            True,
            "startup_pending"
            if self.error
            else "started"
            if value["generation"]
            else "accepted",
        )

    def snapshot(self):
        value = self.core.run_store.status(self.id)
        outcome = value.get("last_outcome") or {}
        state = (
            "startup_pending"
            if value["status"] == "queued" and self.error
            else value["status"]
        )
        terminal = value["status"] in {"completed", "failed", "cancelled"}
        pending = not value.get("cleanup_complete", False)
        if terminal:
            pending = (
                pending
                or not value.get("finalized_at")
                or detached_status(self.core.run_store, self.id).get("live", False)
            )
            if self.owned:
                pending = pending or not self.workers.wait(0)
        return ExecutionSnapshot(
            self.ref,
            state,
            value["revision"],
            outcome.get("reason_code", ""),
            details={
                "episode": value["episode"],
                "selected_model": value.get("selected_model"),
                "member_outcomes": value.get("member_outcomes", {}),
                "mcp_resources": value.get("mcp_resources", []),
                "launch_error": self.error,
            },
            requests=self.requests(),
            output_available=value["status"] in {"completed", "failed", "cancelled"}
            and bool(value.get("last_outcome")),
            persistence_confirmed=True,
            execution_pending=pending,
            cleanup={
                "complete": value.get("cleanup_complete", False),
                "finalized": bool(value.get("finalized_at")),
            },
        )

    def result(self):
        self._require_result()
        value = self.core.run_store.get(self.id)
        outcome = value["last_outcome"]
        return ExecutionResult(
            self.ref,
            value["status"],
            outcome.get("text", ""),
            value=outcome,
            evidence={
                "checkpoint": value.get("checkpoint"),
                "verification_ref": outcome.get("verification_ref"),
            },
        )

    def _require_control(self):
        self.core.check_open()
        if not self.control_enabled:
            raise UnsupportedOperation(
                "This handle observes a Run. Get it with control=True to send explicit controls.",
                ref=self.ref,
            )

    def _ensure_started(self):
        with self.start_lock:
            return self._start_supervisor()

    def _start_supervisor(self):
        self.error = None
        self.closing = False
        self.closed_cache = False
        if self.lifetime == "detached":
            self.core.config.require_reconstructable()
            return launch_detached(
                self.core.run_store, self.id, self.core.config.settings
            )
        if self.supervisor is not None and not self.workers.wait(0):
            return
        if detached_status(self.core.run_store, self.id).get("live"):
            return
        from homebrew_harness.runs.services import RunServices
        from homebrew_harness.execution.model_connections import read_environment

        settings = self.core.config.settings
        services = RunServices.load(
            self.core.run_store,
            settings.control_config,
            environment=read_environment(settings.env_file, self.core.workdir),
        )
        try:
            self.supervisor = RunSupervisor(
                self.core.run_store,
                host_arguments=settings.worker_arguments(self.core.workdir),
                on_update=lambda _id: self.core.changed.set(),
                services=services,
            )
        except BaseException:
            services.close()
            raise

        def supervise():
            try:
                with output_scope():
                    self.supervisor.supervise(self.id)
            except Exception as error:
                self.error = type(error).__name__
            finally:
                self.supervisor.close()
                self.core.changed.set()

        try:
            self.workers.start(
                supervise, name="sdk-run-supervisor", track_operation=False
            )
        except WorkAccepted:
            pass

    @host_operation
    def supervise(self):
        """Attach the native supervisor without altering saved controls or faults."""
        self._require_control()
        self.owned = True
        self.lifetime = "attached"
        self.core.runs.owned[self.id] = self
        self._ensure_started()
        return self.snapshot()

    @host_operation
    def steer(self, text, *, request_id=None, expected_turn_id=None):
        self._require_control()
        request_id = request_id or "sdk_" + uuid.uuid4().hex
        value = self.core.run_store.submit(
            self.id,
            text,
            mode="steer",
            input_id="usr_" + stable_id("run-input", self.id, request_id),
            expected_turn_id=expected_turn_id,
            literal=True,
        )
        self.core.changed.set()
        return SubmissionReceipt(
            self.ref, request_id, True, value.get("state", "accepted")
        )

    @host_operation
    def interrupt(self, text, *, request_id=None):
        self._require_control()
        request_id = request_id or "sdk_" + uuid.uuid4().hex
        value = self.core.run_store.submit(
            self.id,
            text,
            mode="interrupt",
            input_id="usr_" + stable_id("run-input", self.id, request_id),
            literal=True,
        )
        self.core.changed.set()
        return SubmissionReceipt(
            self.ref, request_id, True, value.get("state", "accepted")
        )

    @host_operation
    def pause(self):
        self._require_control()
        self.core.run_store.control(self.id, "pause")
        return self.snapshot()

    @host_operation
    def resume(self):
        self._require_control()
        self.core.run_store.control(self.id, "run")
        self.owned = self.lifetime != "detached"
        if self.owned:
            self.core.runs.owned[self.id] = self
        self._ensure_started()
        return self.snapshot()

    @host_operation
    def cancel(self):
        self._require_control()
        self.core.run_store.control(self.id, "cancel")
        self._ensure_started()
        return self.snapshot()

    @host_operation
    def detach(self):
        self._require_control()
        self.core.config.require_reconstructable()
        if self.supervisor is not None and not self.workers.wait(0):
            self.core.run_store.control(self.id, "pause")
            if not self.wait_closed(self.core.config.settings.shutdown_seconds):
                raise SDKError(
                    "Owned execution is still closing; retry detach after cleanup.",
                    code="cleanup_pending",
                    ref=self.ref,
                )
            self.core.run_store.control(self.id, "run")
        result = launch_detached(
            self.core.run_store, self.id, self.core.config.settings
        )
        self.lifetime = "detached"
        self.owned = False
        self.core.runs.owned.pop(self.id, None)
        return result

    def close(self):
        self.closing = True
        if self.owned and self.supervisor is not None and not self.workers.wait(0):
            if self.core.run_store.get(self.id)["status"] not in {
                "completed",
                "failed",
                "cancelled",
            }:
                try:
                    self.core.run_store.control(self.id, "pause")
                except ValueError:
                    if self.core.run_store.get(self.id)["status"] not in {
                        "completed",
                        "failed",
                        "cancelled",
                    }:
                        raise

    def wait_closed(self, timeout=5):
        if self.closed_cache:
            return True
        if not self.workers.wait(timeout):
            return False
        if not self.owned:
            return True
        if timeout == 0:
            return False
        closed = self.core.run_store.get(self.id).get("cleanup_complete", False)
        if closed and self.closing:
            self.closed_cache = True
        return closed


class Runs:
    def __init__(self, core):
        self.core, self.owned, self.lock = core, {}, threading.RLock()

    @host_operation
    def start(self, spec, *, lifetime="attached", request_id=None):
        self.core.check_open()
        self.core.config.require_reconstructable()
        if not isinstance(spec, RunSpec):
            raise ValueError("Expected RunSpec.")
        if lifetime not in {"attached", "detached"}:
            raise ValueError("Lifetime must be attached or detached.")
        request_id = request_id or "sdk_" + uuid.uuid4().hex
        key = stable_id("run", str(self.core.workdir), request_id)
        payload = digest([spec.data(), self.core.config.identity(), lifetime])
        prior = self.core.store.get("api_submissions", key)
        if prior:
            if prior["payload"] != payload:
                raise StaleReference("Submission ID has another Run/configuration.")
            return self._replay(prior["id"], lifetime)
        bindings, head, control_binding = self.prepare(spec)

        def accept(db, conn, value):
            previous = db._get(conn, "api_submissions", key)
            if previous:
                if previous["payload"] != payload:
                    raise StaleReference("Submission ID has another payload.")
                raise _Replayed(previous["id"])
            value.update(
                bindings=bindings,
                git_head=head,
                expected_binding=bindings["model"],
                expected_models=bindings["models"],
            )
            value.update(control_binding)
            if spec.mcp:
                value["expected_mcp"] = bindings["mcp"]
            db._put(
                conn,
                "api_submissions",
                key,
                {
                    "id": value["id"],
                    "kind": "run",
                    "request_id": request_id,
                    "payload": payload,
                    "checkpoint": "lead:" + value["session_id"],
                },
            )

        try:
            state = self.core.run_store.create(spec, _accept=accept)
        except _Replayed as replay:
            return self._replay(replay.ref, lifetime)
        except Exception:
            known = self.core.store.get("api_submissions", key)
            if not known:
                raise
            state = self.core.run_store.get(known["id"])
        with self.lock:
            handle = RunHandle(
                self.core,
                state["id"],
                owned=lifetime == "attached",
                control=True,
                lifetime=lifetime,
            )
            if handle.owned:
                self.owned[handle.id] = handle
            try:
                handle._ensure_started()
            except Exception as error:
                handle.error = type(error).__name__
        return handle

    @host_operation
    def prepare(self, spec, *, model_bindings=None):
        """Validate and bind a Run without accepting or starting execution."""
        self.core.config.require_reconstructable()
        if not isinstance(spec, RunSpec):
            raise ValueError("Expected RunSpec.")
        c = self.core.config.settings
        settings = replace(
            c,
            lead_model=spec.model_profile or c.lead_model,
            context_chars=spec.context_chars,
            output_tokens=spec.output_tokens,
            total_seconds=spec.total_seconds,
            turn_seconds=spec.episode_seconds,
            request_seconds=spec.request_seconds,
            max_turns=spec.episode_model_calls,
            memory_mode=spec.memory_mode,
            write_paths=spec.write_paths,
            allow_tests=spec.allow_tests,
            mcp_delegation=spec.mcp_delegation,
        )
        with output_scope():
            builder = self.core.config.builder(
                self.core.workdir, settings=settings, model_bindings=model_bindings
            )
            try:
                builder.run_spec(
                    TaskRoute(
                        execution_mode="managed",
                        capabilities=spec.enabled,
                        mcp=spec.mcp,
                        stages=spec.stages,
                        members=spec.members,
                    ),
                    spec.prompt,
                    goal=spec.goal,
                )
                bindings, head, control_binding = (
                    builder.run_bindings,
                    builder.run_head,
                    builder.run_control_binding,
                )
            finally:
                builder.close()

        return bindings, head, control_binding

    def _replay(self, identity, lifetime):
        with self.lock:
            handle = self.get(identity, control=True)
            if self.core.run_store.get(identity)["status"] == "queued":
                handle.lifetime = lifetime
                handle.owned = lifetime == "attached"
                if handle.owned:
                    self.owned[identity] = handle
                try:
                    handle._ensure_started()
                except Exception as error:
                    handle.error = type(error).__name__
            return handle

    def get(self, identity, *, control=False):
        self.core.run_store.get(identity)
        if identity in self.owned:
            return self.owned[identity]
        return RunHandle(
            self.core,
            identity,
            control=control,
            lifetime="detached"
            if detached_status(self.core.run_store, identity).get("live")
            else "attached",
        )

    def list(self):
        return tuple(self.core.store.items("runs").values())
