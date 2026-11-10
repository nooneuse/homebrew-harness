"""Portable remote service view; all state is obtained through HTTP."""

import threading
from homebrew_harness.observation import Handle
from homebrew_harness.contracts import ExecutionRef, ExecutionSnapshot, SDKError
from .http_client import ControlClient, ControlConnection as ControlConnection
from .views import WorkService, ServiceControls


class RemoteContext:
    def __init__(self):
        self.changed = threading.Event()


class RemoteService(ServiceControls, Handle):
    def __init__(self, connection, *, control=False, own_client=None):
        borrowed = isinstance(connection, ControlClient)
        client = connection if borrowed else ControlClient(connection=connection)
        self.own_client = not borrowed if own_client is None else own_client
        try:
            credentials = client.credentials()
        except BaseException:
            if self.own_client:
                client.close()
            raise
        super().__init__(
            RemoteContext(), ExecutionRef("service", credentials.service_id)
        )
        self.client = client
        self.control = control
        self.work = WorkService(self)

    @classmethod
    def from_file(cls, path, *, control=False):
        return cls(ControlClient.from_file(path), control=control, own_client=True)

    @classmethod
    def from_root(cls, root, *, control=False):
        return cls(ControlClient(root), control=control, own_client=True)

    def query(self, method, payload=None):
        return self.client.call(self.id, method, payload)

    def command(self, method, payload, request_id=None):
        if not self.control:
            raise SDKError(
                "Use control=True before sending service controls.",
                code="not_owned",
                ref=self.ref,
            )
        return self.client.call(self.id, method, payload, request_id)

    def snapshot(self):
        value = self.query("snapshot")
        return ExecutionSnapshot(
            self.ref,
            value["state"],
            value["revision"],
            value.get("error") or "",
            persistence_confirmed=True,
            execution_pending=not value["resources_closed"],
            cleanup={
                "resources_closed": value["resources_closed"],
                "live_owner": value.get("live_owner", True),
            },
            details={
                key: value.get(key)
                for key in (
                    "generation",
                    "active_work",
                    "active_run",
                    "scope",
                    "heartbeat_at",
                    "work_counts",
                    "launch",
                )
            },
        )

    def close(self):
        if self.own_client:
            self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
