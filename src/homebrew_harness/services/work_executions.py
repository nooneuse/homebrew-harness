"""Work-scoped adapters over the existing owned execution manager."""

import threading
from homebrew_harness.execution.operations import Operations
from homebrew_harness.execution.model_gateway import Deadline
from homebrew_harness.runtime.limits import current_limits
from homebrew_harness.runtime.execution_options import options


class WorkExecutions:
    def __init__(self, runtime):
        self.runtime = runtime
        self.managers = {}
        self.lock = threading.RLock()
        self.closing = False

    def manager(self, identity=None):
        if identity:
            self.runtime.store.work(identity, self.runtime.id)
        key = identity or self.runtime.id
        with self.lock:
            if key not in self.managers:
                manager = Operations(
                    self.runtime.store.root,
                    "service:" + self.runtime.id + ":work:" + key,
                    max_running=current_limits().execution_slots,
                )
                manager.recover()
                if self.closing:
                    manager.close(0)
                self.managers[key] = manager
            return self.managers[key]

    def invoke(
        self,
        identity,
        name,
        arguments,
        execute,
        *,
        request_id=None,
        execution=None,
        read_only=False,
        settled=None
    ):
        with self.lock:
            if self.closing:
                raise ValueError(
                    "Service executions are closing; retained observations remain available."
                )
        with self.runtime.store.db.connection() as conn:
            self.runtime.store.guard(conn, self.runtime.header)
        config = options(execution)
        manager = self.manager(identity)
        errors = []

        def run():
            try:
                return execute()
            except BaseException as error:
                errors.append(error)
                raise

        settings = {
            k: v
            for k, v in config.items()
            if k != "wait_seconds" or config["mode"] == "interactive"
        }
        handle = manager.start(
            name,
            arguments,
            run,
            execution=settings,
            request_id=request_id,
            deadline=Deadline(),
            read_only=read_only,
            settled=settled,
            resume_interrupted=name
            in {"work_continue", "work_apply_recovery", "work_observe_target"},
        )
        if config["mode"] == "one_shot":
            row = manager.wait_for(handle.id)
            result = row.get("result")
            if isinstance(result, dict) and result.get("error"):
                if errors:
                    raise errors[0]
                raise ValueError(result["error"])
            return result
        row = manager.wait_for(handle.id, config["wait_seconds"])
        return {
            "accepted": True,
            "execution_id": handle.id,
            "state": row["state"],
            "execution_pending": row["execution_pending"],
            "work_id": identity,
            "result": row.get("result") if not row["execution_pending"] else None,
        }

    def identity(self, identity, request_id):
        import hashlib

        return (
            "exec_"
            + hashlib.sha256(
                (self.manager(identity).owner + ":" + request_id).encode()
            ).hexdigest()[:32]
        )

    def describe(self, handle):
        value = handle.snapshot().data()
        details = value.get("details") or {}
        details = {
            k: v
            for k, v in details.items()
            if k not in {"output", "result", "arguments", "controls", "agent_inputs"}
        }
        value["details"] = details
        return value

    def page(self, identity, cursor=0, limit=20):
        if (
            type(cursor) is not int
            or cursor < 0
            or type(limit) is not int
            or not 1 <= limit <= current_limits().service_page_rows
        ):
            raise ValueError("Invalid execution page.")
        handles = self.handles(identity)
        return {
            "executions": [self.describe(h) for h in handles[cursor : cursor + limit]],
            "next_cursor": cursor + limit if cursor + limit < len(handles) else None,
            "total_executions": len(handles),
        }

    def handles(self, identity):
        manager = self.manager(identity)
        with manager.lock:
            result = [manager.handle(v["id"]) for v in manager.rows.values()]
        if identity is None:
            return result
        work = self.runtime.store.work(identity, self.runtime.id)
        for run_id in [work.get("run_id"), *work.get("run_history", [])]:
            if not run_id:
                continue
            location = work.get("run_locations", {}).get(run_id)
            if not location and run_id != work.get("run_id"):
                continue
            host = self.runtime.child_for(
                {**work, **({"run_workspace": location} if location else {})}
            )
            try:
                run = host.runs.get(run_id, control=True)
                result.extend(
                    run.executions.get(row.ref.id) for row in run.executions.list()
                )
            except ValueError:
                continue
        return result

    def get(self, identity, execution_id):
        handle = next((v for v in self.handles(identity) if v.id == execution_id), None)
        if handle is None:
            raise ValueError("Execution is outside this Work.")
        return handle

    def manager_snapshot(self):
        with self.lock:
            return list(self.managers.values())

    def active(self, name):
        for manager in self.manager_snapshot():
            with manager.lock:
                if any(
                    row.get("name") == name
                    and row["execution_pending"]
                    and row["phase"] != "cleanup"
                    for row in manager.rows.values()
                ):
                    return True
        return False

    def request_close(self):
        with self.lock:
            self.closing = True
            managers = list(self.managers.values())
        for manager in managers:
            manager.close(0)

    def wait_closed(self, seconds=1):
        return all(manager.wait_closed(seconds) for manager in self.manager_snapshot())


def dispatch(runtime, method, payload, request_id):
    service_scope = method.startswith("service_")
    identity = None if service_scope else payload["id"]
    work = runtime.store.work(identity, runtime.id) if identity else None
    name = method.replace("service_", "work_", 1) if service_scope else method
    if name == "work_executions":
        return runtime.work_executions.page(
            identity, payload.get("cursor", 0), payload.get("limit", 20)
        )
    execution = runtime.work_executions.get(identity, payload["execution_id"])
    if name == "work_execution_get":
        return runtime.work_executions.describe(execution)
    if name == "work_execution_wait":
        seconds = payload.get("seconds", 10)
        from homebrew_harness.runtime.limits import positive

        positive(seconds, "observation seconds", zero=True)
        execution.wait(seconds)
        return runtime.work_executions.describe(execution)
    if name == "work_execution_output":
        return execution.output(
            payload.get("cursor", 0),
            payload.get("limit", 16000),
            stream=payload.get("stream", "output"),
        )
    with runtime.lock:
        with runtime.store.db.connection() as conn:
            runtime.store.guard(conn, runtime.header)
        if (
            name == "work_execution_input"
            and runtime.store.get(runtime.id).get("operations_phase", "active")
            != "active"
        ):
            raise ValueError(
                "Service is parked or draining; settle its operations before new input."
            )
        if work and runtime.store.work(identity, runtime.id)["revision"] != payload.get(
            "revision"
        ):
            raise ValueError("Work revision changed.")
        if name == "work_execution_input":
            return execution.input(payload["text"], request_id=request_id)
        if name == "work_execution_stop":
            return execution.stop(
                revision=payload.get("execution_revision"), request_id=request_id
            )
        raise ValueError("Unknown execution control.")
