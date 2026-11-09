"""Typed demand routing through the existing interaction host."""

import uuid
from homebrew_harness.runtime.interaction_host import InteractionHost
from homebrew_harness.runtime.output import output_scope
from homebrew_harness.storage.api_state import stable_id
from .base import Handle, host_operation
from .types import ExecutionRef, ExecutionSnapshot, ExecutionResult, StaleReference


class InteractionTaskHandle(Handle):
    def __init__(self, core, host, identity):
        super().__init__(core, ExecutionRef("interaction", identity), owned=True)
        self.host = host

    def snapshot(self):
        task = self.host.state.get(self.id)
        state = task["state"]
        native = None
        if (task.get("target") or {}).get("kind") == "run":
            native = self.core.run_store.status(task["target"]["id"])
            state = native["status"]
        elif state == "finished":
            state = "completed"
        pending = (
            not task.get("cleanup_complete", False)
            if state in {"completed", "cancelled"}
            else state not in {"blocked", "paused", "finished"}
        )
        return ExecutionSnapshot(
            self.ref,
            state,
            task["revision"],
            task.get("error") or "",
            requests=self.requests(),
            output_available=task["state"] in {"finished", "cancelled"},
            persistence_confirmed=True,
            execution_pending=pending,
            cleanup={"complete": task.get("cleanup_complete", False)},
            details={"target": task.get("target"), "route": task.get("route")},
        )

    def result(self):
        self._require_result()
        task = self.host.state.get(self.id)
        return ExecutionResult(
            self.ref,
            self.snapshot().state,
            task.get("result", ""),
            error=task.get("error"),
        )

    def _active(self):
        task = self.host.state.active()
        if not task or task["id"] != self.id:
            raise StaleReference(
                "Interaction input target changed or ended.", ref=self.ref
            )

    @host_operation
    def steer(self, text, *, request_id=None):
        request_id = request_id or "sdk_" + uuid.uuid4().hex
        with self.host.lock, output_scope():
            self._active()
            return self.host.submit(
                text,
                mode="steer",
                input_id="usr_" + stable_id("interaction-input", self.id, request_id),
                literal=True,
            )

    @host_operation
    def interrupt(self, text, *, request_id=None):
        request_id = request_id or "sdk_" + uuid.uuid4().hex
        with self.host.lock, output_scope():
            self._active()
            return self.host.submit(
                text,
                mode="interrupt",
                input_id="usr_" + stable_id("interaction-input", self.id, request_id),
                literal=True,
            )

    @host_operation
    def pause(self):
        return self.host.pause(self.id)

    @host_operation
    def resume(self):
        return self.host.resume(self.id)

    @host_operation
    def cancel(self):
        return self.host.cancel(self.id)

    @host_operation
    def upgrade(self):
        with self.host.lock:
            self._active()
            self.core.config.require_reconstructable()
            return self.host.upgrade()

    @host_operation
    def detach(self):
        with self.host.lock:
            self._active()
            return self.host.detach()


class InteractionHandle(Handle):
    def __init__(self, core, host):
        super().__init__(
            core,
            ExecutionRef(
                "interaction_host",
                stable_id("interaction-host", str(core.workdir), "default"),
            ),
            owned=True,
        )
        self.host = host

    @host_operation
    def submit(self, text, *, request_id=None):
        request_id = request_id or "sdk_" + uuid.uuid4().hex
        with output_scope():
            receipt = self.host.submit(
                text,
                mode="queue",
                input_id="usr_"
                + stable_id("interaction-submit", str(self.core.workdir), request_id),
                literal=True,
            )
        return InteractionTaskHandle(self.core, self.host, receipt["task_id"])

    def get(self, identity):
        self.host.state.get(identity)
        return InteractionTaskHandle(self.core, self.host, identity)

    def snapshot(self):
        status = self.host.status()
        active = self.host.state.active()
        closed = self.host.shutdown.finished.is_set()
        return ExecutionSnapshot(
            self.ref,
            "closed"
            if closed
            else "closing"
            if self.host.closed
            else "running"
            if active
            else "idle",
            persistence_confirmed=closed,
            execution_pending=bool(active) or self.host.closed and not closed,
            cleanup={"closed": closed},
            details=status,
        )

    def close(self):
        with output_scope():
            return self.host.close()

    def wait_closed(self, timeout=5):
        return self.host.shutdown.finished.wait(timeout)

    def __enter__(self):
        return self

    def __exit__(self, kind, error, tb):
        self.close()
        self.core.finish_scope(self, kind)


class Interactions:
    def __init__(self, core):
        self.core, self.live = core, {}

    @host_operation
    def open(self, *, router=None):
        self.core.check_open()
        for handle in self.live.values():
            if not handle.host.closed:
                return handle
        with output_scope():
            builder = self.core.config.builder(self.core.workdir)
            original = builder.run_spec

            def run_spec(*args, **kw):
                self.core.config.require_reconstructable()
                return original(*args, **kw)

            builder.run_spec = run_spec
            host = InteractionHost(
                builder,
                trusted=True,
                router=router,
                announce=lambda _event: self.core.changed.set(),
            )
        handle = InteractionHandle(self.core, host)
        self.live[handle.id] = handle
        return handle

    def get(self, identity):
        if identity in self.live:
            return self.live[identity]
        raise StaleReference(
            "Interaction host is not attached; open the workspace host explicitly."
        )
