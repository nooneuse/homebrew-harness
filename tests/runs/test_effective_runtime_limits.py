"""The accepted Run limits, not the launching host's defaults, drive the worker."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from homebrew_harness import Harness, HostConfig
from homebrew_harness.cli import main as cli_main
from homebrew_harness.runs import RunSpec, RunStore
from homebrew_harness.runs.supervisor import RunSupervisor
from homebrew_harness.runtime.limits import current_limits, limits_scope
from homebrew_harness.runtime.task_routing import TaskRoute
from tests.integration.test_native_model_protocols import ProtocolService
from tests.runtime.test_interaction import eventually
from tests.support.clients import call, fake_client, reply


class EffectiveRunLimits(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "workspace"
        self.root.mkdir()

    def test_automatic_run_persists_selected_host_limits_not_the_ambient_scope(self):
        path = self.base / "interaction.json"
        path.write_text(json.dumps({
            "runtime_limits": {"run_prompt_chars": 40, "file_read_chars": 2400},
        }))
        client, _ = fake_client([])
        file_config = HostConfig.from_file(path, workdir=self.root)
        config = HostConfig.from_components(
            client=client, model="fixture", runtime_limits=file_config.settings.runtime_limits,
        )
        route = TaskRoute(execution_mode="managed")
        with limits_scope({"run_prompt_chars": 100, "file_read_chars": 5000}):
            builder = config.builder(self.root)
            self.addCleanup(builder.close)
            with self.assertRaisesRegex(ValueError, "Run prompt exceeds"):
                builder.run_spec(route, "Long task " * 5)
            spec = builder.run_spec(route, "Read the source.")
            store = RunStore(self.root)
            self.addCleanup(store.close)
            accepted = store.create(spec)
            self.assertEqual(accepted["spec"]["runtime_limits"], config.settings.runtime_limits)
            self.assertEqual(current_limits().file_read_chars, 5000)
        client.chat.completions.create.assert_not_called()

    def test_run_cli_defaults_apply_only_when_the_spec_omits_limits(self):
        def capture(supervisor, identity):
            accepted.append(supervisor.store.get(identity)["spec"]["runtime_limits"])
            return {"status": "queued"}

        accepted = []
        path = self.base / "run.json"
        for explicit in (False, True):
            workspace = self.root / ("explicit" if explicit else "inherited")
            workspace.mkdir()
            declaration = {"prompt": "Read the source."}
            if explicit:
                declaration.update(runtime_limits={"goal_condition_chars": 80}, goal={
                    "condition": "The source file exists.",
                    "judge": False,
                    "predicates": [{"id": "source", "type": "file_exists", "path": "source.txt"}],
                })
            path.write_text(json.dumps(declaration))
            with patch.object(RunSupervisor, "supervise", capture), redirect_stdout(io.StringIO()):
                self.assertEqual(cli_main([
                    "run", "--cwd", str(workspace), "--runtime-limits",
                    '{"file_read_chars": 5000, "goal_condition_chars": 10}',
                    "start", "--spec", str(path),
                ]), 2)
        self.assertEqual(accepted[0]["file_read_chars"], 5000)
        self.assertEqual(accepted[1], RunSpec("Work", runtime_limits={"goal_condition_chars": 80}).runtime_limits)

    def test_sdk_worker_restores_checkpoint_with_saved_limits_under_a_new_host(self):
        def read(identity):
            return reply(calls=[call(identity, name="read_file", arguments='{"path":"source.txt"}')])

        responses = [
            read("before"),
            reply(calls=[call("question", name="request_attention", arguments=json.dumps({
                "question": "Continue?", "reason": "Need host input", "impact": "Completion",
            }))]),
            read("after"),
            reply("done"),
        ]
        server = ProtocolService([response.model_dump(exclude_none=True) for response in responses])
        self.addCleanup(server.close)
        env = self.base / "model.env"
        env.write_text(f"OPENAI_API_KEY=synthetic\nOPENAI_BASE_URL={server.url}\nOPENAI_MODEL=fixture\n")
        (self.root / "source.txt").write_text("z" * 4000)
        config = HostConfig.from_files(
            env_file=env, shutdown_seconds=5, runtime_limits={"file_read_chars": 5000},
        )
        spec = RunSpec(
            "Read the file and ask for host input before finishing.",
            tools=("read_file",),
            goal={
                "condition": "The source file exists.",
                "judge": False,
                "predicates": [{"id": "source", "type": "file_exists", "path": "source.txt"}],
            },
            runtime_limits={"file_read_chars": 1200, "goal_condition_chars": 80},
            total_seconds=45, request_seconds=5, shutdown_seconds=5,
            heartbeat_seconds=0.1, heartbeat_timeout=10,
        )

        with patch.dict("os.environ", {}, clear=True):
            with Harness(workdir=self.root, config=config) as host:
                job = host.runs.start(spec)
                waiting = eventually(lambda: self.settled_state(host, job, "waiting"), 15)
                saved = host.run_store.runtime.get("checkpoints", waiting["checkpoint"])
                self.assertEqual(saved["managed_run"]["run_id"], job.id)
                self.assert_page_limit(server.requests[1], "before", 1200)
                job.pause()
                self.assertTrue(job.wait_closed(10))
                self.assertEqual(job.snapshot().state, "paused")

            reopened_config = replace(config, settings=replace(
                config.settings, runtime_limits={"file_read_chars": 8000, "goal_condition_chars": 10},
            ))
            with Harness(workdir=self.root, config=reopened_config) as reopened:
                resumed = reopened.runs.get(job.id, control=True)
                resumed.steer("Continue", request_id="continue")
                resumed.resume()
                snapshot = resumed.wait(15, until="settled").snapshot
                self.assertEqual(snapshot.state, "completed", snapshot)
                completed = reopened.run_store.get(job.id)
                self.assertGreater(completed["generation"], waiting["generation"])
                self.assertEqual(completed["checkpoint"], waiting["checkpoint"])
                self.assertEqual(completed["spec"]["runtime_limits"], spec.runtime_limits)
                self.assert_page_limit(server.requests[-1], "after", 1200)

    def settled_state(self, host, job, status):
        value = host.run_store.get(job.id)
        if value["status"] == "blocked":
            self.fail(str(job.snapshot()))
        return value if value["status"] == status and value["cleanup_complete"] else None

    def assert_page_limit(self, request, identity, characters):
        result = next(message["content"] for message in request["messages"]
                      if message.get("tool_call_id") == identity)
        self.assertEqual(len(result), characters)
        self.assertTrue(json.loads(result)["truncated"])
