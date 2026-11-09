import asyncio
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path
from homebrew_harness import AsyncHarness, HostConfig
from tests.support.clients import fake_client, reply
from homebrew_harness.runtime.interaction_config import InteractionAssembler


class APIAsync(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_factory_is_still_owned_and_closed(self):
        entered, release = threading.Event(), threading.Event()
        client, _ = fake_client([])
        original = InteractionAssembler.session

        def prepare(builder, *args, **kw):
            entered.set()
            release.wait(5)
            return original(builder, *args, **kw)

        with tempfile.TemporaryDirectory() as root:
            host = AsyncHarness(
                workdir=Path(root),
                config=HostConfig.from_components(client=client, model="fixture"),
            )
            with patch.object(InteractionAssembler, "session", prepare):
                pending = asyncio.create_task(host.sessions.open())
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                pending.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await pending
                report = await host.close(0.01)
                self.assertFalse(report.resources_closed)
                release.set()
                self.assertEqual((await host.wait_closed(5)).state, "closed")
            self.assertEqual(len(host._sync.sessions.live), 1)
            self.assertTrue(
                next(iter(host._sync.sessions.live.values())).runtime.wait_closed(0)
            )

    async def test_cancelling_wait_does_not_cancel_the_underlying_turn(self):
        entered, release = threading.Event(), threading.Event()
        client, _ = fake_client([])

        def create(**_):
            entered.set()
            release.wait(5)
            return reply("done")

        client.chat.completions.create.side_effect = create
        with tempfile.TemporaryDirectory() as root:
            async with AsyncHarness(
                workdir=Path(root),
                config=HostConfig.from_components(client=client, model="fixture"),
            ) as host:
                session = await host.sessions.open()
                turn = await session.submit("Work", request_id="one")
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                waiter = asyncio.create_task(turn.wait())
                await asyncio.sleep(0.05)
                waiter.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await waiter
                self.assertEqual((await turn.snapshot()).state, "running")
                release.set()
                self.assertEqual((await turn.wait(5)).snapshot.state, "completed")
                self.assertEqual((await turn.result()).text, "done")

    async def test_async_execute_is_an_adapter_over_the_same_session(self):
        client, _ = fake_client([reply("first"), reply("second")])
        with tempfile.TemporaryDirectory() as root:
            async with AsyncHarness(
                workdir=Path(root),
                config=HostConfig.from_components(client=client, model="fixture"),
            ) as host:
                async with await host.sessions.open() as session:
                    report = await session.execute("First", timeout=5)
                    self.assertEqual(report.snapshot.state, "completed")
                    turn = await session.submit("Second")
                    events = [event async for event in turn.watch(interval=0.02)]
                    self.assertEqual(events[-1].snapshot.state, "completed")
