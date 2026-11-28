import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from homebrew_harness import AsyncHarness, HostConfig, ServiceSpec
from tests.integration.test_native_model_protocols import ProtocolService
from tests.services.test_notifications import Webhook
from tests.support.clients import reply


class AsyncServices(unittest.IsolatedAsyncioTestCase):
    async def test_nested_work_service_is_awaitable_and_close_retains_native_owner(
        self,
    ):
        remote = ProtocolService([reply("Done").model_dump(exclude_none=True)])
        self.addCleanup(remote.close)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            control = base / "control"
            control.mkdir(mode=0o700)
            workspace = base / "workspace"
            workspace.mkdir()
            env = base / "model.env"
            env.write_text(
                "OPENAI_API_KEY=synthetic\nOPENAI_BASE_URL="
                + remote.url
                + "\nOPENAI_MODEL=fixture\n"
            )
            with patch.dict("os.environ", {}, clear=True):
                async with AsyncHarness(
                    workdir=control, config=HostConfig.from_files(env_file=env)
                ) as host:
                    service = await host.services.start(ServiceSpec(str(workspace)))
                    work = await service.work.submit("Work", request_id="one")
                    self.assertEqual((await work.wait(20)).snapshot.state, "review")
                    self.assertEqual((await work.result()).text, "Done")
                    inbox = await service.inbox(work.id, include_expired=True)
                    self.assertEqual(inbox["counts"]["total"], 0)
                    await service.stop()
                    self.assertTrue(await service.wait_closed(10))

    async def test_maintenance_refresh_expired_is_awaitable(self):
        from tests.services import test_maintenance as fixture

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        control = base / "control"
        control.mkdir(mode=0o700)
        workspace = base / "workspace"
        workspace.mkdir()
        peer = fixture.Peer()
        self.addCleanup(peer.close)
        env = base / "model.env"
        env.write_text(f"OPENAI_API_KEY=synthetic\nOPENAI_BASE_URL={peer.url}/v1\nOPENAI_MODEL=fixture\n")
        with patch.dict("os.environ", {}, clear=True):
            async with AsyncHarness(workdir=control, config=HostConfig.from_files(env_file=env)) as host:
                service = await host.services.start(ServiceSpec(
                    str(workspace), profile="repository_maintenance",
                    maintenance={"scopes": {"code": {"write_paths": ["src"]}}},
                ))
                result = await service.refresh_expired(ids=[], request_id="async-refresh")
                self.assertEqual(result, {
                    "request_id": "async-refresh", "refreshed": [], "skipped": [],
                    "has_more": False, "next_cursor": None,
                })
                await service.stop()
                self.assertTrue(await service.wait_closed(10))

    async def test_notification_status_and_manual_send_are_awaitable(self):
        webhook = Webhook()
        self.addCleanup(webhook.close)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            control = base / "control"
            control.mkdir(mode=0o700)
            workspace = base / "workspace"
            workspace.mkdir()
            env = base / "model.env"
            env.write_text(
                "OPENAI_API_KEY=synthetic\n"
                f"OPENAI_BASE_URL=http://127.0.0.1:{webhook.port}/v1\n"
                "OPENAI_MODEL=fixture\n"
                f"WEBHOOK_URL={webhook.url}\n"
            )
            env.chmod(0o600)
            with patch.dict("os.environ", {}, clear=True):
                async with AsyncHarness(
                    workdir=control, config=HostConfig.from_files(env_file=env)
                ) as host:
                    service = await host.services.start(ServiceSpec(
                        str(workspace), notifications={"url_env": "WEBHOOK_URL", "mode": "local"},
                    ))
                    status = await service.notification_status()
                    self.assertTrue(status["enabled"])
                    self.assertEqual(status["sent_count"], 0)
                    self.assertEqual(await service.send_notification(), {
                        "sent": False, "reason": "no_actionable_items",
                    })
                    self.assertEqual(webhook.calls, [])

                    options = {"force": True, "request_id": "async-notify"}
                    sent = await service.send_notification(**options)
                    self.assertTrue(sent["sent"])
                    self.assertEqual(await service.send_notification(**options), sent)
                    status = await service.notification_status()
                    self.assertEqual(status["sent_count"], 1)
                    self.assertEqual(status["last_delivery_id"], sent["delivery_id"])
                    self.assertEqual(len(webhook.calls), 1)
                    path, headers, payload = webhook.calls[0]
                    self.assertEqual(path, "/private/digest")
                    self.assertEqual(headers["Idempotency-Key"], sent["delivery_id"])
                    self.assertEqual(payload["event"], "service.inbox.digest")
                    self.assertEqual(payload["service_id"], service.id)
                    self.assertEqual(payload["trigger"], "manual")
                    await service.stop()
                    self.assertTrue(await service.wait_closed(10))
