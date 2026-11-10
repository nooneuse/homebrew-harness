import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from homebrew_harness import AsyncHarness, HostConfig, ServiceSpec
from tests.integration.test_native_model_protocols import ProtocolService
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
                    await service.stop()
                    self.assertTrue(await service.wait_closed(10))
