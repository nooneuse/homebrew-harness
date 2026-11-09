"""Global model costs, real deadlines and cross-component usage contracts."""

from tests.support.clients import model_binding

from homebrew_harness.runtime.capabilities import Capabilities

import io
import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import homebrew_harness.runtime.hooks

import homebrew_harness.runtime.loop
from homebrew_harness.context.compaction import ContextCompactor
from homebrew_harness.security.data_boundary import model_request
from homebrew_harness.runtime.session import HarnessSession
from homebrew_harness.context.memory import MemoryRuntime
from homebrew_harness.execution.model_gateway import (
    ModelGateway,
    GatewayLimits,
    Deadline,
    UsageLedger,
    normalize_usage,
    ModelDeadlineExceeded,
    ModelCancelled,
    ModelGatewayError,
)
from openai import OpenAI
import httpx
from tests.support.clients import fake_client, reply, call
from tests.context.test_memory import record
from homebrew_harness.orchestration.workflow_runtime import (
    ModelWorkflowRunner,
    WorkflowRuntime,
    WorkflowRegistry,
    WorkflowSpec,
)


def response(tokens=20, **details):
    return SimpleNamespace(
        usage=SimpleNamespace(total_tokens=tokens, **details),
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="ok", tool_calls=[]),
            )
        ],
    )


class Fixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        for redirect in (
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            redirect.__enter__()
            self.addCleanup(redirect.__exit__, None, None, None)

    def gateway(self, client=None, **limits):
        client = client or SimpleNamespace(
            _harness_model_identity="test-client",
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=Mock(return_value=response()))
            ),
        )
        gateway = ModelGateway(
            client,
            GatewayLimits(**limits),
            workdir=self.root,
            ledger=UsageLedger(self.root, enabled=True),
        )
        self.addCleanup(lambda: gateway._state.ledger.close(1))
        self.addCleanup(gateway.close)
        return gateway, client

    def invoke(self, gateway, **kwargs):
        try:
            return gateway.request(
                model="test",
                messages=[{"role": "user", "content": "task"}],
                max_tokens=16,
                **kwargs,
            )
        finally:
            gateway._state.ledger.flush(1)


class AccountingTests(Fixture):
    def test_all_provider_token_categories_are_preserved_without_double_counting(self):
        usage = normalize_usage(
            response(
                150,
                prompt_tokens=100,
                completion_tokens=50,
                prompt_cache_hit_tokens=80,
                prompt_cache_miss_tokens=20,
                completion_tokens_details=SimpleNamespace(reasoning_tokens=30),
            )
        )
        self.assertEqual(usage["total_tokens"], 150)
        self.assertEqual(usage["reasoning_tokens"], 30)
        self.assertEqual(usage["cache_hit_tokens"], 80)
        self.assertEqual(usage["cache_miss_tokens"], 20)

    def test_missing_total_uses_parts_and_invalid_values_remain_unknown(self):
        self.assertEqual(
            normalize_usage(response(None, prompt_tokens=5, completion_tokens=8))[
                "total_tokens"
            ],
            13,
        )
        self.assertIsNone(normalize_usage(response(True))["total_tokens"])
        self.assertIsNone(normalize_usage(response(-1))["total_tokens"])
        self.assertEqual(normalize_usage(response(0))["total_tokens"], 0)

    def test_known_usage_and_missing_usage_are_observations(self):
        gateway, client = self.gateway()
        self.invoke(gateway)
        client.chat.completions.create.return_value = response(None)
        self.invoke(gateway)
        summary = gateway.summary()
        self.assertEqual(summary["known_tokens"], 20)
        self.assertEqual(summary["calls"], 2)
        self.assertEqual(summary["unknown_usage_calls"], 1)
        self.assertNotIn("estimated_tokens", summary)
        self.assertNotIn("remaining_tokens", summary)

    def test_failed_request_records_unknown_usage_without_retry(self):
        gateway, client = self.gateway()
        client.chat.completions.create.side_effect = RuntimeError(
            "private request body"
        )
        with self.assertRaises(RuntimeError):
            self.invoke(gateway)
        self.assertEqual(gateway.summary()["known_tokens"], 0)
        self.assertEqual(gateway.summary()["unknown_usage_calls"], 1)
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertNotIn(
            "private request body",
            (self.root / gateway.summary()["ledger"]).read_text(),
        )

    def test_usage_ledger_is_private_and_never_contains_payload_or_credentials(self):
        gateway, client = self.gateway()
        client.api_key = uuid.uuid4().hex
        gateway.request(
            model="test",
            messages=[
                {"role": "user", "content": "PRIVATE_TASK_BODY " + client.api_key}
            ],
            max_tokens=16,
        )
        gateway._state.ledger.flush(1)
        path = self.root / gateway.summary()["ledger"]
        text = path.read_text()
        self.assertNotIn("PRIVATE_TASK_BODY", text)
        self.assertNotIn(client.api_key, text)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        rows = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(
            [row["event"] for row in rows], ["usage_snapshot", "usage_snapshot"]
        )

    def test_observation_failure_does_not_block_model_execution(self):
        gateway, client = self.gateway()
        gateway._state.ledger.append = Mock(side_effect=OSError("fixture"))
        self.invoke(gateway)
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertEqual(gateway.summary()["known_tokens"], 20)
        self.assertFalse(gateway.blocked)

    def test_sdk_retry_policy_is_disabled_before_dispatch(self):
        api = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=Mock(return_value=response()))
            )
        )
        client = SimpleNamespace(max_retries=2, with_options=Mock(return_value=api))
        gateway, _ = self.gateway(client)
        self.invoke(gateway)
        client.with_options.assert_called_once_with(max_retries=0)
        api.chat.completions.create.assert_called_once()


class RuntimeControlTests(Fixture):
    def test_client_clones_share_statistics_without_a_call_budget(self):
        gateway, client = self.gateway()
        clone = gateway.with_options(max_retries=0, timeout=2)
        for _ in range(101):
            self.invoke(clone)
        self.assertEqual(gateway.summary()["calls"], 101)
        self.assertEqual(client.chat.completions.create.call_count, 101)

    def test_large_input_is_not_refused_by_a_cost_estimate(self):
        gateway, client = self.gateway()
        gateway.request(
            model="test",
            messages=[{"role": "user", "content": "material " * 20000}],
            max_tokens=4096,
        )
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertFalse(gateway.blocked)

    def test_large_reported_usage_does_not_block_the_next_request(self):
        gateway, client = self.gateway()
        client.chat.completions.create.return_value = response(1_000_000)
        self.invoke(gateway)
        self.invoke(gateway)
        self.assertEqual(gateway.summary()["known_tokens"], 2_000_000)
        self.assertFalse(gateway.blocked)

    def test_two_requests_can_run_without_token_reservations(self):
        release = threading.Event()
        self.addCleanup(release.set)
        entered = threading.Barrier(3)

        def create(**kwargs):
            entered.wait(2)
            release.wait(2)
            return response(20)

        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        gateway, _ = self.gateway(client, concurrency=2)
        results = []
        workers = [
            threading.Thread(target=lambda: results.append(self.invoke(gateway)))
            for _ in range(2)
        ]
        for worker in workers:
            worker.start()
        entered.wait(2)
        self.assertEqual(gateway.summary()["inflight"], 2)
        release.set()
        for worker in workers:
            worker.join(2)
        self.assertEqual(len(results), 2)
        self.assertEqual(gateway.summary()["known_tokens"], 40)

    def test_global_concurrency_is_enforced_across_actor_scopes(self):
        lock, release, entered = threading.Lock(), threading.Event(), threading.Event()
        active = maximum = 0

        def create(**kwargs):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
                entered.set()
            release.wait(2)
            with lock:
                active -= 1
            return response(20)

        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        gateway, _ = self.gateway(client, concurrency=1)
        results = []

        def run(role):
            with gateway.scope(role=role):
                results.append(self.invoke(gateway))

        workers = [
            threading.Thread(target=run, args=(role,))
            for role in ("lead", "teammate", "subagent")
        ]
        for worker in workers:
            worker.start()
        self.assertTrue(entered.wait(1))
        time.sleep(0.03)
        release.set()
        for worker in workers:
            worker.join(2)
        self.assertEqual(maximum, 1)
        self.assertEqual(len(results), 3)
        self.assertEqual(
            set(gateway.summary()["by_role"]), {"lead", "teammate", "subagent"}
        )

    def test_removed_budget_fields_are_not_accepted(self):
        for kwargs in ({"max_calls": 1}, {"max_tokens": 100}):
            with self.assertRaises(TypeError):
                GatewayLimits(**kwargs)

    def test_nested_gateway_uses_the_active_session_statistics(self):
        parent, _ = self.gateway()
        other, client = self.gateway()
        with parent.scope():
            model_request(other, model="test", messages=[], max_tokens=16)
            model_request(other, model="test", messages=[], max_tokens=16)
        self.assertEqual(parent.summary()["calls"], 2)
        self.assertEqual(other.summary()["calls"], 0)
        self.assertEqual(client.chat.completions.create.call_count, 2)


class DeadlineTests(Fixture):
    def blocked(self, **limits):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def create(**kwargs):
            entered.set()
            release.wait(2)
            return response(23)

        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=Mock(side_effect=create))
            )
        )
        gateway, _ = self.gateway(client, **limits)
        return gateway, client, entered, release

    def test_absolute_timeout_returns_while_request_is_still_running(self):
        gateway, _, entered, release = self.blocked(request_timeout=0.1)
        started = time.monotonic()
        with self.assertRaises(ModelDeadlineExceeded):
            self.invoke(gateway)
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertTrue(entered.is_set())
        self.assertEqual(gateway.summary()["inflight"], 1)
        self.assertEqual(gateway.summary()["pending_usage_calls"], 1)
        release.set()
        self.assertTrue(gateway.wait_idle(1))
        self.assertEqual(gateway.summary()["known_tokens"], 23)
        self.assertEqual(gateway.summary()["late_calls"], 1)

    def test_queue_wait_obeys_deadline_and_does_not_start_a_second_request(self):
        gateway, client, entered, release = self.blocked(concurrency=1)
        results = []
        worker = threading.Thread(target=lambda: results.append(self.invoke(gateway)))
        worker.start()
        self.assertTrue(entered.wait(1))
        with self.assertRaises(ModelDeadlineExceeded):
            self.invoke(gateway, deadline=Deadline.after(0.02))
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertEqual(gateway.summary()["calls"], 1)
        release.set()
        worker.join(2)

    def test_cancelled_request_tracks_dispatch_until_late_usage_arrives(self):
        gateway, _, entered, release = self.blocked()
        cancel = threading.Event()
        errors = []

        def run():
            try:
                self.invoke(gateway, cancel_event=cancel)
            except ModelCancelled:
                errors.append("cancelled")

        worker = threading.Thread(target=run)
        worker.start()
        self.assertTrue(entered.wait(1))
        cancel.set()
        worker.join(1)
        self.assertEqual(errors, ["cancelled"])
        self.assertEqual(gateway.summary()["pending_usage_calls"], 1)
        release.set()
        self.assertTrue(gateway.wait_idle(1))
        self.assertEqual(gateway.summary()["known_tokens"], 23)

    def test_child_scope_cannot_extend_parent_deadline(self):
        gateway, client = self.gateway()
        with gateway.scope(deadline=Deadline.after(0)):
            with gateway.scope(deadline=Deadline.after(10)):
                with self.assertRaises(ModelDeadlineExceeded):
                    self.invoke(gateway)
        client.chat.completions.create.assert_not_called()

    def test_client_view_keeps_a_tighter_timeout_without_with_options_support(self):
        gateway, _, _, release = self.blocked()
        clone = gateway.with_options(timeout=0.02)
        with self.assertRaises(ModelDeadlineExceeded):
            self.invoke(clone)
        release.set()
        self.assertTrue(gateway.wait_idle(1))

    def test_slow_sdk_preparation_cannot_send_after_deadline(self):
        ready, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        api = Mock(return_value=response())

        class Client:
            chat = SimpleNamespace(completions=SimpleNamespace(create=api))

            @property
            def default_headers(self):
                ready.set()
                release.wait(2)
                return {}

        gateway, _ = self.gateway(Client(), request_timeout=0.05)
        with self.assertRaises(ModelDeadlineExceeded):
            self.invoke(gateway)
        self.assertTrue(ready.is_set())
        release.set()
        self.assertTrue(gateway.wait_idle(1))
        api.assert_not_called()
        self.assertEqual(gateway.summary()["known_tokens"], 0)

    def test_sdk_socket_timeout_limits_are_not_relaxed(self):
        gateway, client = self.gateway()
        client.timeout = httpx.Timeout(connect=0.1, read=1, write=0.2, pool=0.3)
        self.invoke(gateway)
        timeout = client.chat.completions.create.call_args.kwargs["timeout"]
        self.assertEqual(timeout.connect, 0.1)
        self.assertEqual(timeout.write, 0.2)
        self.assertEqual(timeout.pool, 0.3)
        self.assertLessEqual(timeout.read, 1)

    def test_deadline_after_pre_hook_blocks_the_approved_tool(self):
        client, _ = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "write",
                            name="write_file",
                            arguments='{"path":"not-written.txt","content":"no"}',
                        )
                    ]
                )
            ]
        )
        gateway, _ = self.gateway(client)
        hooks = homebrew_harness.runtime.hooks.create_hooks()
        expired = threading.Event()
        hooks.register_hook("PreToolUse", lambda event: expired.set())
        remaining = Deadline.remaining

        def controlled_remaining(deadline):
            return (
                0
                if expired.is_set() and deadline.expires_at is not None
                else remaining(deadline)
            )

        history = [{"role": "user", "content": "Write"}]
        with (
            patch.object(Deadline, "remaining", controlled_remaining),
            gateway.scope(deadline=Deadline.after(10)),
            self.assertRaises(ModelDeadlineExceeded),
        ):
            homebrew_harness.runtime.loop.agent_loop(
                gateway,
                "test",
                history,
                self.root,
                hooks=hooks,
                capabilities=Capabilities.full(memory_mode="off"),
            )
        ContextCompactor.groups(history)
        self.assertFalse((self.root / "not-written.txt").exists())
        result = json.loads(
            next(
                message["content"]
                for message in history
                if message.get("tool_call_id") == "write"
            )
        )
        self.assertFalse(result["executed"])

    def test_session_deadline_is_shared_by_future_requests(self):
        gateway, client = self.gateway(session_timeout=0.03)
        time.sleep(0.04)
        with self.assertRaises(ModelDeadlineExceeded):
            self.invoke(gateway)
        client.chat.completions.create.assert_not_called()

    def test_late_model_tool_response_never_executes_the_tool(self):
        release = threading.Event()
        self.addCleanup(release.set)
        returned = reply(
            calls=[
                call(
                    "late",
                    name="write_file",
                    arguments='{"path":"late.txt","content":"no"}',
                )
            ]
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=lambda **kwargs: (release.wait(2), returned)[1]
                )
            )
        )
        gateway, _ = self.gateway(client, request_timeout=0.03)
        with self.assertRaises(ModelDeadlineExceeded):
            homebrew_harness.runtime.loop.agent_loop(
                gateway,
                "test",
                [{"role": "user", "content": "Write"}],
                self.root,
                capabilities=Capabilities.full(memory_mode="off"),
            )
        release.set()
        self.assertTrue(gateway.wait_idle(1))
        self.assertFalse((self.root / "late.txt").exists())

    def test_deadline_between_tools_closes_batch_without_later_side_effect(self):
        returned = reply(
            calls=[
                call(
                    "first",
                    name="write_file",
                    arguments='{"path":"first.txt","content":"yes"}',
                ),
                call(
                    "later",
                    name="write_file",
                    arguments='{"path":"later.txt","content":"no"}',
                ),
            ]
        )
        client, _ = fake_client([returned])
        gateway, _ = self.gateway(client)
        hooks = homebrew_harness.runtime.hooks.create_hooks()
        expired = threading.Event()
        hooks.register_hook("PostToolUse", lambda event, output: expired.set())
        remaining = Deadline.remaining

        def controlled_remaining(deadline):
            return (
                0
                if expired.is_set() and deadline.expires_at is not None
                else remaining(deadline)
            )

        history = [{"role": "user", "content": "Write"}]
        # Inject clock expiry only after the first tool has completed.
        with (
            patch.object(Deadline, "remaining", controlled_remaining),
            gateway.scope(deadline=Deadline.after(10)),
            self.assertRaises(ModelDeadlineExceeded),
        ):
            homebrew_harness.runtime.loop.agent_loop(
                gateway,
                "test",
                history,
                self.root,
                hooks=hooks,
                capabilities=Capabilities.full(memory_mode="off"),
            )
        ContextCompactor.groups(history)
        self.assertTrue((self.root / "first.txt").exists())
        self.assertFalse((self.root / "later.txt").exists())


class IntegrationTests(Fixture):
    def test_worker_compaction_memory_and_goal_share_one_ledger(self):
        client, _ = fake_client(
            [
                reply("[0]"),
                reply(calls=[call("compact", name="compact", arguments="{}")]),
                reply("summary"),
                reply("answer"),
                reply(
                    '{"ok":true,"impossible":false,"reason":"Text supplied","evidence_ids":["final"]}'
                ),
                reply("[]"),
            ]
        )
        memory = MemoryRuntime(client, "test", self.root, mode="auto")
        memory.store.replace([record()], memory.store.snapshot().version)
        session = HarnessSession(
            client,
            "test",
            self.root,
            memory_runtime=memory,
            capabilities=Capabilities.full(memory_mode="auto"),
        )
        self.addCleanup(lambda: session.wait_closed(5))
        self.addCleanup(session.close)
        self.assertIn(
            "[goal achieved]", session.run_user("/goal provide a short answer")
        )
        summary = session.gateway.summary()
        self.assertEqual(summary["calls"], 6)
        self.assertEqual(summary["by_purpose"]["worker"]["calls"], 2)
        self.assertEqual(
            set(summary["by_purpose"]),
            {
                "worker",
                "memory_selection",
                "memory_extraction",
                "compaction",
                "goal_check",
            },
        )

    def test_subagent_uses_parent_statistics_and_reports_its_role(self):
        client, _ = fake_client(
            [
                reply(
                    calls=[call("child", name="task", arguments='{"prompt":"Explain"}')]
                ),
                reply("child answer"),
                reply("lead answer"),
            ]
        )
        session = HarnessSession(
            client, "test", self.root, capabilities=Capabilities.full(memory_mode="off")
        )
        self.addCleanup(lambda: session.wait_closed(5))
        self.addCleanup(session.close)
        self.assertEqual(session.run_user("Explain"), "lead answer")
        summary = session.gateway.summary()
        self.assertEqual(summary["calls"], 3)
        self.assertEqual(summary["by_role"]["subagent"]["calls"], 1)

    def test_human_continuation_and_new_tasks_keep_cumulative_statistics(self):
        client, _ = fake_client([reply("first"), reply("second"), reply("third")])
        session = HarnessSession(
            client, "test", self.root, capabilities=Capabilities.full(memory_mode="off")
        )
        self.addCleanup(lambda: session.wait_closed(5))
        self.addCleanup(session.close)
        session.run_user("First task")
        session.run_user("继续")
        session.run_user("/task New task")
        self.assertEqual(session.gateway.summary()["calls"], 3)

    def test_usage_command_has_no_model_call(self):
        client, _ = fake_client([])
        session = HarnessSession(
            client, "test", self.root, capabilities=Capabilities.full(memory_mode="off")
        )
        self.addCleanup(lambda: session.wait_closed(5))
        self.addCleanup(session.close)
        self.assertEqual(json.loads(session.run_user("/usage"))["calls"], 0)
        client.chat.completions.create.assert_not_called()

    def test_workflow_steps_and_json_corrections_use_session_statistics(self):
        async def script(ctx, args):
            return await ctx.agent(
                "Return JSON",
                schema={
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                },
            )

        client, _ = fake_client([reply("bad JSON"), reply('{"ok":true}')])
        gateway, _ = self.gateway(client)
        runtime = WorkflowRuntime(
            self.root,
            ModelWorkflowRunner(model_binding(gateway, "test")),
            WorkflowRegistry(
                [
                    WorkflowSpec(
                        "test", "test", script, {"type": "object", "properties": {}}
                    )
                ]
            ),
        )
        output = runtime.run("test", {})
        self.assertEqual(output["task"]["status"], "completed")
        self.assertEqual(gateway.summary()["by_purpose"]["workflow_step"]["calls"], 2)
        self.assertEqual(gateway.summary()["by_role"]["workflow"]["calls"], 2)

    def test_optional_memory_failure_cannot_hide_completed_primary_answer(self):
        client, _ = fake_client([reply("primary answer")])
        memory = MemoryRuntime(client, "test", self.root, mode="auto")
        session = HarnessSession(
            client,
            "test",
            self.root,
            memory_runtime=memory,
            capabilities=Capabilities.full(memory_mode="auto"),
        )
        self.addCleanup(lambda: session.wait_closed(5))
        self.addCleanup(session.close)
        self.assertEqual(session.run_user("Use tabs for indentation"), "primary answer")
        self.assertEqual(session.gateway.summary()["unknown_usage_calls"], 2)

    def test_native_teammate_and_lead_use_the_same_statistics(self):
        def create(**kwargs):
            names = {tool["function"]["name"] for tool in kwargs["tools"]}
            return reply("member answer" if "submit_plan" in names else "lead answer")

        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=Mock(side_effect=create))
            )
        )
        session = HarnessSession(
            client, "test", self.root, capabilities=Capabilities.full(memory_mode="off")
        )
        self.addCleanup(lambda: session.wait_closed(5))
        self.addCleanup(session.close)
        arguments = dict(name="reader", role="reader", prompt="Read only")
        session.team.authorize_spawn(arguments)
        session.team.spawn(**arguments)
        member = session.team.members["reader"]
        deadline = time.monotonic() + 2
        while member.status != "idle" and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        self.assertEqual(member.status, "idle")
        self.assertEqual(session.run_user("Give a short answer"), "lead answer")
        summary = session.gateway.summary()
        self.assertEqual(summary["calls"], 2)
        self.assertEqual(summary["by_role"]["teammate"]["calls"], 1)
        self.assertEqual(summary["by_role"]["lead"]["calls"], 1)

    def test_teammate_thread_inherits_parent_operation_deadline(self):
        release = threading.Event()
        self.addCleanup(release.set)
        result = reply(
            calls=[
                call(
                    "late",
                    name="write_file",
                    arguments='{"path":"late-member.txt","content":"no"}',
                )
            ]
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=lambda **kwargs: (release.wait(2), result)[1]
                )
            )
        )
        session = HarnessSession(
            client, "test", self.root, capabilities=Capabilities.full(memory_mode="off")
        )
        self.addCleanup(lambda: session.wait_closed(5))
        self.addCleanup(session.close)
        task = session.team.store.create("Bounded work")
        arguments = dict(
            name="reader",
            role="reader",
            prompt="Write in the task directory",
            task_id=task.id,
        )
        session.team.authorize_spawn(arguments)
        with session.gateway.scope(deadline=Deadline.after(0.15)):
            session.team.spawn(**arguments)
        member = session.team.members["reader"]
        limit = time.monotonic() + 1
        while member.status != "failed" and time.monotonic() < limit:
            threading.Event().wait(0.01)
        self.assertEqual(member.status, "failed")
        release.set()
        self.assertTrue(session.gateway.wait_idle(1))
        self.assertFalse((self.root / "late-member.txt").exists())

    def test_cron_and_user_work_share_cumulative_statistics(self):
        client, _ = fake_client(
            [reply("human answer"), reply("scheduled answer"), reply("another")]
        )
        session = HarnessSession(
            client, "test", self.root, capabilities=Capabilities.full(memory_mode="off")
        )
        self.addCleanup(lambda: session.wait_closed(5))
        self.addCleanup(session.close)
        session.run_user("Human task")
        session.cron.schedule("* * * * *", "Scheduled task", recurring=False)
        session.cron.poll_due(datetime(2026, 11, 4, 9))
        self.assertTrue(session.process_once())
        session.run_user("Another task")
        self.assertEqual(session.gateway.summary()["calls"], 3)

    def test_workflow_checkpoint_cache_consumes_no_new_global_request(self):
        async def script(ctx, args):
            return await ctx.agent(
                "Return JSON",
                schema={
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                },
            )

        client, _ = fake_client([reply('{"ok":true}')])
        gateway, _ = self.gateway(client)
        registry = WorkflowRegistry(
            [WorkflowSpec("test", "test", script, {"type": "object", "properties": {}})]
        )
        first = WorkflowRuntime(
            self.root, ModelWorkflowRunner(model_binding(gateway, "test")), registry
        ).run("test", {})
        resumed = WorkflowRuntime(
            self.root, ModelWorkflowRunner(model_binding(gateway, "test")), registry
        ).run("test", resume_from_run_id=first["task"]["run_id"])
        self.assertEqual(resumed["task"]["status"], "completed")
        self.assertEqual(resumed["task"]["progress"]["calls"], 0)
        self.assertEqual(gateway.summary()["calls"], 1)
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_authentication_rejection_stops_without_automatic_retries(self):
        class Denied(Exception):
            status_code = 401

        gateway, client = self.gateway()
        client.chat.completions.create.side_effect = Denied(
            "private rejected credential"
        )
        with self.assertRaises(Denied):
            self.invoke(gateway)
        with self.assertRaises(ModelGatewayError):
            self.invoke(gateway)
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertEqual(
            gateway.summary()["blocked"], "provider authentication or access denied"
        )
        gateway.reset_failures()
        client.chat.completions.create.side_effect = None
        client.chat.completions.create.return_value = response()
        self.invoke(gateway)
        self.assertEqual(client.chat.completions.create.call_count, 2)

    def test_consecutive_provider_failures_stop_automatic_calls_until_host_reset(self):
        from homebrew_harness.execution.model_protocol import ProviderError

        Unavailable = ProviderError

        gateway, client = self.gateway(max_failures=2)
        client.chat.completions.create.side_effect = Unavailable(
            "server_error", status_code=503
        )
        for _ in range(2):
            with self.assertRaises(Unavailable):
                self.invoke(gateway)
        with self.assertRaises(ModelGatewayError):
            self.invoke(gateway)
        self.assertEqual(client.chat.completions.create.call_count, 2)
        gateway.reset_failures()
        client.chat.completions.create.side_effect = None
        client.chat.completions.create.return_value = response()
        self.invoke(gateway)
        self.assertEqual(client.chat.completions.create.call_count, 3)


class RealTransportTests(Fixture):
    def test_real_sdk_slow_chunks_do_not_extend_absolute_deadline(self):
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
                        "id": "local",
                        "object": "chat.completion",
                        "created": 0,
                        "model": "test",
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "stop",
                                "message": {"role": "assistant", "content": "answer"},
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 10,
                            "completion_tokens": 5,
                            "total_tokens": 15,
                        },
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    for index in range(0, len(data), 30):
                        self.wfile.write(data[index : index + 30])
                        self.wfile.flush()
                        time.sleep(0.04)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        address = (
            "http://"
            + server.server_address[0]
            + ":"
            + str(server.server_address[1])
            + "/v1"
        )
        try:
            with OpenAI(
                api_key=uuid.uuid4().hex,
                base_url=address,
                max_retries=2,
                timeout=1,
                http_client=httpx.Client(trust_env=False),
            ) as client:
                # Warm SDK platform headers so this test isolates slow response chunks.
                _ = client.default_headers
                _ = client.chat.completions
                gateway, _ = self.gateway(client, request_timeout=0.15)
                started = time.monotonic()
                with self.assertRaises(ModelDeadlineExceeded) as raised:
                    self.invoke(gateway)
                self.assertEqual(raised.exception.source, "request")
                self.assertEqual(raised.exception.phase, "transport")
                self.assertTrue(raised.exception.sent)
                self.assertIsNotNone(raised.exception.request_id)
                self.assertLess(time.monotonic() - started, 0.3)
                self.assertTrue(gateway.wait_idle(2))
                self.assertEqual(len(requests), 1)
                self.assertEqual(gateway.summary()["known_tokens"], 15)
                self.assertNotIn(
                    address, (self.root / gateway.summary()["ledger"]).read_text()
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
