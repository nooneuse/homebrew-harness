"""Explicit offline host recovery; never a remote client's database fallback."""

from contextlib import ExitStack
from types import SimpleNamespace

from homebrew_harness.execution.execution_leases import ExecutionLeases
from homebrew_harness.services.spec import ServiceSpec
from homebrew_harness.services.store import ServiceStore
from homebrew_harness.services.protocol import request_key
from homebrew_harness.storage.storage_admin import maintenance
from .retention import business_fingerprint, roots


def rollback(control, identity, plan_id, request_id):
    store = ServiceStore(control)
    leases = ExecutionLeases(store.root)
    try:
        with leases.use("service"):
            meta = store.get(identity)
            key = "op_" + request_key("host-upgrade-rollback", identity, request_id)
            prior = store.runtime.get("service_operations", key)
            if prior:
                if prior["payload"] != {"id": plan_id}:
                    raise ValueError("Rollback request ID has another payload.")
                return prior
            plan = store.runtime.get("service_upgrade_plans", plan_id)
            if (
                not plan
                or plan["service_id"] != identity
                or plan["state"] not in {"armed", "probation"}
            ):
                raise ValueError("No unconfirmed upgrade can be rolled back.")
            if meta.get("operations_phase") != "parked":
                raise ValueError("Rollback needs an actually parked service.")
            spec = ServiceSpec(**meta["spec"])
            manager = SimpleNamespace(
                store=store,
                runtime=SimpleNamespace(id=identity, spec=spec),
                spec=spec.operations,
            )
            with ExitStack() as owned:
                for root in roots(manager).values():
                    if root == store.root:
                        continue
                    for resource in (
                        "service-workspace",
                        "managed-supervisor",
                        "managed-services",
                    ):
                        guard = ExecutionLeases(root)
                        owned.callback(guard.close)
                        owned.enter_context(guard.use(resource))
                if plan["business"] != business_fingerprint(manager):
                    raise ValueError("New business facts prevent upgrade rollback.")
                with maintenance(store.root), store.db.write() as conn:
                    current = store.db._get(conn, "service_meta", identity)
                    if current["generation"] != meta["generation"]:
                        raise ValueError("Service generation changed.")
                    current.update(
                        binding=plan["old_binding"],
                        state="stopped",
                        scope="all",
                        owner=None,
                        pid=None,
                        resources_closed=True,
                        revision=current["revision"] + 1,
                        launch={"phase": "ended", "token": None},
                    )
                    current.pop("upgrade_plan", None)
                    plan.update(state="rolled_back", revision=plan["revision"] + 1)
                    store.db._put(conn, "service_upgrade_plans", plan_id, plan)
                    store.db._put(conn, "service_meta", identity, current)
                    result = {
                        "id": key,
                        "kind": "upgrade_rollback",
                        "payload": {"id": plan_id},
                        "state": "completed",
                        "result": {
                            "old_runtime": plan["old_binding"]["runtime"],
                            "next": "Start the recorded old installed version; it remains parked.",
                        },
                        "service_id": identity,
                        "checkpoint": "service:" + identity,
                        "revision": 0,
                    }
                    store.db._put(conn, "service_operations", key, result)
                    return result
    finally:
        leases.close()
