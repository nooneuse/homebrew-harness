"""Cross-component contracts: event dispatch, memory, task cwd and archives."""

from __future__ import annotations
from homebrew_harness.runtime.capabilities import Capabilities

import copy
import io
import json
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from tests.support.tools import execute_fixture_tool
import homebrew_harness.context.compaction
from homebrew_harness.execution.background import BackgroundManager
from homebrew_harness.runtime.session import HarnessSession
from homebrew_harness.security.permissions import PermissionDecision
from homebrew_harness.execution.model_protocol import ProviderError
from homebrew_harness.orchestration.teams import TeammateRuntime
from tests.support.clients import call, fake_client, reply


class HarnessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for manager in (redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO())):
            manager.__enter__()
            self.addCleanup(manager.__exit__, None, None, None)

    def session(self, responses, **kwargs):
        client, snapshots = fake_client(responses)
        session = HarnessSession(
            client,
            "test-model",
            self.root,
            dispatch_interval=0.01,
            poll_interval=0.01,
            retry_delay=0,
            **kwargs,
            capabilities=Capabilities.full(memory_mode="off"),
        )
        self.addCleanup(lambda: session.wait_closed(5))
        self.addCleanup(session.close)
        return session, client, snapshots

    def wait(self, predicate):
        deadline = time.monotonic() + 4
        while not predicate() and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        self.assertTrue(predicate(), "Integrated runtime did not reach expected state")

    def test_completed_background_job_wakes_idle_lead_without_another_user_prompt(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def command(*args, **kwargs):
            release.wait(timeout=3)
            return '{"exit_code":0,"stdout":"completed","stderr":""}'

        background = BackgroundManager(self.root, command)
        self.addCleanup(background.close)
        session, client, snapshots = self.session(
            [
                reply(
                    calls=[
                        call(
                            "start",
                            name="bash",
                            arguments='{"command":"fixture","run_in_background":true}',
                        )
                    ]
                ),
                reply("Started"),
                reply("Observed completion"),
            ],
            background_manager=background,
        )
        prompts = Mock()
        session.hooks.register_hook("UserPromptSubmit", prompts)
        with patch(
            "homebrew_harness.security.permissions.ask_user",
            return_value=PermissionDecision("allow", "Fixture authorization"),
        ):
            session.run_user("Start independent fixture work")
        self.assertEqual(client.chat.completions.create.call_count, 2)
        session.start()
        release.set()
        self.wait(
            lambda: (
                client.chat.completions.create.call_count == 3
                and any(
                    message.get("name") == "background_notification"
                    for message in session.messages
                )
            )
        )
        prompts.assert_called_once()
        self.assertEqual(
            sum(
                message.get("name") == "background_notification"
                for message in session.messages
            ),
            1,
        )
        rows = [
            json.loads(line) for line in session.trace.path.read_text().splitlines()
        ]
        self.assertTrue(any(row.get("source") == "background" for row in rows))

    def test_background_and_team_events_merge_while_cron_remains_a_separate_goal(self):
        session, client, snapshots = self.session(
            [reply("Observed both"), reply("Scheduled goal complete")]
        )
        session.user_request = "Original user objective"
        session.team.bus.send("worker", "lead", "Finding", "result")
        session.background.run_command = lambda *args, **kwargs: (
            '{"exit_code":0,"stdout":"done","stderr":""}'
        )
        session.background.start("fixture")
        for thread in session.background._threads:
            thread.join(timeout=2)
        session.cron.schedule(
            "* * * * *", "Scheduled independent objective", recurring=False
        )
        session.cron.poll_due(datetime(2026, 11, 3, 9))
        self.assertTrue(session.process_once())
        names = [message.get("name") for message in snapshots[0]]
        self.assertIn("team_events", names)
        self.assertIn("background_notification", names)
        self.assertNotIn("scheduled_task", names)
        self.assertTrue(session.process_once())
        self.assertTrue(
            any(message.get("name") == "scheduled_task" for message in snapshots[1])
        )
        self.assertEqual(session.cron.list(), [])
        self.assertEqual(client.chat.completions.create.call_count, 2)

    def test_automatic_turns_recall_memory_but_extract_only_actual_user_input(self):
        memory = Mock()
        memory.mode = "auto"
        memory.recall.return_value = '[{"key":"report_language","body":"Use Chinese"}]'
        session, _, snapshots = self.session(
            [reply("Human complete"), reply("Events complete"), reply("Cron complete")],
            memory_runtime=memory,
        )
        session.run_user("User objective")
        session.team.bus.send("worker", "lead", "Finding", "result")
        session.process_once()
        session.cron.schedule("* * * * *", "Scheduled objective", recurring=False)
        session.cron.poll_due(datetime(2026, 11, 3, 9))
        session.process_once()
        self.assertEqual(
            [item.args[0] for item in memory.recall.call_args_list],
            ["User objective", "User objective", "Scheduled objective"],
        )
        memory.finish_turn.assert_called_once_with("User objective")
        self.assertTrue(
            all(
                any(message.get("name") == "memory_context" for message in frame)
                for frame in snapshots
            )
        )

    def test_initial_observation_failure_preserves_pending_events_without_duplicate_history(
        self,
    ):
        session, client, snapshots = self.session([reply("Recovered")])
        session.user_request = "Keep the original objective"
        session.team.bus.send("worker", "lead", "Finding", "result")
        session.background.run_command = lambda *args, **kwargs: (
            '{"exit_code":0,"stdout":"done","stderr":""}'
        )
        session.background.start("fixture")
        for thread in session.background._threads:
            thread.join(timeout=2)
        before = copy.deepcopy(session.messages)
        create = client.chat.completions.create.side_effect
        fail = [True]

        def request(**kwargs):
            if fail[0]:
                fail[0] = False
                raise RuntimeError("private endpoint")
            return create(**kwargs)

        client.chat.completions.create.side_effect = request
        session.process_once()
        self.assertEqual(session.messages, before)
        self.assertTrue(session._pending_background and session._pending_team)
        session.process_once()
        self.assertEqual(
            sum(message.get("name") == "team_events" for message in session.messages), 1
        )
        self.assertEqual(
            sum(
                message.get("name") == "background_notification"
                for message in session.messages
            ),
            1,
        )
        self.assertEqual(session._pending_background, [])
        self.assertEqual(session._pending_team, [])

    def test_events_keep_original_user_request_for_compaction(self):
        session, _, _ = self.session([reply("Observed")])
        session.user_request = "Complete the originally requested refactor"
        requests = []
        prepare = session.compactor.prepare

        def capture(messages, request, *args, **kwargs):
            requests.append(request)
            return prepare(messages, request, *args, **kwargs)

        session.compactor.prepare = capture
        session.team.bus.send("worker", "lead", "Finding", "result")
        session.process_once()
        self.assertEqual(requests, [session.user_request])

    def test_event_turn_cannot_request_terminal_approval(self):
        session, _, _ = self.session(
            [reply(calls=[call("blocked", "printf fixture")]), reply("Denied")]
        )
        session.team.bus.send("worker", "lead", "Finding", "result")
        with (
            patch(
                "homebrew_harness.security.permissions.sys.stdin.isatty",
                return_value=True,
            ),
            patch("builtins.input") as approval,
        ):
            session.process_once()
        approval.assert_not_called()
        result = next(
            message
            for message in session.messages
            if message.get("tool_call_id") == "blocked"
        )
        self.assertEqual(json.loads(result["content"])["permission"], "deny")

    def test_cron_defers_during_manual_assignment_but_events_can_finish_it(self):
        session, _, _ = self.session([])
        task = session.team.store.create("Manual task")
        session.team.lead_claim(task.id, workdir=self.root)
        session.cron.schedule(
            "* * * * *", "Independent scheduled task", recurring=False
        )
        session.cron.poll_due(datetime(2026, 11, 3, 9))
        self.assertFalse(session.process_once())
        self.assertTrue(session.cron.has_pending())
        session.team.store.complete(task.id)
        session.team.end_lead_turn()
        client, _ = fake_client([reply("Cron finished")])
        session.client = client
        self.assertTrue(session.process_once())
        self.assertEqual(session.cron.list(), [])

    def test_waiting_user_and_busy_lock_prevent_automatic_delivery(self):
        session, client, _ = self.session([])
        session.team.bus.send("worker", "lead", "Finding", "result")
        session._user_waiting.set()
        self.assertFalse(session.process_once())
        session._user_waiting.clear()
        with session.lock:
            self.assertFalse(session.process_once())
        client.chat.completions.create.assert_not_called()
        self.assertTrue(session.team.bus.peek("lead"))

    def test_quota_failure_pauses_automatic_retries_and_preserves_pending_observations(
        self,
    ):
        session, client, _ = self.session([])
        session.team.bus.send("worker", "lead", "Finding", "result")
        client.chat.completions.create.side_effect = ProviderError("quota", status_code=403)
        self.assertTrue(session.process_once())
        self.assertTrue(session._quota_blocked)
        self.assertFalse(session.process_once())
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertTrue(session._pending_team)
        recovered, _ = fake_client([reply("User resumed"), reply("Events resumed")])
        session.client = recovered
        session.run_user("Quota restored; resume")
        self.assertFalse(session._quota_blocked)
        self.assertTrue(session.process_once())
        self.assertEqual(session._pending_team, [])


class WorktreeIntegrationTests(unittest.TestCase):
    session = HarnessTests.session
    wait = HarnessTests.wait

    def setUp(self):
        HarnessTests.setUp(self)
        self.git("init", "-q")
        (self.root / "README.md").write_text("Fixture")
        (self.root / ".gitignore").write_text(
            ".tasks/\n.worktrees/\n.mailboxes/\n.harness/\n.memory/\n"
        )
        self.git("add", "README.md", ".gitignore")
        self.git(
            "-c",
            "user.name=Harness Fixture",
            "-c",
            "user.email=harness@example.invalid",
            "commit",
            "-qm",
            "fixture",
        )

    def git(self, *args):
        return subprocess.run(
            ["git", *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )

    def assign(self, session):
        task = session.team.store.create(
            "Worktree task", "Operate only on the bound checkout"
        )
        result = session.team.worktrees.create("feature", task.id)
        session.team.lead_claim(task.id, workdir=self.root)
        return task, Path(result["path"])

    def test_subagent_inherits_lead_task_cwd_and_retains_independent_context(self):
        session, client, snapshots = self.session([])
        task, path = self.assign(session)
        scripted, _ = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "delegate",
                            name="task",
                            arguments='{"prompt":"Create child.txt and verify"}',
                        )
                    ]
                ),
                reply(
                    calls=[
                        call(
                            "child-write",
                            name="write_file",
                            arguments='{"path":"child.txt","content":"checkout"}',
                        )
                    ]
                ),
                reply("Child complete"),
                reply("Lead complete"),
            ]
        )
        session.client = scripted
        session.run_user("Operate in the assigned task directory")
        self.assertTrue((path / "child.txt").exists())
        self.assertFalse((self.root / "child.txt").exists())
        self.assertFalse(
            any(
                message.get("tool_call_id") == "child-write"
                for message in session.messages
            )
        )

    def test_worktree_background_blocks_completion_and_cleanup_until_finished(self):
        release, entered = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        cwd = []

        def command(*args, **kwargs):
            cwd.append(kwargs["workdir"])
            entered.set()
            release.wait(timeout=3)
            return '{"exit_code":0,"stdout":"done","stderr":""}'

        background = BackgroundManager(self.root, command)
        self.addCleanup(background.close)
        session, _, _ = self.session(
            [
                reply(
                    calls=[
                        call(
                            "start",
                            name="bash",
                            arguments='{"command":"fixture","run_in_background":true}',
                        )
                    ]
                ),
                reply("Started"),
            ],
            background_manager=background,
        )
        task, path = self.assign(session)
        with patch(
            "homebrew_harness.security.permissions.ask_user",
            return_value=PermissionDecision("allow", "Fixture authorization"),
        ):
            session.run_user("Start task validation in background")
        self.assertTrue(entered.wait(timeout=2))
        self.assertEqual(cwd, [path])
        with self.assertRaisesRegex(ValueError, "still running"):
            session.team.lead_complete(task.id, workdir=self.root)
        # Even an external host completion cannot remove an actively used checkout.
        session.team.store.complete(task.id)
        session.team.end_lead_turn()
        with self.assertRaisesRegex(ValueError, "leased"):
            session.team.remove_worktree("feature")
        release.set()
        for thread in background._threads:
            thread.join(timeout=2)
        self.assertEqual(session.team.remove_worktree("feature")["removed"], "feature")

    def test_archive_reference_survives_cwd_switch_and_other_contexts_cannot_read_it(
        self,
    ):
        session, _, _ = self.session([])
        task, path = self.assign(session)
        reference = session.compactor.store.save("tool-results", "LEAD_PRIVATE_ARCHIVE")
        block = (
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
                block,
                self.root,
                team_manager=session.team,
                archive_store=session.compactor.store,
            )
        )
        self.assertEqual(result["content"], "LEAD_PRIVATE_ARCHIVE")
        member = TeammateRuntime(session.team, "worker", "worker", "Work", False)
        session.team.members[member.name] = member
        other_task = session.team.store.create("Other work")
        member.bind(member.store.claim(other_task.id))
        own_store = homebrew_harness.context.compaction.ArchiveStore(
            self.root, "worker-fixture"
        )
        result = json.loads(
            execute_fixture_tool(
                block,
                self.root,
                team_member=member,
                archive_store=own_store,
                restrict_archives=True,
            )
        )
        self.assertIn("own generated", result["error"])

    def test_model_context_reports_current_task_cwd_and_replaces_old_metadata(self):
        session, _, snapshots = self.session([reply("Observed assigned directory")])
        task, path = self.assign(session)
        session.run_user("Work on the current task")
        metadata = [
            message
            for message in snapshots[0]
            if message.get("name") == "runtime_context"
        ]
        self.assertEqual(len(metadata), 1)
        self.assertEqual(json.loads(metadata[0]["content"])["execution_cwd"], str(path))
        self.assertEqual(json.loads(metadata[0]["content"])["task_id"], task.id)
        session.team.store.complete(task.id)
        session.team.end_lead_turn()
        client, next_snapshots = fake_client([reply("Observed base directory")])
        session.client = client
        session.run_user("Inspect the base workspace")
        metadata = [
            message
            for message in next_snapshots[0]
            if message.get("name") == "runtime_context"
        ]
        self.assertEqual(len(metadata), 1)
        self.assertEqual(
            json.loads(metadata[0]["content"])["execution_cwd"], str(self.root)
        )

    def test_repeated_current_worktree_prefix_is_rejected_before_creating_nested_files(
        self,
    ):
        session, _, _ = self.session([])
        task, path = self.assign(session)
        block = (
            reply(
                calls=[
                    call(
                        "wrong-path",
                        name="write_file",
                        arguments='{"path":".worktrees/feature/nested.txt","content":"no"}',
                    )
                ]
            )
            .choices[0]
            .message.tool_calls[0]
        )
        result = json.loads(
            execute_fixture_tool(block, self.root, team_manager=session.team)
        )
        self.assertIn("already this worktree", result["error"])
        self.assertFalse((path / ".worktrees").exists())

    def test_completed_task_cannot_start_new_background_work(self):
        session, _, _ = self.session([])
        task, path = self.assign(session)
        session.team.store.complete(task.id)
        block = (
            reply(
                calls=[
                    call(
                        "late",
                        name="bash",
                        arguments='{"command":"fixture","run_in_background":true}',
                    )
                ]
            )
            .choices[0]
            .message.tool_calls[0]
        )
        with (
            patch(
                "homebrew_harness.security.permissions.ask_user",
                return_value=PermissionDecision("allow", "Fixture authorization"),
            ),
            patch.object(session.background, "start") as start,
        ):
            result = json.loads(
                execute_fixture_tool(
                    block,
                    self.root,
                    team_manager=session.team,
                    background_manager=session.background,
                )
            )
        self.assertIn("after completing", result["error"])
        start.assert_not_called()
