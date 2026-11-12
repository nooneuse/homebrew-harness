"""Real CLI workers forward host approval policy and consume original requests."""

import json
import subprocess
import sys
import unittest

from homebrew_harness.runs import RunStore, RunSpec
from homebrew_harness.runs.spec import DEFAULT_TOOLS
from homebrew_harness.runs.requests import HumanRequests
from tests.integration import test_native_model_protocols as helpers
from tests.support.clients import reply, call


class ApprovalCLITests(unittest.TestCase):
    setUp = helpers.NativeProtocolTests.setUp
    service = helpers.NativeProtocolTests.service

    def cli(self, *args):
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "homebrew_harness",
                "--cwd",
                str(self.workspace),
                "--env-file",
                str(self.env),
                *args,
            ],
            capture_output=True,
            text=True,
            timeout=25,
        )

    def setup_model(self):
        approved = json.dumps(
            {
                "decision": "approve",
                "reason": "Exact user command",
                "risk": "low",
                "authorization": "explicit",
            }
        )
        server = self.service(
            [
                reply(calls=[call("ask", command="printf ok")]).model_dump(
                    exclude_none=True
                ),
                reply(approved).model_dump(exclude_none=True),
            ]
        )
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.env = self.root / "host.env"
        self.env.write_text(
            "OPENAI_API_KEY=synthetic-credential\nOPENAI_MODEL=fixture\nOPENAI_BASE_URL="
            + server.url
            + "\nHARNESS_MODEL_TRANSPORT=http\n"
        )
        return server

    def test_session_cli_uses_auto_review(self):
        server = self.setup_model()
        server.responses.append(reply("Done").model_dump(exclude_none=True))
        result = self.cli(
            "session", "--approval-review", '{"mode":"auto_review"}', "Print ok"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Done", result.stdout)
        self.assertEqual(len(server.requests), 3)
        self.assertIn(
            "independent approval reviewer", str(server.requests[1]["messages"])
        )

    def test_run_cli_passes_host_policy_into_recreated_worker(self):
        server = self.setup_model()
        spec = RunSpec(
            "Print ok",
            tools=(*DEFAULT_TOOLS, "bash"),
            total_seconds=60,
            request_seconds=5,
            episode_model_calls=3,
        )
        path = self.root / "run.json"
        path.write_text(json.dumps(spec.data()))

        def execute(_payload):
            store = RunStore(self.workspace)
            try:
                state = next(iter(store.runtime.items("runs").values()))
                request = HumanRequests(store).records(state["id"])[0]
                self.assertEqual(request["state"], "approved")
                return reply(
                    calls=[
                        call(
                            "accepted",
                            name="execute_approved",
                            arguments=json.dumps({"request_id": request["id"]}),
                        )
                    ]
                ).model_dump(exclude_none=True)
            finally:
                store.close()

        server.responses.extend([execute, reply("Done").model_dump(exclude_none=True)])
        first = self.cli(
            "run",
            "--approval-review",
            '{"mode":"auto_review"}',
            "start",
            "--spec",
            str(path),
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        store = RunStore(self.workspace)
        self.addCleanup(store.close)
        state = next(iter(store.runtime.items("runs").values()))
        request = HumanRequests(store).records(state["id"])[0]
        self.assertEqual(request["state"], "consumed")
        self.assertGreaterEqual(state["generation"], 2)
        self.assertEqual(len(server.requests), 4)
