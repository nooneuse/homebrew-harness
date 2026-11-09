"""Explicit host maintenance delegates to existing recovery and storage guards."""

from .base import host_operation

from .types import ExecutionRef, UnsupportedOperation


class Admin:
    def __init__(self, core):
        self.core = core

    @host_operation
    def inspect(self, target, operation_id, revision):
        target = target.ref if hasattr(target, "ref") else target
        if not isinstance(target, ExecutionRef):
            raise ValueError("Expected execution reference.")
        if target.kind == "run":
            from homebrew_harness.execution.execution_leases import ExecutionLeases
            from homebrew_harness.runs.supervisor import RunSupervisor
            from homebrew_harness.runs.controller import RunController
            from homebrew_harness.runs.worker import make_session

            self.core.config.require_reconstructable()
            leases = ExecutionLeases(self.core.workdir)
            try:
                with leases.use("managed-supervisor"):
                    check = RunSupervisor(self.core.run_store)
                    value = self.core.run_store.get(target.id)
                    if not check.execution_free() or check.worker_alive(value):
                        raise ValueError("Stop live execution before inspection.")
                    value = self.core.run_store.claim(target.id, manage=True)
                    control = RunController(
                        self.core.run_store,
                        target.id,
                        value["owner"],
                        value["generation"],
                    )
                    control.inspection_mode = True
                    session, raw = make_session(
                        self.core.run_store, control, self.core.config.settings
                    )
                    try:
                        from homebrew_harness.runtime.host_commands import HostCommand
                        from homebrew_harness.runtime.output import output_scope

                        with output_scope():
                            return session.control(
                                HostCommand(
                                    "",
                                    "recovery",
                                    "inspect",
                                    mode="manage",
                                    values=(operation_id, revision),
                                )
                            )
                    finally:
                        session.close()
                        closed = session.wait_closed(control.spec.shutdown_seconds)
                        control.close_heartbeat()
                        self.core.run_store.ended(
                            control.header,
                            cleanup=closed
                            and session.shutdown_status()["persistence_ok"],
                        )
                        if raw and closed:
                            raw.close()
            finally:
                leases.close()
        if target.kind == "session":
            session = self.core.sessions.live.get(target.id)
            if session is None:
                raise UnsupportedOperation(
                    "Resume the owning Session before operation inspection.", ref=target
                )
            return session.runtime.inspect_operation(operation_id, revision)
        raise UnsupportedOperation(
            "This execution does not use Session operation inspection.", ref=target
        )

    @host_operation
    def export(self, target, output):
        target = target.ref if hasattr(target, "ref") else target
        from homebrew_harness.runs.maintenance import write_export

        if target.kind != "run":
            raise UnsupportedOperation("Run export requires a Run reference.")
        return write_export(self.core.run_store, target.id, output)

    @host_operation
    def backup(self, target, output):
        target = target.ref if hasattr(target, "ref") else target
        from homebrew_harness.runs.maintenance import backup_run

        if target.kind != "run":
            raise UnsupportedOperation("Run backup requires a Run reference.")
        return backup_run(self.core.run_store, target.id, output)
