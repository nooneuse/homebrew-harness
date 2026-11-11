"""Service model faults remain visible, profile-bound and explicitly recoverable."""

import unittest
from unittest.mock import patch

from homebrew_harness.contracts import SDKError
from homebrew_harness.execution.model_gateway import ModelGatewayError
from homebrew_harness.execution.model_protocol import ProviderError
from tests.services.communication import test_boundaries as fixture


class ModelRecovery(unittest.TestCase):
    setUp = fixture.CommunicationBoundaries.setUp
    start = fixture.CommunicationBoundaries.start
    discussion = fixture.CommunicationBoundaries.discussion
    managed = fixture.CommunicationBoundaries.managed

    def broken(self):
        service, did, c = self.managed()
        actor = service.runtime.maintenance.agent
        gateway = actor.gateway
        arguments = {
            "model": actor.binding.model,
            "messages": [{"role": "user", "content": "Synthetic request."}],
            "max_tokens": 32,
        }
        with patch.object(
            gateway._transport(),
            "execute",
            side_effect=ProviderError("server_error", status_code=503),
        ) as call:
            for _ in range(3):
                with self.assertRaises(ProviderError):
                    gateway.request(**arguments)
            self.assertEqual(call.call_count, 3)
        actor.model_control.save()
        model = next(row for row in service.models() if row["blocked"])
        self.assertFalse(service.health()["ready"])
        return service, actor, arguments, model

    def test_fault_confirmation_recovers_without_retrying_work_or_resetting_usage(self):
        service, actor, arguments, model = self.broken()
        count = actor.gateway.summary()["calls"]
        result = service.recover_model(
            model["profile"], revision=model["revision"], request_id="recover"
        )
        self.assertFalse(result["jobs_retried"])
        self.assertFalse(actor.gateway.blocked)
        self.assertEqual(actor.gateway.summary()["calls"], count)
        actor.gateway.request(**arguments)
        self.assertFalse(actor.gateway.blocked)
        self.assertEqual(
            service.recover_model(
                model["profile"], revision=model["revision"], request_id="recover"
            ),
            result,
        )

    def test_stale_confirmation_keeps_faults(self):
        service, actor, arguments, model = self.broken()
        with self.assertRaises(SDKError):
            service.recover_model(model["profile"], revision="stale")
        self.assertTrue(actor.gateway.blocked)
        with self.assertRaises(ModelGatewayError):
            actor.gateway.request(**arguments)

    def test_saved_fault_survives_service_restart(self):
        service, actor, arguments, model = self.broken()
        identity = service.id
        service.stop()
        self.assertTrue(service.wait_closed(10))
        restarted = self.host.services.start(identity=identity)
        self.addCleanup(lambda: (restarted.close(), restarted.wait_closed(10)))
        restored = next(
            row for row in restarted.models() if row["profile"] == model["profile"]
        )
        self.assertTrue(restored["blocked"])
        self.assertEqual(restored["revision"], model["revision"])
        restarted.recover_model(restored["profile"], revision=restored["revision"])
        self.assertFalse(
            next(
                row
                for row in restarted.models()
                if row["profile"] == restored["profile"]
            )["blocked"]
        )
