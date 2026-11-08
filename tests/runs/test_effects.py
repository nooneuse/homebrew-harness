import os
from unittest.mock import patch

from tests.runs import test_human_control as support
from tests.support.run_services import RunServiceFixture
from homebrew_harness.runs import RunSpec
from homebrew_harness.runs.services import RunServices


class EffectTests(support.ManagedRunFixture):
    def setup_notification(self):
        server = RunServiceFixture("0" * 40)
        self.addCleanup(server.close)
        env = patch.dict(os.environ, {"HARNESS_NOTICE_URL": server.url + "notify"})
        env.start()
        self.addCleanup(env.stop)
        service = RunServices(
            self.store,
            {"version": 1, "notification": {"url_env": "HARNESS_NOTICE_URL"}},
        )
        self.addCleanup(service.close)
        run = self.store.create(RunSpec("Work"))
        service.bind(run["id"])
        self.run_id = run["id"]
        self.store.update(
            {"run_id": run["id"], "owner": None, "generation": 0},
            {"status": "blocked", "last_outcome": {"reason_code": "fixture_block"}},
        )
        return server, service

    def test_real_notification_is_minimal_and_does_not_repeat(self):
        server, service = self.setup_notification()
        service.tick(self.run_id)
        service.tick(self.run_id)
        self.assertEqual(len(server.notifications), 1)
        self.assertEqual(server.notifications[0]["path"], "/notify")
        self.assertEqual(
            set(server.notifications[0]["payload"]), {"run_id", "status", "reason_code"}
        )
        self.assertNotIn("Authorization", server.notifications[0]["headers"])
        effect = next(iter(self.store.runtime.items("run_outbox").values()))
        self.assertEqual(effect["state"], "delivered")
        self.assertEqual(
            server.notifications[0]["headers"]["Idempotency-Key"], effect["id"]
        )

    def test_unknown_remote_return_is_not_replayed_and_can_be_inspected(self):
        server, service = self.setup_notification()
        server.drop_notification = True
        service.tick(self.run_id)
        service.tick(self.run_id)
        effect = next(iter(self.store.runtime.items("run_outbox").values()))
        self.assertEqual(effect["state"], "unknown")
        self.assertEqual(len(server.notifications), 1)
        self.assertEqual(self.store.get(self.run_id)["status"], "blocked")
        with self.assertRaises(ValueError):
            service.resolve(
                self.run_id,
                effect["id"],
                effect["revision"] - 1,
                "delivered",
                "Observed remotely",
            )
        resolved = service.resolve(
            self.run_id,
            effect["id"],
            effect["revision"],
            "delivered",
            "Observed this exact notification remotely",
        )
        self.assertEqual(resolved["state"], "delivered")
        service.tick(self.run_id)
        self.assertEqual(len(server.notifications), 1)

    def test_orphaned_started_delivery_is_unknown_without_sending(self):
        server, service = self.setup_notification()
        identity, effect = next(iter(self.store.runtime.items("run_outbox").items()))
        self.store.runtime.put(
            "run_outbox",
            identity,
            {**effect, "state": "started", "owner": "old", "revision": 1},
        )
        service.tick(self.run_id)
        self.assertEqual(
            self.store.runtime.get("run_outbox", identity)["state"], "unknown"
        )
        self.assertFalse(server.notifications)

    def test_failed_notification_does_not_change_business_status(self):
        server, service = self.setup_notification()
        server.notification_status = 400
        service.tick(self.run_id)
        effect = next(iter(self.store.runtime.items("run_outbox").values()))
        self.assertEqual(effect["state"], "failed")
        self.assertEqual(
            self.store.get(self.run_id)["last_outcome"]["reason_code"], "fixture_block"
        )
