"""Service and work handles use native Runs and owner-only local control."""

import uuid

from homebrew_harness.services.spec import ServiceSpec, request_key
from homebrew_harness.services.store import ServiceStore
from homebrew_harness.services.control import ControlClient
from homebrew_harness.services.launcher import live, launch, recover_launch, gone
from .base import Handle, host_operation
from homebrew_harness.services.views import (
    WorkHandle as WorkHandle,
    WorkService,
    ServiceControls,
)
from .types import ExecutionRef, ExecutionSnapshot, SDKError


class ServiceHandle(ServiceControls, Handle):
    def __init__(
        self, core, identity, *, runtime=None, control=False, lifetime="attached"
    ):
        super().__init__(
            core, ExecutionRef("service", identity), owned=runtime is not None
        )
        self.runtime = runtime
        self.control = control or self.owned
        self.lifetime = lifetime
        self.store = ServiceStore(core.workdir)
        self.client = ControlClient(core.workdir)
        self.work = WorkService(self)

    def snapshot(self):
        meta = self.store.get(self.id)
        connected = live(self.core.workdir)
        state = meta["state"]
        phase = (meta.get("launch") or {}).get("phase")
        if not connected and state != "stopped":
            state = "unattached"
        if not connected and phase == "pending":
            state = "startup_pending"
        return ExecutionSnapshot(
            self.ref,
            state,
            meta["revision"],
            meta.get("error") or "",
            persistence_confirmed=True,
            execution_pending=connected or not meta["resources_closed"],
            cleanup={
                "resources_closed": meta["resources_closed"],
                "live_owner": connected,
            },
            details={
                "profile": meta["spec"]["profile"],
                "generation": meta["generation"],
                "active_work": meta.get("active_work"),
                "active_run": meta.get("active_run"),
                "launch": meta.get("launch"),
                "scope": meta["scope"],
                "heartbeat_at": meta["heartbeat_at"],
            },
        )

    def query(self, method, payload=None):
        if self.runtime is not None and not self.runtime.finished.is_set():
            return self.runtime.dispatch(self.id, method, payload or {})
        if live(self.core.workdir):
            return self.client.call(self.id, method, payload)
        if method == "work_list":
            return self.store.page(self.id, **(payload or {}))
        if method == "schedules":
            return self.store.page(self.id, section="service_jobs", **(payload or {}))
        if method == "receipt":
            return self.store.runtime.get(
                "service_commands",
                request_key("command", self.id, payload["request_id"]),
            )
        if method == "focus_get":
            return self.store.runtime.get(
                "service_focus", self.id + ":" + payload["context"]
            )
        if method == "work_input":
            return self.store.input_page(
                self.id,
                payload["id"],
                **{k: v for k, v in payload.items() if k != "id"},
            )
        if method == "snapshot":
            return self.snapshot().data()
        if method in {"work_get", "requests"}:
            from .core import Harness
            from homebrew_harness.services.runtime import saved_config

            meta = self.store.get(self.id)
            work = (
                self.store.work(payload["id"], self.id)
                if method == "work_get"
                else self.store.work(payload["work_id"], self.id)
                if payload and payload.get("work_id")
                else self.store.next_work(self.id)
            )
            if work is None:
                return []
            if not work["run_id"]:
                return work if method == "work_get" else []
            with Harness(
                workdir=meta["spec"]["workspace"], config=saved_config(meta["config"])
            ) as observer:
                if method == "requests":
                    return [
                        r.data()
                        for r in observer.requests.list(
                            target=ExecutionRef("run", work["run_id"])
                        )
                    ]
                return {
                    **self.store.summary(work),
                    "execution": observer.runs.get(work["run_id"]).snapshot().data(),
                }
        raise SDKError(
            "Start the saved service before sending controls.",
            code="service_not_running",
            ref=self.ref,
        )

    @host_operation
    def command(self, method, payload, request_id=None):
        if not self.control:
            raise SDKError(
                "Get this service with control=True before sending controls.",
                code="not_owned",
                ref=self.ref,
            )
        request_id = request_id or "control_" + uuid.uuid4().hex
        if self.runtime is not None and not self.runtime.finished.is_set():
            return self.runtime.dispatch(self.id, method, payload, request_id)
        return self.client.call(self.id, method, payload, request_id)

    def close(self):
        self.client.close()
        if self.runtime is not None:
            return self.runtime.close()

    def wait_closed(self, timeout=5):
        return self.runtime is None or self.runtime.wait_closed(timeout)


class Services:
    def __init__(self, core):
        self.core = core
        self.live = {}
        self.observed = {}
        self.lock = __import__("threading").RLock()

    def observe(self, identity, *, control=False, lifetime="attached"):
        with self.lock:
            key = (identity, control, lifetime)
            handle = self.observed.get(key)
            if handle is None or handle.client.closed:
                handle = ServiceHandle(
                    self.core, identity, control=control, lifetime=lifetime
                )
                self.observed[key] = handle
            return handle

    def handles(self):
        with self.lock:
            return [*self.live.values(), *self.observed.values()]

    @host_operation
    def start(self, spec=None, *, identity=None, request_id=None, lifetime="attached"):
        from homebrew_harness.services.runtime import prepare_binding, ServiceRuntime

        if lifetime not in {"attached", "detached"}:
            raise ValueError("Service lifetime must be attached or detached.")
        store = ServiceStore(self.core.workdir)
        with self.lock:
            if identity is None:
                if not isinstance(spec, ServiceSpec):
                    raise ValueError("Expected ServiceSpec.")
                spec = ServiceSpec(**spec.data())
                settings = self.core.config.settings
                inherited = {
                    "model_profile": settings.lead_model,
                    "context_chars": settings.context_chars,
                    "output_tokens": settings.output_tokens,
                    "total_seconds": settings.total_seconds,
                    "episode_seconds": settings.turn_seconds,
                    "request_seconds": settings.request_seconds,
                    "episode_model_calls": settings.max_turns,
                    "memory_mode": settings.memory_mode,
                    "write_paths": settings.write_paths,
                    "allow_tests": settings.allow_tests,
                }
                spec = ServiceSpec(
                    **{
                        **spec.data(),
                        "run_defaults": {**inherited, **spec.run_defaults},
                    }
                )
                binding = prepare_binding(self.core, spec)
                meta = store.create(
                    spec,
                    self.core.config.settings.identity(),
                    binding,
                    request_id or "default-service",
                )
                identity = meta["id"]
            else:
                meta = store.get(identity)
            if (
                identity in self.live
                and not self.live[identity].runtime.finished.is_set()
            ):
                return self.live[identity]
            if live(self.core.workdir):
                return self.get(identity, control=True)
            phase = (meta.get("launch") or {}).get("phase")
            if phase == "pending" or phase == "accepted" and not gone(meta.get("pid")):
                return self.observe(identity, control=True, lifetime=lifetime)
            if lifetime == "detached":
                launch(store, identity)
                return self.observe(identity, control=True, lifetime=lifetime)
            # The active root retains the service owner, including closing resources.
            runtime = ServiceRuntime(self.core, store, identity)
            handle = ServiceHandle(self.core, identity, runtime=runtime, control=True)
            self.live[identity] = handle
            try:
                runtime.start()
            except Exception as error:
                runtime.start_error = type(error).__name__
                runtime.close()
            return handle

    @host_operation
    def get(self, identity, *, control=False):
        ServiceStore(self.core.workdir).get(identity)
        if identity in self.live:
            return self.live[identity]
        return self.observe(identity, control=control)

    @host_operation
    def recover_launch(self, identity, token):
        return recover_launch(ServiceStore(self.core.workdir), identity, token)
