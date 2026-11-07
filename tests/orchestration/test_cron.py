"""Cron matching, durable handoff, idle execution and scheduled permissions."""

from __future__ import annotations
from tests.support.paths import PROJECT_ROOT
from homebrew_harness.runtime.capabilities import Capabilities

import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from homebrew_harness.runtime.loop_components import LoopComponents
from tests.support.tools import execute_fixture_tool
import homebrew_harness.cli
import homebrew_harness.configuration
import homebrew_harness.context.memory
import homebrew_harness.context.skill_loader
import homebrew_harness.runtime.errors
import homebrew_harness.runtime.hooks
import homebrew_harness.runtime.loop
import homebrew_harness.runtime.prompts
import homebrew_harness.runtime.turns
import homebrew_harness.tools.dispatch
import homebrew_harness.tools.files
import homebrew_harness.tools.registry
import homebrew_harness.tools.shell
import homebrew_harness.security.permissions as permissions
from homebrew_harness.context.compaction import ContextCompactor
from homebrew_harness.execution.background import BackgroundManager
from homebrew_harness.orchestration.cron_scheduler import (
    CRON_TOOL_NAMES,
    CronError,
    CronScheduler,
    cron_matches,
    validate_cron,
)
from homebrew_harness.context.skill_loader import SkillLoader
from tests.support.clients import call, fake_client, reply
from homebrew_harness.context.todo import TodoManager


class CronMatchingTests(unittest.TestCase):
    def test_five_fields_lists_ranges_steps_and_sunday_zero(self):
        monday = datetime(2026, 11, 2, 9, 10)
        sunday = datetime(2026, 11, 1, 9, 10)
        for expression in (
            "* * * * *",
            "*/5 9 * * 1-5",
            "0,10,20 8-10 * 11 1",
            "10 9 2 11 *",
        ):
            with self.subTest(expression=expression):
                self.assertTrue(cron_matches(expression, monday))
        self.assertTrue(cron_matches("10 9 * * 0", sunday))
        self.assertFalse(cron_matches("*/3 9 * * *", monday))
        self.assertFalse(cron_matches("10 8 * * *", monday))
        self.assertFalse(cron_matches("10 9 * 12 *", monday))
        self.assertEqual(validate_cron("  0   9 * * 1-5  "), "0 9 * * 1-5")

    def test_day_and_weekday_use_or_when_both_are_restricted(self):
        expression = "0 9 1 * 1"
        self.assertTrue(
            cron_matches(expression, datetime(2026, 11, 1, 9))
        )  # Day 1, Sunday.
        self.assertTrue(
            cron_matches(expression, datetime(2026, 11, 2, 9))
        )  # Monday, day 2.
        self.assertFalse(cron_matches(expression, datetime(2026, 11, 3, 9)))

    def test_invalid_syntax_and_field_bounds_are_rejected(self):
        expressions = [
            "* * * *",
            "* * * * * *",
            "60 * * * *",
            "* 24 * * *",
            "* * 0 * *",
            "* * * 13 *",
            "* * * * 7",
            "*/0 * * * *",
            "*/-1 * * * *",
            "9-3 * * * *",
            "* * * * mon",
            "1,,2 * * * *",
            "1-4/2 * * * *",
            "@daily",
            "-1 * * * *",
            None,
        ]
        for expression in expressions:
            with self.subTest(expression=expression), self.assertRaises(CronError):
                validate_cron(expression)


class CronStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.scheduler = CronScheduler(self.root)
        self.moment = datetime(2026, 11, 3, 9)

    def test_empty_and_volatile_jobs_do_not_create_saved_data(self):
        self.assertEqual(self.scheduler.list(), [])
        self.assertFalse(self.scheduler.path.exists())
        self.scheduler.schedule("* * * * *", "Temporary work", durable=False)
        self.scheduler.poll_due(self.moment)
        self.assertFalse(self.scheduler.path.exists())
        self.assertEqual(CronScheduler(self.root).list(), [])

    def test_default_durable_definition_and_pending_delivery_survive_restart(self):
        job = self.scheduler.schedule("0 9 * * *", "Read file and verify")
        self.assertTrue(job.recurring and job.durable)
        self.assertEqual(CronScheduler(self.root).list(), [job])
        due = self.scheduler.poll_due(self.moment)
        recovered = CronScheduler(self.root)
        self.assertEqual(recovered.take_pending(), due[0])
        self.assertIsNone(recovered.take_pending())
        self.assertEqual(due[0].last_fired, "2026-11-03 09:00")
        self.assertEqual(
            json.loads(self.scheduler.path.read_text())[0]["pending_delivery"], True
        )

    def test_minute_dedup_and_pending_backlog_do_not_accumulate(self):
        self.scheduler.schedule("* * * * *", "Work")
        first = self.scheduler.poll_due(self.moment)[0]
        self.assertEqual(
            self.scheduler.poll_due(self.moment + timedelta(seconds=40)), []
        )
        self.assertEqual(
            self.scheduler.poll_due(self.moment + timedelta(minutes=1)), []
        )
        self.assertEqual(self.scheduler.take_pending(), first)
        self.assertIsNone(self.scheduler.take_pending())
        self.scheduler.restore(first)
        self.scheduler.restore(first)
        self.assertEqual(self.scheduler.take_pending(), first)
        self.assertIsNone(self.scheduler.take_pending())

    def test_recurring_ack_rearms_and_one_shot_ack_removes_definition(self):
        recurring = self.scheduler.schedule("* * * * *", "Recurring")
        once = self.scheduler.schedule("* * * * *", "Once", recurring=False)
        self.scheduler.poll_due(self.moment)
        jobs = [self.scheduler.take_pending(), self.scheduler.take_pending()]
        for job in jobs:
            self.scheduler.acknowledge(job)
        self.assertEqual([job.id for job in self.scheduler.list()], [recurring.id])
        self.assertFalse(self.scheduler.list()[0].pending_delivery)
        self.assertEqual(self.scheduler.poll_due(self.moment), [])
        self.assertEqual(
            len(self.scheduler.poll_due(self.moment + timedelta(minutes=1))), 1
        )
        self.assertNotIn(once.id, self.scheduler._jobs)

    def test_restart_does_not_backfill_missed_minutes(self):
        self.scheduler.schedule("0 9 * * *", "Daily")
        recovered = CronScheduler(self.root)
        self.assertFalse(recovered.has_pending())
        self.assertEqual(recovered.poll_due(self.moment.replace(hour=10)), [])
        self.assertEqual(len(recovered.poll_due(self.moment + timedelta(days=1))), 1)

    def test_cancel_removes_queued_job_and_rejects_stale_ack(self):
        job = self.scheduler.schedule("* * * * *", "Work")
        self.scheduler.poll_due(self.moment)
        delivered = self.scheduler.take_pending()
        self.scheduler.cancel(job.id)
        self.scheduler.restore(delivered)
        self.assertFalse(self.scheduler.has_pending())
        self.assertEqual(CronScheduler(self.root).list(), [])
        with self.assertRaises(CronError):
            self.scheduler.acknowledge(delivered)
        for job_id in (job.id, "../outside", "cron_bad"):
            with self.assertRaises(CronError):
                self.scheduler.cancel(job_id)

    def test_create_enqueue_cancel_and_ack_failures_keep_previous_state(self):
        job = self.scheduler.schedule("* * * * *", "Work")
        before = self.scheduler.path.read_bytes()
        with patch(
            "homebrew_harness.orchestration.cron_scheduler.os.replace",
            side_effect=OSError("Failure"),
        ):
            for action in (
                lambda: self.scheduler.schedule("* * * * *", "New"),
                lambda: self.scheduler.poll_due(self.moment),
                lambda: self.scheduler.cancel(job.id),
            ):
                with self.assertRaises(OSError):
                    action()
                self.assertEqual(self.scheduler.path.read_bytes(), before)
                self.assertEqual(self.scheduler.list(), [job])
                self.assertFalse(self.scheduler.has_pending())
        pending = self.scheduler.poll_due(self.moment)[0]
        self.scheduler.take_pending()
        before = self.scheduler.path.read_bytes()
        with (
            patch(
                "homebrew_harness.orchestration.cron_scheduler.os.replace",
                side_effect=OSError("Failure"),
            ),
            self.assertRaises(OSError),
        ):
            self.scheduler.acknowledge(pending)
        self.assertEqual(self.scheduler.path.read_bytes(), before)
        self.scheduler.restore(pending)
        self.assertEqual(self.scheduler.take_pending(), pending)
        self.assertEqual(list(self.root.glob(".scheduled_tasks-*.tmp")), [])

    def test_stale_instance_cannot_overwrite_another_writer(self):
        other = CronScheduler(self.root)
        original = self.scheduler.schedule("* * * * *", "Original")
        with self.assertRaisesRegex(CronError, "changed externally"):
            other.schedule("* * * * *", "Stale write")
        self.assertEqual(other.list(), [])
        self.assertEqual(CronScheduler(self.root).list(), [original])

    def test_invalid_saved_data_and_symlinks_are_reported_without_overwrite(self):
        job = self.scheduler.schedule("* * * * *", "Original")
        saved = asdict(job)
        corruptions = [
            "bad JSON",
            json.dumps({}),
            json.dumps([saved, saved]),
            json.dumps([{**saved, "recurring": "true"}]),
            json.dumps([{**saved, "prompt": " "}]),
            json.dumps([{**saved, "cron": "60 * * * *"}]),
            json.dumps([{**saved, "extra": True}]),
            json.dumps([{**saved, "pending_delivery": True}]),
        ]
        for content in corruptions:
            self.scheduler.path.write_text(content)
            with self.subTest(content=content), self.assertRaises(CronError):
                CronScheduler(self.root)
            self.assertEqual(self.scheduler.path.read_text(), content)
        outside = self.root / "outside.json"
        outside.write_text("[]")
        self.scheduler.path.unlink()
        self.scheduler.path.symlink_to(outside)
        with self.assertRaisesRegex(CronError, "symlink"):
            CronScheduler(self.root)
        self.assertEqual(outside.read_text(), "[]")

    def test_id_collision_and_host_argument_validation(self):
        with patch(
            "homebrew_harness.orchestration.cron_scheduler.secrets.token_hex",
            side_effect=["aaaaaaaa", "aaaaaaaa", "bbbbbbbb"],
        ):
            first = self.scheduler.schedule("* * * * *", "First")
            second = self.scheduler.schedule("* * * * *", "Second")
        self.assertNotEqual(first.id, second.id)
        for arguments in (
            {"prompt": " "},
            {"prompt": "Work", "recurring": 1},
            {"prompt": "Work", "durable": "true"},
        ):
            with self.assertRaises(CronError):
                self.scheduler.schedule("* * * * *", **arguments)

    def test_fresh_python_process_recovers_and_acknowledges_pending_task(self):
        job = self.scheduler.schedule("* * * * *", "Recover me", recurring=False)
        self.scheduler.poll_due(self.moment)
        script = """
import json, sys
from pathlib import Path
from homebrew_harness.orchestration.cron_scheduler import CronScheduler
store = CronScheduler(Path(sys.argv[1]))
job = store.take_pending()
assert job.id == sys.argv[2] and job.prompt == 'Recover me'
store.acknowledge(job)
print(json.dumps([job.id for job in store.list()]))
"""
        result = subprocess.run(
            [sys.executable, "-c", script, str(self.root), job.id],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            timeout=5,
        )
        self.assertEqual(json.loads(result.stdout), [])
        self.assertEqual(CronScheduler(self.root).list(), [])


class CronExecutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.scheduler = CronScheduler(self.root)
        self.hooks = homebrew_harness.runtime.hooks.create_hooks()
        self.moment = datetime(2026, 11, 3, 9)
        self.history = [
            {
                "role": "system",
                "content": homebrew_harness.runtime.prompts.build_system_prompt(
                    self.root,
                    skill_loader=homebrew_harness.context.skill_loader.SkillLoader(
                        self.root / "skills"
                    ),
                ),
            },
            {"role": "user", "content": "Earlier human request"},
            {"role": "assistant", "content": "Done"},
        ]
        self.stderr = io.StringIO()
        capture = redirect_stderr(self.stderr)
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def pending(self, *, recurring=False):
        self.scheduler.schedule(
            "* * * * *", "Create output.txt and verify it", recurring=recurring
        )
        self.scheduler.poll_due(self.moment)
        return self.scheduler.take_pending()

    def run_job(self, job, client, **kwargs):
        parts = {
            name: kwargs.pop(name)
            for name in tuple(kwargs)
            if name in LoopComponents.__dataclass_fields__
        }
        return homebrew_harness.runtime.turns.run_scheduled_turn(
            job,
            client=client,
            model="test-model",
            messages=self.history,
            workdir=self.root,
            scheduler=self.scheduler,
            **kwargs,
            components=LoopComponents(
                hooks=self.hooks,
                skill_loader=SkillLoader(self.root / "skills"),
                compactor=ContextCompactor(client, "test-model", self.root),
                **parts,
            ),
        )

    def invoke(self, name, arguments):
        block = (
            reply(calls=[call("cron-call", name=name, arguments=json.dumps(arguments))])
            .choices[0]
            .message.tool_calls[0]
        )
        handlers = homebrew_harness.tools.registry.create_tool_handlers(
            TodoManager(), cron_scheduler=self.scheduler
        )
        return json.loads(
            execute_fixture_tool(block, self.root, hooks=self.hooks, handlers=handlers)
        )

    def test_three_tools_have_explicit_permissions_and_hooked_structured_results(self):
        events = []
        self.hooks.register_hook(
            "PostToolUse", lambda event, output: events.append(event.name)
        )
        with patch("homebrew_harness.security.permissions.ask_user") as ask:
            job = self.invoke(
                "schedule_cron", {"cron": "0 9 * * 1-5", "prompt": "Read notes"}
            )
            self.assertEqual(self.invoke("list_crons", {})["jobs"][0]["id"], job["id"])
            self.assertEqual(
                self.invoke("cancel_cron", {"job_id": job["id"]})["cancelled"]["id"],
                job["id"],
            )
        self.assertEqual(events, list(CRON_TOOL_NAMES))
        ask.assert_not_called()
        for name in CRON_TOOL_NAMES:
            self.assertEqual(
                permissions.check_rules(name, {}, self.root).action, "allow"
            )

    def test_invalid_schema_and_pre_hook_cannot_create_schedule(self):
        for extra in (
            {"owner": "other"},
            {"durable": "true"},
            {"recurring": 1},
            {"path": "outside"},
        ):
            result = self.invoke(
                "schedule_cron", {"cron": "* * * * *", "prompt": "Work", **extra}
            )
            self.assertIn("error", result)
        self.assertEqual(self.scheduler.list(), [])
        self.hooks.register_hook(
            "PreToolUse",
            lambda event: (
                '{"error":"Blocked"}' if event.name == "schedule_cron" else None
            ),
        )
        self.assertIn(
            "error",
            self.invoke("schedule_cron", {"cron": "* * * * *", "prompt": "Work"}),
        )
        self.assertEqual(self.scheduler.list(), [])

    def test_ordinary_file_tools_cannot_rewrite_schedule_through_alias(self):
        self.scheduler.schedule("* * * * *", "Original")
        before = self.scheduler.path.read_bytes()
        (self.root / "alias.json").symlink_to(self.scheduler.path)
        for path in (".scheduled_tasks.json", "alias.json", ".scheduled_tasks.lock"):
            result = self.invoke("write_file", {"path": path, "content": "bypass"})
            self.assertEqual(result["permission"], "deny")
        self.assertEqual(self.scheduler.path.read_bytes(), before)

    def test_model_receipt_is_persisted_before_tools_and_human_input_hook_is_skipped(
        self,
    ):
        job = self.pending()
        user_hook = Mock()
        self.hooks.register_hook("UserPromptSubmit", user_hook)
        client, snapshots = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "write",
                            name="write_file",
                            arguments='{"path":"output.txt","content":"ready"}',
                        )
                    ]
                ),
                reply("Verified completion"),
            ]
        )
        original = homebrew_harness.tools.files.run_write

        def write(**kwargs):
            recovered = CronScheduler(self.root)
            self.assertTrue(recovered.list()[0].pending_delivery)
            self.assertTrue(recovered.unresolved())
            self.assertFalse(recovered.has_pending())
            return original(**kwargs)

        with patch.dict(
            homebrew_harness.tools.registry.TOOL_HANDLERS, {"write_file": write}
        ):
            self.assertEqual(self.run_job(job, client), "Verified completion")
        user_hook.assert_not_called()
        self.assertEqual(snapshots[0][-1]["name"], "scheduled_task")
        self.assertIn(job.id, snapshots[0][-1]["content"])
        self.assertEqual((self.root / "output.txt").read_text(), "ready")
        self.assertEqual(self.scheduler.list(), [])

    def test_initial_model_failure_restores_pending_job_and_history(self):
        job = self.pending()
        before = copy.deepcopy(self.history)
        client, _ = fake_client([])
        client.chat.completions.create.side_effect = RuntimeError("private endpoint")
        with self.assertRaises(RuntimeError):
            self.run_job(job, client)
        self.assertEqual(self.history, before)
        self.assertEqual(self.scheduler.take_pending(), job)
        self.assertTrue(CronScheduler(self.root).has_pending())

    def test_ack_failure_prevents_side_effect_and_leaves_durable_delivery_pending(self):
        job = self.pending()
        client, _ = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "write",
                            name="write_file",
                            arguments='{"path":"output.txt","content":"ready"}',
                        )
                    ]
                )
            ]
        )
        with (
            patch.object(self.scheduler, "received", side_effect=OSError("Failure")),
            self.assertRaises(OSError),
        ):
            self.run_job(job, client)
        self.assertFalse((self.root / "output.txt").exists())
        self.assertEqual(self.scheduler.take_pending(), job)
        self.assertTrue(CronScheduler(self.root).has_pending())

    def test_failed_scheduled_request_preserves_independent_background_notifications(
        self,
    ):
        job = self.pending()
        with BackgroundManager(
            self.root,
            lambda *args, **kwargs: (
                '{"exit_code":0,"stdout":"prior background result","stderr":""}'
            ),
        ) as manager:
            manager.start("prior background command")
            for thread in manager._threads:
                thread.join(timeout=2)
            client, _ = fake_client([])
            client.chat.completions.create.side_effect = RuntimeError("API failure")
            with self.assertRaises(RuntimeError):
                self.run_job(job, client, background_manager=manager)
            notifications = [
                item
                for item in self.history
                if item.get("name") == "background_notification"
            ]
            self.assertEqual(len(notifications), 1)
            self.assertIn("prior background result", notifications[0]["content"])
            self.assertFalse(
                any(item.get("name") == "scheduled_task" for item in self.history)
            )
            self.assertEqual(manager.collect(), [])

    def test_failure_after_receipt_does_not_redeliver_completed_side_effects(self):
        job = self.pending()
        client, _ = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "write",
                            name="write_file",
                            arguments='{"path":"output.txt","content":"ready"}',
                        )
                    ]
                )
            ]
        )
        create = client.chat.completions.create.side_effect
        calls = 0

        def fail_second(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("private endpoint")
            return create(**kwargs)

        client.chat.completions.create.side_effect = fail_second
        with self.assertRaises(RuntimeError):
            self.run_job(job, client)
        self.assertEqual((self.root / "output.txt").read_text(), "ready")
        self.assertFalse(self.scheduler.has_pending())
        recovered = CronScheduler(self.root)
        self.assertTrue(recovered.list()[0].pending_delivery)
        self.assertTrue(recovered.unresolved())
        self.assertFalse(recovered.has_pending())
        self.assertEqual(self.history[-1]["name"], "scheduled_status")
        ContextCompactor.groups(self.history)

    def test_interruption_after_tool_side_effect_closes_uncertain_batch(self):
        job = self.pending()
        client, _ = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "write",
                            name="write_file",
                            arguments='{"path":"output.txt","content":"ready"}',
                        ),
                        call("later", name="glob", arguments='{"pattern":"*.txt"}'),
                    ]
                )
            ]
        )
        execute = homebrew_harness.tools.dispatch.execute_tool

        def interrupt_after_execution(*args, **kwargs):
            execute(*args, **kwargs)
            raise RuntimeError("private detail")

        with (
            patch(
                "homebrew_harness.tools.dispatch.execute_tool",
                side_effect=interrupt_after_execution,
            ),
            self.assertRaises(RuntimeError),
        ):
            self.run_job(job, client)
        ContextCompactor.groups(self.history)
        self.assertEqual(len([m for m in self.history if m["role"] == "tool"]), 2)
        self.assertIn("uncertain", json.dumps(self.history))
        self.assertNotIn("private detail", json.dumps(self.history))

    def test_truncated_first_response_is_not_acknowledged_or_executed(self):
        job = self.pending()
        before = copy.deepcopy(self.history)
        client, _ = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "write",
                            name="write_file",
                            arguments='{"path":"output.txt","content":"ready"}',
                        )
                    ],
                    finish_reason="length",
                )
            ]
        )
        with self.assertRaises(homebrew_harness.runtime.errors.AgentError):
            self.run_job(job, client)
        self.assertEqual(self.history, before)
        self.assertEqual(self.scheduler.take_pending(), job)
        self.assertFalse((self.root / "output.txt").exists())

    def test_scheduled_bash_is_denied_without_reading_terminal_but_files_work(self):
        job = self.pending()
        client, _ = fake_client(
            [
                reply(
                    calls=[
                        call("bash", "printf forbidden", arguments=None),
                        call(
                            "write",
                            name="write_file",
                            arguments='{"path":"output.txt","content":"ready"}',
                        ),
                    ]
                ),
                reply("Bash denied; file written"),
            ]
        )
        with (
            patch(
                "homebrew_harness.security.permissions.sys.stdin.isatty",
                return_value=True,
            ),
            patch("builtins.input") as ask,
            patch("homebrew_harness.tools.shell.run_bash") as bash,
        ):
            self.run_job(job, client)
        ask.assert_not_called()
        bash.assert_not_called()
        result = next(m for m in self.history if m.get("tool_call_id") == "bash")
        self.assertEqual(json.loads(result["content"])["permission"], "deny")
        self.assertEqual((self.root / "output.txt").read_text(), "ready")

    def test_worker_threads_cannot_ask_for_terminal_input(self):
        results = []
        with (
            patch(
                "homebrew_harness.security.permissions.sys.stdin.isatty",
                return_value=True,
            ),
            patch("builtins.input") as ask,
        ):
            worker = threading.Thread(
                target=lambda: results.append(
                    permissions.check_permission("bash", {"command": "true"}, self.root)
                )
            )
            worker.start()
            worker.join(timeout=2)
        self.assertEqual(results[0].action, "deny")
        ask.assert_not_called()

    def test_shutdown_cancels_unexecuted_calls_and_keeps_tool_batch_complete(self):
        job = self.pending()
        stop = threading.Event()
        self.hooks.register_hook("PostToolUse", lambda event, output: stop.set())
        client, _ = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "write",
                            name="write_file",
                            arguments='{"path":"first.txt","content":"ready"}',
                        ),
                        call(
                            "later",
                            name="write_file",
                            arguments='{"path":"later.txt","content":"must not run"}',
                        ),
                    ]
                )
            ]
        )
        with self.assertRaises(homebrew_harness.runtime.errors.AgentError):
            self.run_job(job, client, stop_event=stop)
        self.assertTrue((self.root / "first.txt").exists())
        self.assertFalse((self.root / "later.txt").exists())
        ContextCompactor.groups(self.history)
        self.assertIn(
            "not executed",
            next(m for m in self.history if m.get("tool_call_id") == "later")[
                "content"
            ],
        )

    def test_child_cannot_invent_cron_tools(self):
        client, _ = fake_client(
            [
                reply(
                    calls=[
                        call(name, name=name, arguments="{}")
                        for name in CRON_TOOL_NAMES
                    ]
                ),
                reply("Unavailable"),
            ]
        )
        history = [{"role": "user", "content": "Focused task"}]
        homebrew_harness.runtime.loop.agent_loop(
            client,
            "test-model",
            history,
            self.root,
            subagent=True,
            capabilities=Capabilities.full(memory_mode="off"),
        )
        self.assertTrue(
            all(
                json.loads(m["content"])["error"] == "Unknown tool."
                for m in history
                if m["role"] == "tool"
            )
        )
        self.assertEqual(self.scheduler.list(), [])


class CronSessionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.scheduler = CronScheduler(self.root)
        self.moment = datetime(2026, 11, 3, 9)
        self.agent_lock = threading.Lock()
        capture = redirect_stderr(io.StringIO())
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def due(self):
        self.scheduler.schedule("* * * * *", "Work", recurring=False)
        self.scheduler.poll_due(self.moment)

    def test_import_does_not_create_scheduler_threads(self):
        script = "import threading, homebrew_harness.runtime.loop as agent, homebrew_harness.orchestration.cron_scheduler as cron_scheduler; print([t.name for t in threading.enumerate() if t.name.startswith('cron-')])"
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.assertEqual(result.stdout.strip(), "[]")

    def test_interactive_cli_dispatches_pending_work_and_extracts_only_human_memory(
        self,
    ):
        self.due()
        done = threading.Event()
        hooks = homebrew_harness.runtime.hooks.create_hooks()
        user_prompts = []
        hooks.register_hook(
            "UserPromptSubmit", lambda query, workdir: user_prompts.append(query)
        )

        def stopped(messages):
            if any(message.get("name") == "scheduled_task" for message in messages):
                done.set()

        hooks.register_hook("Stop", stopped)
        client, _ = fake_client(
            [
                reply("Human request finished"),
                reply(
                    calls=[
                        call(
                            "write",
                            name="write_file",
                            arguments='{"path":"scheduled.txt","content":"ready"}',
                        )
                    ]
                ),
                reply("Scheduled request finished"),
            ]
        )
        inputs = 0

        def user_input(_prompt):
            nonlocal inputs
            inputs += 1
            if inputs == 1:
                return "Human request"
            self.assertTrue(
                done.wait(timeout=3), "Scheduled CLI turn did not run while idle"
            )
            deadline = time.monotonic() + 3
            while CronScheduler(self.root).list() and time.monotonic() < deadline:
                threading.Event().wait(0.01)
            return "quit"

        config = {
            key: "test-config"
            for key in homebrew_harness.configuration.CONFIG_VARIABLES
        }
        with (
            patch.dict(os.environ, config),
            patch("homebrew_harness.cli.load_dotenv"),
            patch("homebrew_harness.runtime.hooks.create_hooks", return_value=hooks),
            patch("homebrew_harness.cli.OpenAI") as factory,
            patch("homebrew_harness.context.memory.MemoryRuntime") as memory,
            patch("builtins.input", side_effect=user_input),
            redirect_stdout(io.StringIO()),
        ):
            factory.return_value.__enter__.return_value = client
            memory.return_value.recall.return_value = ""
            self.assertEqual(
                homebrew_harness.cli.main(
                    ["--profile", "full"] + ["--cwd", str(self.root)]
                ),
                0,
            )
            memory.return_value.finish_turn.assert_called_once_with("Human request")
        self.assertEqual(user_prompts, ["Human request"])
        self.assertEqual((self.root / "scheduled.txt").read_text(), "ready")
        self.assertEqual(CronScheduler(self.root).list(), [])
