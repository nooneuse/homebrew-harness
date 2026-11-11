"""Profile-bound Service gateways and explicit, persistent fault recovery."""

import threading
from homebrew_harness.execution.model_gateway import ModelGateway, GatewayLimits
from .protocol import fingerprint, request_key


class ServiceModels:
    def __init__(self, agent):
        self.agent = agent
        self.lock = threading.RLock()
        self.contexts = {
            agent.models.alias or "@default": (agent.binding, agent.gateway)
        }

    def add(self, alias, binding):
        gateway = ModelGateway(
            binding.client,
            GatewayLimits(
                request_timeout=self.agent.runtime.core.config.settings.request_seconds
            ),
            workdir=self.agent.runtime.store.root,
        )
        self.agent.models.track_gateway(gateway)
        self.contexts[alias] = (binding, gateway)

    def load(self):
        for alias, (binding, gateway) in self.contexts.items():
            value = self.agent.store.store.runtime.get(
                "service_model_faults", self.key(alias)
            )
            if value and value["binding"] == fingerprint(binding.identity()):
                gateway.restore_acknowledged_faults(value["faults"])

    def key(self, alias):
        return self.agent.store.id + ":" + alias

    def save(self, conn=None):
        if conn is None:
            with self.agent.store.db.write() as connection:
                self.agent.store.store.guard(connection, self.agent.runtime.header)
                return self.save(connection)
        for alias, (binding, gateway) in self.contexts.items():
            faults = gateway.summary()["connection_faults"]
            value = {
                "binding": fingerprint(binding.identity()),
                "faults": faults,
                "service_id": self.agent.store.id,
                "checkpoint": "service:" + self.agent.store.id,
            }
            if (
                self.agent.store.db._get(conn, "service_model_faults", self.key(alias))
                != value
            ):
                self.agent.store.db._put(
                    conn, "service_model_faults", self.key(alias), value
                )

    def view(self):
        return [
            {
                "profile": alias,
                "blocked": gateway.blocked,
                "revision": fingerprint(gateway.summary()["connection_faults"]),
                "inflight": gateway.summary()["inflight"],
            }
            for alias, (_binding, gateway) in sorted(self.contexts.items())
        ]

    def recover(self, profile, revision, request_id):
        if profile not in self.contexts:
            raise ValueError("Unknown Service model profile.")
        if not self.lock.acquire(blocking=False):
            raise ValueError(
                "Wait for the active model job before recovering its connection."
            )
        try:
            gateway = self.contexts[profile][1]
            runtime = self.agent.runtime
            faults = gateway.summary()["connection_faults"]

            def apply(conn):
                meta = runtime.store.guard(conn, runtime.header)
                current = gateway.summary()
                if meta["state"] in {"stopping", "stopped"}:
                    raise ValueError("Service model recovery requires a live service.")
                if (
                    fingerprint(current["connection_faults"]) != revision
                    or current["inflight"]
                ):
                    raise ValueError(
                        "Model fault changed or execution is active; refresh its reference."
                    )
                binding = self.contexts[profile][0]
                self.agent.store.db._put(
                    conn,
                    "service_model_faults",
                    self.key(profile),
                    {
                        "binding": fingerprint(binding.identity()),
                        "faults": {},
                        "service_id": runtime.id,
                        "checkpoint": "service:" + runtime.id,
                    },
                )
                return {"profile": profile, "recovered": True, "jobs_retried": False}

            payload = {"profile": profile, "revision": revision}
            try:
                result = runtime.store.command(
                    runtime.header, "model_recover", payload, request_id, apply
                )
            except BaseException:
                saved = runtime.store.runtime.get(
                    "service_commands", request_key("command", runtime.id, request_id)
                )
                if not saved or saved["payload"] != fingerprint(
                    ["model_recover", payload]
                ):
                    raise
                result = saved["result"]
            # Replaying an old recovery cannot erase faults from a newer request.
            if fingerprint(gateway.summary()["connection_faults"]) == revision:
                gateway.acknowledge_faults(faults)
            return result
        finally:
            self.lock.release()

    def select(self, job):
        spec = self.agent.runtime.spec
        alias = {
            "audit": spec.operations.get("audit_model"),
            "review": (spec.delivery or {}).get("review_model"),
        }.get(job["kind"])
        if job["kind"] == "discussion":
            alias = (
                (
                    spec.communication.get("review_model")
                    or spec.communication.get("model_profile")
                )
                if job["input"].get("review_id")
                else spec.communication.get("model_profile")
            )
        return self.contexts[alias or self.agent.models.alias or "@default"]

    def close(self):
        for _binding, gateway in self.contexts.values():
            gateway.close()
