"""Recovery, actual concurrency, bounded usage and existing harness boundaries."""

from __future__ import annotations
from tests.support.paths import PROJECT_ROOT
from homebrew_harness.runtime.capabilities import Capabilities

import asyncio
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from tests.support.tools import execute_fixture_tool
import homebrew_harness.tools.catalog
import homebrew_harness.tools.files
from homebrew_harness.runtime.session import HarnessSession
from homebrew_harness.runtime.hooks import HookRegistry
from tests.support.clients import call, fake_client, reply
from homebrew_harness.orchestration.workflow_runtime import (
    ModelWorkflowRunner,
    RunnerOutput,
    WorkflowError,
    WorkflowLimits,
    WorkflowRegistry,
    WorkflowRuntime,
    WorkflowSpec,
    WorkflowStore,
    schema_validator,
)
from homebrew_harness.orchestration.workflows import (
    DEMO_CHANGES,
    DemoWorkflowRunner,
    default_registry,
)


EMPTY_ARGS = {"type": "object", "additionalProperties": False}
VALUE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["value"],
    "properties": {"value": {"type": "integer"}},
}


class Runner:
    identity = "test-runner-v1"

    def __init__(self, action=None):
        self.calls = []
        self.action = action or (
            lambda prompt, schema, label, cap: RunnerOutput(prompt)
        )

    def run(self, prompt, schema, label, cap):
        self.calls.append((prompt, schema, label, cap))
        return self.action(prompt, schema, label, cap)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for capture in (redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            capture.__enter__()
            self.addCleanup(capture.__exit__, None, None, None)

    def runtime(self, script, runner=None, **kwargs):
        return WorkflowRuntime(
            self.root,
            runner or Runner(),
            WorkflowRegistry(
                [WorkflowSpec("test-flow", "Test workflow", script, EMPTY_ARGS)]
            ),
            **kwargs,
        )

    def files(self, run_id):
        root = self.root / ".harness/workflows"
        return {
            name: (root / (run_id + name)).read_bytes()
            for name in (".json", ".output.json", ".journal.jsonl")
        }

    def test_real_review_script_and_fresh_runtime_resume(self):
        runtime = WorkflowRuntime(
            self.root,
            DemoWorkflowRunner(),
            registry=__import__(
                "homebrew_harness.orchestration.workflows", fromlist=["*"]
            ).default_registry(),
        )
        first = runtime.run("review-changes", {"changes": DEMO_CHANGES})
        resumed = WorkflowRuntime(
            self.root,
            DemoWorkflowRunner(),
            registry=__import__(
                "homebrew_harness.orchestration.workflows", fromlist=["*"]
            ).default_registry(),
        ).run("review-changes", resume_from_run_id=first["task"]["run_id"])
        self.assertEqual(first["task"]["status"], "completed")
        self.assertEqual(resumed["result"], first["result"])
        self.assertEqual(resumed["task"]["progress"], {"calls": 0, "cached_steps": 3})
        self.assertEqual(len(first["result"]["confirmed"]), 1)
        for suffix in (".json", ".journal.jsonl", ".output.json", ".lock"):
            self.assertEqual(
                os.stat(runtime.store.path(first["task"]["run_id"], suffix)).st_mode
                & 0o777,
                0o600,
            )

    def test_cli_resume_in_another_process_uses_only_saved_results(self):
        script = PROJECT_ROOT / "homebrew_harness.workflow_cli.py"

        def cli(*arguments):
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "homebrew_harness.workflow_cli",
                    "--cwd",
                    str(self.root),
                    *arguments,
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            return json.loads(result.stdout)

        demo = cli("demo")
        run_id = demo["first"]["task"]["run_id"]
        resumed = cli("resume", run_id, "--demo")
        self.assertEqual(resumed["task"]["progress"]["calls"], 0)
        self.assertNotIn("tokens", resumed["task"]["progress"])
        self.assertEqual(cli("status", run_id)["task"]["status"], "completed")

    def test_metadata_and_model_arguments_fail_before_launch(self):
        async def script(ctx, args):
            self.fail("Invalid workflow launched")

        for name in ("../escape", "nested/path", "", "."):
            with self.assertRaises(WorkflowError):
                WorkflowRegistry([WorkflowSpec(name, "Invalid", script, EMPTY_ARGS)])
        runtime = self.runtime(script)
        for name, arguments in (
            ("missing", {}),
            ("test-flow", {"code": "raise SystemExit"}),
            ("test-flow", []),
        ):
            with self.assertRaises(WorkflowError):
                runtime.run(name, arguments)
        self.assertFalse((self.root / ".harness").exists())

    def test_denied_workflow_never_runs(self):
        async def script(ctx, args):
            self.fail("Denied workflow launched")

        registry = WorkflowRegistry(
            [WorkflowSpec("denied", "Denied", script, EMPTY_ARGS, allowed=False)]
        )
        with self.assertRaisesRegex(WorkflowError, "denied"):
            WorkflowRuntime(self.root, Runner(), registry).run("denied", {})
        self.assertFalse((self.root / ".harness").exists())

    def test_parallel_failure_drains_successful_peer_and_resumes_it(self):
        failed, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        healthy = False

        def action(prompt, schema, label, cap):
            if label == "bad" and not healthy:
                failed.set()
                raise RuntimeError("provider-private-detail")
            if label == "good":
                release.wait(3)
            return RunnerOutput(label)

        runner, events = Runner(action), []

        async def script(ctx, args):
            return await ctx.parallel(
                [
                    lambda: ctx.agent("bad", label="bad"),
                    lambda: ctx.agent("good", label="good"),
                ]
            )

        runtime = self.runtime(
            script,
            runner,
            on_event=lambda event, **fields: events.append((event, fields)),
        )

        async def scene():
            pending = asyncio.create_task(runtime.arun("test-flow", {}))
            self.assertTrue(await asyncio.to_thread(failed.wait, 2))
            await asyncio.sleep(0)
            self.assertFalse(pending.done())
            self.assertFalse(
                any(event == "task_notification" for event, fields in events)
            )
            release.set()
            return await pending

        first = asyncio.run(scene())
        self.assertEqual(first["task"]["status"], "failed")
        self.assertNotIn("provider-private-detail", json.dumps(first))
        self.assertEqual(events[-1][0], "task_notification")
        healthy = True
        before = len(runner.calls)
        resumed = self.runtime(script, runner).run(
            "test-flow", resume_from_run_id=first["task"]["run_id"]
        )
        self.assertEqual(len(runner.calls) - before, 1)
        self.assertEqual(resumed["task"]["progress"]["cached_steps"], 1)
        self.assertEqual(resumed["result"], ["bad", "good"])

    def test_changed_step_and_its_downstream_rerun_only(self):
        runner = Runner()

        def make_script(changed):
            async def script(ctx, args):
                a = await ctx.agent("fixed", label="a")
                b = await ctx.agent(changed, label="b")
                return await ctx.agent(a + b, label="combine")

            return script

        first = self.runtime(make_script("old"), runner).run("test-flow", {})
        resumed = self.runtime(make_script("new"), runner).run(
            "test-flow", resume_from_run_id=first["task"]["run_id"]
        )
        self.assertEqual(resumed["task"]["progress"]["calls"], 2)
        self.assertEqual(resumed["task"]["progress"]["cached_steps"], 1)
        self.assertEqual(resumed["result"], "fixednew")

    def test_duplicate_concurrent_steps_share_one_request_and_return_copies(self):
        runner = Runner(lambda *args: RunnerOutput({"value": 1}))

        async def script(ctx, args):
            values = await ctx.parallel(
                [
                    lambda: ctx.agent("same", schema=VALUE_SCHEMA),
                    lambda: ctx.agent("same", schema=VALUE_SCHEMA),
                ]
            )
            values[0]["value"] = 5
            return values

        result = self.runtime(script, runner).run("test-flow", {})
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(result["result"], [{"value": 5}, {"value": 1}])

    def test_pipeline_advances_fast_item_without_waiting_for_slow_item(self):
        reached, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def action(prompt, schema, label, cap):
            if label == "slow:first":
                self.assertTrue(release.wait(3))
            if label == "fast:second":
                reached.set()
                release.set()
            return RunnerOutput(label)

        async def script(ctx, args):
            async def first(value, item, index):
                return await ctx.agent(item, label=item + ":first")

            async def second(value, item, index):
                return await ctx.agent(value, label=item + ":second")

            return await ctx.pipeline(["slow", "fast"], first, second)

        result = self.runtime(script, Runner(action)).run("test-flow", {})
        self.assertTrue(reached.is_set())
        self.assertEqual(result["result"], ["slow:second", "fast:second"])

    def test_schema_retry_stays_in_concurrency_cap_and_accounts_all_usage(self):
        calls, active, peak = {}, 0, 0
        mutex = threading.Lock()
        barrier = threading.Barrier(2)

        def action(prompt, schema, label, cap):
            nonlocal active, peak
            with mutex:
                active += 1
                peak = max(peak, active)
                calls[label] = calls.get(label, 0) + 1
                number = calls[label]
            barrier.wait(timeout=3)
            with mutex:
                active -= 1
            return RunnerOutput({"value": "invalid"} if number == 1 else {"value": 1})

        async def script(ctx, args):
            return await ctx.parallel(
                [
                    lambda index=index: ctx.agent(
                        "value", schema=VALUE_SCHEMA, label=str(index)
                    )
                    for index in range(4)
                ]
            )

        result = self.runtime(
            script, Runner(action), limits=WorkflowLimits(concurrency=2)
        ).run("test-flow", {})
        self.assertEqual(result["task"]["status"], "completed")
        self.assertEqual(result["task"]["progress"]["calls"], 8)
        self.assertNotIn("tokens", result["task"]["progress"])
        self.assertEqual(peak, 2)

    def test_two_invalid_outputs_fail_without_unbounded_retry(self):
        runner = Runner(lambda *args: RunnerOutput({"value": "bad"}))

        async def script(ctx, args):
            return await ctx.agent("one", schema=VALUE_SCHEMA)

        out = self.runtime(script, runner).run("test-flow", {})
        self.assertEqual(out["task"]["status"], "failed")
        self.assertEqual(out["task"]["progress"]["calls"], 2)
        self.assertNotIn("tokens", out["task"]["progress"])

    def test_parallel_requests_are_not_serialized_by_token_cost(self):
        release = threading.Event()
        self.addCleanup(release.set)
        entered = threading.Barrier(3)

        def action(*args):
            entered.wait(3)
            release.wait(3)
            return RunnerOutput("done")

        runner = Runner(action)

        async def script(ctx, args):
            return await ctx.parallel(
                [
                    lambda: ctx.agent("one", label="one"),
                    lambda: ctx.agent("two", label="two"),
                ]
            )

        runtime = self.runtime(
            script, runner, limits=WorkflowLimits(concurrency=2, max_output_tokens=100)
        )

        async def scene():
            pending = asyncio.create_task(runtime.arun("test-flow", {}))
            await asyncio.to_thread(entered.wait, 3)
            self.assertEqual(len(runner.calls), 2)
            release.set()
            return await pending

        out = asyncio.run(scene())
        self.assertEqual(out["task"]["status"], "completed")
        self.assertEqual(out["task"]["progress"]["calls"], 2)

    def test_resume_reuses_results_without_a_cost_budget(self):
        runner = Runner()

        async def script(ctx, args):
            await ctx.agent("one", label="one")
            return await ctx.agent("two", label="two")

        first = self.runtime(script, runner).run("test-flow", {})
        self.assertEqual(first["task"]["status"], "completed")
        resumed = self.runtime(script, runner).run(
            "test-flow", resume_from_run_id=first["task"]["run_id"]
        )
        self.assertEqual(resumed["task"]["progress"]["calls"], 0)
        self.assertEqual(resumed["task"]["durable_steps"], 2)
        self.assertEqual(resumed["task"]["progress"]["cached_steps"], 2)

    def test_custom_runner_needs_only_output_and_completeness(self):
        async def script(ctx, args):
            await ctx.agent("one", label="one")
            return await ctx.agent("two", label="two")

        runner = Runner(lambda *args: RunnerOutput("saved"))
        first = self.runtime(
            script, runner, limits=WorkflowLimits(max_output_tokens=100)
        ).run("test-flow", {})
        self.assertEqual(first["task"]["status"], "completed")
        self.assertEqual(
            first["task"]["model_usage_source"], "external_runner_unobserved"
        )
        self.assertEqual(len(runner.calls), 2)

    def test_corrupt_journal_and_invalid_resume_preserve_success_artifacts(self):
        async def script(ctx, args):
            return await ctx.agent("saved")

        runtime = self.runtime(script)
        first = runtime.run("test-flow", {})
        run_id = first["task"]["run_id"]
        baseline = self.files(run_id)
        for inputs in ({"changed": True}, []):
            with self.assertRaises(WorkflowError):
                runtime.run("test-flow", inputs, run_id)
            self.assertEqual(self.files(run_id), baseline)
        different = Runner()
        different.identity = "different-model"
        with self.assertRaises(WorkflowError):
            self.runtime(script, different).run("test-flow", resume_from_run_id=run_id)
        self.assertEqual(self.files(run_id), baseline)
        journal = runtime.store.path(run_id, ".journal.jsonl")
        rows = [json.loads(line) for line in journal.read_text().splitlines()]
        rows[-1]["value"] = 17
        journal.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        corrupted = self.files(run_id)
        with self.assertRaises(WorkflowError):
            runtime.run("test-flow", resume_from_run_id=run_id)
        self.assertEqual(self.files(run_id), corrupted)

    def test_journal_contains_only_durable_steps_and_rejects_old_ledger_records(self):
        async def script(ctx, args):
            return await ctx.agent("saved")

        runtime = self.runtime(script)
        first = runtime.run("test-flow", {})
        run_id = first["task"]["run_id"]
        journal = runtime.store.path(run_id, ".journal.jsonl")
        rows = [json.loads(line) for line in journal.read_text().splitlines()]
        self.assertEqual([row["type"] for row in rows], ["checkpoint"])
        self.assertEqual(first["task"]["durable_steps"], 1)
        with journal.open("a") as handle:
            handle.write(
                json.dumps({"type": "request_started", "id": "old-request"}) + "\n"
            )
        with self.assertRaises(WorkflowError):
            runtime.run("test-flow", resume_from_run_id=run_id)

    def test_lock_excludes_other_runtime_and_another_process(self):
        async def script(ctx, args):
            return await ctx.agent("saved")

        runtime = self.runtime(script)
        first = runtime.run("test-flow", {})
        run_id = first["task"]["run_id"]
        with runtime.store.lock(run_id):
            with self.assertRaisesRegex(WorkflowError, "active"):
                self.runtime(script).run("test-flow", resume_from_run_id=run_id)
            code = "from homebrew_harness.orchestration.workflow_runtime import WorkflowStore; import sys\nwith WorkflowStore(sys.argv[1]).lock(sys.argv[2]): print('acquired')"
            child = subprocess.run(
                [sys.executable, "-c", code, str(self.root), run_id],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=PROJECT_ROOT,
            )
            self.assertNotEqual(child.returncode, 0)
            self.assertIn("already active", child.stderr)

    def test_storage_symlinks_and_remote_schema_references_are_rejected(self):
        (self.root / ".harness").symlink_to(self.root / "elsewhere")
        with self.assertRaisesRegex(WorkflowError, "symlink"):
            WorkflowStore(self.root).new_id()
        with self.assertRaisesRegex(WorkflowError, "local"):
            schema_validator({"type": "object", "$ref": "external.json"})

    def test_cancellation_waits_for_request_and_preserves_checkpoint(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def action(*args):
            started.set()
            release.wait(3)
            return RunnerOutput("saved")

        async def script(ctx, args):
            return await ctx.agent("one")

        runner, runtime = Runner(action), None
        runtime = self.runtime(script, runner)

        async def scene():
            task = asyncio.create_task(runtime.arun("test-flow", {}))
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(scene())
        snapshot = json.loads(
            next(
                path
                for path in (self.root / ".harness/workflows").glob("wf_*.json")
                if not path.name.endswith(".output.json")
            ).read_text()
        )
        self.assertEqual(snapshot["task"]["status"], "paused")
        resumed = self.runtime(script, runner).run(
            "test-flow", resume_from_run_id=snapshot["run_id"]
        )
        self.assertEqual(resumed["task"]["progress"]["calls"], 0)
        self.assertEqual(resumed["result"], "saved")

    def test_nested_workflow_shares_statistics_and_enforces_depth(self):
        async def child(ctx, args):
            return await ctx.agent("child", label="child")

        async def parent(ctx, args):
            await ctx.agent("parent", label="parent")
            return await ctx.workflow("child", {})

        registry = WorkflowRegistry(
            [
                WorkflowSpec("parent", "Parent", parent, EMPTY_ARGS),
                WorkflowSpec("child", "Child", child, EMPTY_ARGS),
            ]
        )
        runner = Runner()
        first = WorkflowRuntime(
            self.root, runner, registry, limits=WorkflowLimits()
        ).run("parent", {})
        self.assertEqual(first["task"]["status"], "completed")
        second = WorkflowRuntime(
            self.root, runner, registry, limits=WorkflowLimits()
        ).run("parent", resume_from_run_id=first["task"]["run_id"])
        self.assertEqual(second["result"], "child")
        self.assertEqual(second["task"]["progress"]["calls"], 0)

        async def recursive(ctx, args):
            return await ctx.workflow("recursive", {})

        registry = WorkflowRegistry(
            [WorkflowSpec("recursive", "Recursive", recursive, EMPTY_ARGS)]
        )
        out = WorkflowRuntime(self.root, Runner(), registry).run("recursive", {})
        self.assertEqual(out["task"]["status"], "failed")
        self.assertIn("one child level", out["result"]["error"])

    def test_openai_adapter_uses_fresh_tool_free_messages_and_bounded_output(self):
        message = SimpleNamespace(content='{"value":1}', tool_calls=None)
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="stop")],
            usage=SimpleNamespace(total_tokens=43),
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=Mock(return_value=response))
            )
        )
        runner = ModelWorkflowRunner(client, "test-model")
        result = runner.run("one step", VALUE_SCHEMA, "one", 100)
        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(result.value, {"value": 1})
        self.assertEqual(runner.client.summary()["known_tokens"], 43)
        self.assertEqual(len(request["messages"]), 2)
        self.assertNotIn("tools", request)
        self.assertEqual(request["max_tokens"], 100)
        self.assertEqual(request["response_format"]["type"], "json_schema")
        self.assertEqual(request["response_format"]["json_schema"]["schema"], VALUE_SCHEMA)
        response.choices[0].finish_reason = "length"
        self.assertFalse(runner.run("one step", VALUE_SCHEMA, "one", 100).complete)
        response.choices[0].finish_reason = "stop"
        message.content = '{"value":NaN}'
        self.assertIsInstance(
            runner.run("one step", VALUE_SCHEMA, "one", 100).value, str
        )
        configured = SimpleNamespace(with_options=Mock(return_value=client))
        ModelWorkflowRunner(configured, "test-model")
        configured.with_options.assert_called_once_with(max_retries=0)

    def test_workflow_tool_closes_one_batch_without_intermediate_model_context(self):
        workflow = WorkflowRuntime(
            self.root,
            DemoWorkflowRunner(),
            registry=__import__(
                "homebrew_harness.orchestration.workflows", fromlist=["*"]
            ).default_registry(),
        )
        client, snapshots = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "workflow",
                            name="workflow",
                            arguments=json.dumps(
                                {
                                    "name": "review-changes",
                                    "args": {"changes": DEMO_CHANGES},
                                }
                            ),
                        )
                    ]
                ),
                reply("Review complete."),
            ]
        )
        with HarnessSession(
            client,
            "test-model",
            self.root,
            workflow_runtime=workflow,
            capabilities=Capabilities.full(memory_mode="off"),
        ) as session:
            self.assertEqual(
                session.run_user("Review supplied changes"), "Review complete."
            )
            result = json.loads(
                next(
                    message["content"]
                    for message in session.messages
                    if message.get("role") == "tool"
                )
            )
            self.assertEqual(result["task"]["status"], "completed")
            self.assertEqual(
                sum(message.get("role") == "tool" for message in session.messages), 1
            )
            self.assertEqual(len(snapshots), 2)
            self.assertNotIn("Adversarially verify", json.dumps(snapshots[-1]))
        self.assertNotIn("workflow", homebrew_harness.tools.catalog.SUB_TOOL_NAMES)
        self.assertNotIn("workflow", homebrew_harness.tools.catalog.TEAMMATE_TOOL_NAMES)

    def test_hook_denial_and_extra_model_code_cannot_launch_workflows(self):
        runtime = WorkflowRuntime(
            self.root,
            DemoWorkflowRunner(),
            registry=__import__(
                "homebrew_harness.orchestration.workflows", fromlist=["*"]
            ).default_registry(),
        )
        hooks = HookRegistry()
        hooks.register_hook("PreToolUse", lambda event: "Blocked by host")
        output = execute_fixture_tool(
            reply(
                calls=[
                    call("one", name="workflow", arguments='{"name":"review-changes"}')
                ]
            )
            .choices[0]
            .message.tool_calls[0],
            self.root,
            handlers={"workflow": runtime.run_tool},
            hooks=hooks,
        )
        self.assertEqual(output, "Blocked by host")
        self.assertFalse((self.root / ".harness/workflows").exists())
        malicious = (
            reply(
                calls=[
                    call(
                        "bad",
                        name="workflow",
                        arguments=json.dumps(
                            {
                                "name": "review-changes",
                                "args": {"changes": DEMO_CHANGES},
                                "code": "raise SystemExit",
                            }
                        ),
                    )
                ]
            )
            .choices[0]
            .message.tool_calls[0]
        )
        output = execute_fixture_tool(
            malicious, self.root, handlers={"workflow": runtime.run_tool}
        )
        self.assertIn("error", json.loads(output))
        self.assertFalse((self.root / ".harness/workflows").exists())

        valid = (
            reply(
                calls=[
                    call(
                        "valid",
                        name="workflow",
                        arguments=json.dumps(
                            {
                                "name": "review-changes",
                                "args": {"changes": DEMO_CHANGES},
                            }
                        ),
                    )
                ]
            )
            .choices[0]
            .message.tool_calls[0]
        )
        output = json.loads(
            execute_fixture_tool(valid, self.root, workflow_runtime=runtime)
        )
        self.assertEqual(output["task"]["status"], "completed")

    def test_large_results_return_private_reference_and_short_preview(self):
        async def script(ctx, args):
            return {"body": "data " * 3000}

        runtime = self.runtime(script)
        output = json.loads(runtime.run_tool("test-flow", self.root, {}))
        self.assertTrue(output["result"]["truncated"])
        self.assertLess(len(json.dumps(output)), 3000)
        self.assertTrue((self.root / output["result"]["saved_to"]).is_file())

    def test_workflow_scope_is_bound_to_lead_task_directory(self):
        directory = self.root / "task-scope"
        directory.mkdir()
        runtime = WorkflowRuntime(
            self.root,
            DemoWorkflowRunner(),
            registry=__import__(
                "homebrew_harness.orchestration.workflows", fromlist=["*"]
            ).default_registry(),
        )
        team = SimpleNamespace(lead_cwd=lambda: directory, lead_assignment=None)
        tool = (
            reply(
                calls=[
                    call(
                        "w",
                        name="workflow",
                        arguments=json.dumps(
                            {
                                "name": "review-changes",
                                "args": {"changes": DEMO_CHANGES},
                            }
                        ),
                    )
                ]
            )
            .choices[0]
            .message.tool_calls[0]
        )
        out = json.loads(
            execute_fixture_tool(
                tool,
                self.root,
                team_manager=team,
                handlers={"workflow": runtime.run_tool},
            )
        )
        self.assertEqual(
            runtime.inspect(out["task"]["run_id"])["execution_dir"], "task-scope"
        )
        with self.assertRaisesRegex(WorkflowError, "directory"):
            runtime.run(
                "review-changes",
                resume_from_run_id=out["task"]["run_id"],
                workdir=self.root,
            )

    def test_cron_turn_uses_the_same_host_workflow_runtime(self):
        workflow = WorkflowRuntime(
            self.root,
            DemoWorkflowRunner(),
            registry=__import__(
                "homebrew_harness.orchestration.workflows", fromlist=["*"]
            ).default_registry(),
        )
        client, snapshots = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "workflow",
                            name="workflow",
                            arguments=json.dumps(
                                {
                                    "name": "review-changes",
                                    "args": {"changes": DEMO_CHANGES},
                                }
                            ),
                        )
                    ]
                ),
                reply("Scheduled review complete."),
            ]
        )
        with HarnessSession(
            client,
            "test-model",
            self.root,
            workflow_runtime=workflow,
            capabilities=Capabilities.full(memory_mode="off"),
        ) as session:
            session.cron.schedule(
                "* * * * *", "Run the registered review workflow", recurring=False
            )
            session.cron.poll_due(datetime(2026, 11, 3, 20))
            self.assertTrue(session.process_once())
            out = json.loads(
                next(
                    message["content"]
                    for message in session.messages
                    if message.get("role") == "tool"
                )
            )
            self.assertEqual(out["task"]["status"], "completed")
            self.assertEqual(out["task"]["runner"], DemoWorkflowRunner.identity)
            self.assertEqual(len(snapshots), 2)

    def test_closed_runtime_cannot_launch_new_work(self):
        async def script(ctx, args):
            return await ctx.agent("one")

        runner = Runner()
        runtime = self.runtime(script, runner)
        runtime.close()
        with self.assertRaisesRegex(WorkflowError, "closed"):
            runtime.run("test-flow", {})
        self.assertEqual(runner.calls, [])

    def test_saved_large_result_is_readable_from_its_task_directory(self):
        directory = self.root / "task-scope"
        directory.mkdir()

        async def script(ctx, args):
            return {"rows": ["saved result"] * 1000}

        runtime = self.runtime(script)
        out = json.loads(runtime.run_tool("test-flow", directory, {}))
        reference = out["task"]["result_file"]
        team = SimpleNamespace(lead_cwd=lambda: directory, lead_assignment=None)
        tool = (
            reply(
                calls=[
                    call(
                        "read",
                        name="read_file",
                        arguments=json.dumps({"path": reference}),
                    )
                ]
            )
            .choices[0]
            .message.tool_calls[0]
        )
        result = json.loads(
            execute_fixture_tool(
                tool,
                self.root,
                team_manager=team,
                handlers={"read_file": homebrew_harness.tools.files.run_read},
                workflow_runtime=runtime,
            )
        )
        self.assertIn("saved result", result["content"])
        self.assertFalse(runtime.owns_result(reference, self.root))

    def test_openai_sdk_transport_runs_against_local_service_without_remote_tokens(
        self,
    ):
        from openai import OpenAI

        requests = []

        class Service(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                requests.append(
                    json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                )
                data = json.dumps(
                    {
                        "id": "local-workflow",
                        "object": "chat.completion",
                        "created": 0,
                        "model": "test-model",
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "stop",
                                "message": {
                                    "role": "assistant",
                                    "content": '{"value":7}',
                                },
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 21,
                            "completion_tokens": 4,
                            "total_tokens": 25,
                        },
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            address = (
                "http://"
                + server.server_address[0]
                + ":"
                + str(server.server_address[1])
                + "/v1"
            )
            with OpenAI(
                api_key=uuid.uuid4().hex, base_url=address, timeout=3, max_retries=0
            ) as client:
                _ = client.default_headers

                async def script(ctx, args):
                    return await ctx.agent(
                        "Return the local fixture value.", schema=VALUE_SCHEMA
                    )

                runtime = self.runtime(
                    script, ModelWorkflowRunner(client, "test-model")
                )
                first = runtime.run("test-flow", {})
                resumed = runtime.run(
                    "test-flow", resume_from_run_id=first["task"]["run_id"]
                )
            self.assertEqual(first["result"], {"value": 7})
            self.assertEqual(runtime.runner.client.summary()["known_tokens"], 25)
            self.assertNotIn("tokens", resumed["task"]["progress"])
            self.assertEqual(len(requests), 1)
            self.assertNotIn("tools", requests[0])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
