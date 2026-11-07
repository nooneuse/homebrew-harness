from tests.support.paths import PROJECT_ROOT

"""Regression cases for intent, authorization, live ownership and async exit."""
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
from pathlib import Path
from unittest.mock import patch

from tests.support.tools import execute_fixture_tool
import homebrew_harness.context.compaction
import homebrew_harness.runtime.errors
import homebrew_harness.runtime.hooks
import homebrew_harness.runtime.loop
import homebrew_harness.tools.files
import homebrew_harness.security.permissions as permissions
from homebrew_harness.execution.background import BackgroundManager
from homebrew_harness.context.compaction import CompactionError, ContextCompactor
from homebrew_harness.security.data_boundary import (
    model_request,
    register_private_values,
)
from homebrew_harness.execution.execution_leases import (
    ExecutionLeases,
    file_update_lease,
)
from homebrew_harness.runtime.session import HarnessSession
from homebrew_harness.orchestration.goal_loop import GoalEvaluation
from homebrew_harness.security.safe_files import SafeFile
from homebrew_harness.context.task_intent import TaskIntent
from homebrew_harness.orchestration.task_store import TaskStore
from homebrew_harness.orchestration.teams import TeamManager, TeammateRuntime
from tests.support.clients import call, fake_client, reply


class Fixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for redirect in (
            redirect_stderr(io.StringIO()),
            redirect_stdout(io.StringIO()),
        ):
            redirect.__enter__()
            self.addCleanup(redirect.__exit__, None, None, None)

    def session(self, responses=(), **kwargs):
        client, snapshots = fake_client(responses)
        session = HarnessSession(
            client,
            "test-model",
            self.root,
            dispatch_interval=0.01,
            shutdown_timeout=0.2,
            **kwargs,
            capabilities=Capabilities.full(memory_mode="off"),
        )
        self.addCleanup(lambda: session.wait_closed(5))
        self.addCleanup(session.close)
        return session, client, snapshots

    def tool(self, tool_name, *, manager=None, **arguments):
        block = (
            reply(
                calls=[call("fixture", name=tool_name, arguments=json.dumps(arguments))]
            )
            .choices[0]
            .message.tool_calls[0]
        )
        return json.loads(execute_fixture_tool(block, self.root, team_manager=manager))


class IntentTests(Fixture):
    def test_three_system_messages_and_continue_cannot_drop_original_constraints(self):
        compactor = ContextCompactor(
            None, "test", self.root, trigger_messages=20, target_messages=10
        )
        history = [
            {"role": "system", "name": name, "content": name}
            for name in ("policy", "runtime_context", "memory_context")
        ]
        history.append(
            {
                "role": "user",
                "content": "Refactor parser; do not edit tests; never commit.",
            }
        )
        history.extend(
            {"role": "assistant", "content": str(index)} for index in range(30)
        )
        prepared = compactor.prepare(history, "continue")
        pinned = next(
            message for message in prepared if message.get("name") == "task_intent"
        )
        self.assertIn("do not edit tests; never commit", pinned["content"])
        self.assertEqual(
            sum(message.get("name") == "task_intent" for message in prepared), 1
        )
        self.assertEqual(
            sum(message.get("role") == "system" for message in prepared), 4
        )
        self.assertEqual(len(history), 34)

    def test_amendment_survives_unreliable_summary_without_extra_model_calls(self):
        session, client, snapshots = self.session(
            [reply("First"), reply("Second"), reply("Third")]
        )
        session.run_user("Implement parser; preserve public API")
        session.run_user("Also keep the dependency versions unchanged")
        session.run_user("继续")
        pinned = next(
            message for message in snapshots[-1] if message.get("name") == "task_intent"
        )
        self.assertIn("preserve public API", pinned["content"])
        self.assertIn("dependency versions unchanged", pinned["content"])
        self.assertEqual(session.user_request, "Implement parser; preserve public API")
        self.assertEqual(client.chat.completions.create.call_count, 3)

    def test_explicit_new_task_resets_old_constraints_and_history(self):
        session, _, snapshots = self.session([reply("First"), reply("Second")])
        session.run_user("OLD_TASK forbid modification")
        session.run_user("/task NEW_TASK generate an example")
        self.assertNotIn("OLD_TASK", json.dumps(snapshots[-1]))
        self.assertEqual(session.intent.original, "NEW_TASK generate an example")

    def test_auxiliary_goal_does_not_replace_original_work_constraints(self):
        session, _, snapshots = self.session([reply("Work"), reply("summary")])
        session.run_user("Refactor without editing tests")
        session.goal.evaluator = type(
            "Evaluator",
            (),
            {
                "evaluate": lambda self, packet: GoalEvaluation(
                    True, "Text supplied", evidence_ids=("final",)
                )
            },
        )()
        session.run_user("/goal Answer includes summary")
        self.assertIn(
            "without editing tests",
            next(
                message["content"]
                for message in snapshots[-1]
                if message.get("name") == "task_intent"
            ),
        )
        self.assertEqual(session.goal.state["status"], "achieved")

    def test_child_receives_original_constraints_but_not_parent_tool_history(self):
        (self.root / "source.txt").write_text("PRIVATE_PARENT_TOOL_HISTORY")
        session, _, snapshots = self.session(
            [
                reply(
                    calls=[
                        call(
                            "delegate",
                            name="task",
                            arguments='{"prompt":"Explain the parser"}',
                        )
                    ]
                ),
                reply("Child answer"),
                reply("Lead answer"),
            ]
        )
        session.run_user("Explain parser; never modify source files")
        child = snapshots[1]
        self.assertIn("never modify source files", json.dumps(child))
        self.assertNotIn("PRIVATE_PARENT_TOOL_HISTORY", json.dumps(child))

    def test_user_amendment_is_saved_before_provider_failure(self):
        session, client, _ = self.session([reply("First")])
        session.run_user("Refactor without editing tests")
        task = session.team.store.create("Work")
        session.team.lead_claim(task.id, workdir=self.root)
        client.chat.completions.create.side_effect = RuntimeError("fixture failure")
        with self.assertRaises(RuntimeError):
            session.run_user("Also preserve dependencies")
        saved = session.team.store.load(task.id).task_intent
        self.assertIn("Also preserve dependencies", saved["amendments"])

    def test_active_teammate_receives_later_human_constraints(self):
        session, _, _ = self.session([reply("First"), reply("Second")])
        session.run_user("Refactor without editing tests")
        member = TeammateRuntime(
            session.team, "worker", "reader", "Read the parser", False
        )
        session.team.members[member.name] = member
        task = session.team.store.create("Read parser")
        member.bind(member.store.claim(task.id))
        session.run_user("Also preserve dependencies")
        member.before_model()
        inherited = next(
            message
            for message in member.messages
            if message.get("name") == "inherited_task_constraints"
        )
        self.assertIn("without editing tests", inherited["content"])
        self.assertIn("Also preserve dependencies", inherited["content"])
        member.status, member.assignment = "idle", None
        with self.assertRaises(ValueError):
            session.run_user("/task Unrelated task")

    def test_standalone_task_store_delegates_with_its_existing_workspace_lease(self):
        store = TaskStore(self.root)
        self.addCleanup(store.close)
        task = store.create("Delegated work")
        store.claim(task.id)
        client, _ = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "delegate",
                            name="task",
                            arguments='{"prompt":"Write child.txt"}',
                        )
                    ]
                ),
                reply(
                    calls=[
                        call(
                            "child-write",
                            name="write_file",
                            arguments='{"path":"child.txt","content":"ready"}',
                        )
                    ]
                ),
                reply("Child done"),
                reply("Lead done"),
            ]
        )
        history = [
            {"role": "user", "content": "Write child.txt without modifying tests"}
        ]
        self.assertEqual(
            homebrew_harness.runtime.loop.agent_loop(
                client,
                "test",
                history,
                self.root,
                task_store=store,
                capabilities=Capabilities.full(memory_mode="off"),
            ),
            "Lead done",
        )
        self.assertEqual((self.root / "child.txt").read_text(), "ready")

    def test_budget_rejects_overlarge_constraints_instead_of_summarizing_them_away(
        self,
    ):
        compactor = ContextCompactor(None, "test", self.root, context_chars=1500)
        compactor.intent.update("Constraint " * 500)
        with self.assertRaises(CompactionError):
            compactor.prepare([{"role": "user", "content": "continue"}], "continue")


class FileBoundaryTests(Fixture):
    def test_parent_swap_after_snapshot_cannot_write_outside(self):
        (self.root / "parent").mkdir()
        outside = self.root.parent / (self.root.name + "-outside")
        outside.mkdir()
        self.addCleanup(lambda: outside.rmdir())
        original = SafeFile.publish

        def swapped(target, *args):
            (self.root / "parent").rename(self.root / "detached")
            (self.root / "parent").symlink_to(outside, target_is_directory=True)
            return original(target, *args)

        with patch.object(SafeFile, "publish", swapped), self.assertRaises(ValueError):
            homebrew_harness.tools.files.run_write(
                "parent/new.txt", "must remain contained", self.root
            )
        self.assertFalse((outside / "new.txt").exists())
        self.assertFalse((self.root / "detached/new.txt").exists())

    def test_replaced_workspace_root_cannot_redirect_file_write(self):
        workspace, outside = self.root / "workspace", self.root / "outside"
        workspace.mkdir()
        outside.mkdir()
        selected = workspace.resolve()
        workspace.rename(self.root / "detached-workspace")
        workspace.symlink_to(outside, target_is_directory=True)
        with self.assertRaises((OSError, ValueError)):
            homebrew_harness.tools.files.run_write("file.txt", "no escape", selected)
        self.assertFalse((outside / "file.txt").exists())
        self.assertFalse((outside / ".harness").exists())

    def test_final_symlink_swap_never_overwrites_its_target(self):
        target = self.root / "target.txt"
        target.write_text("protected")
        (self.root / "file.txt").write_text("old")
        original = SafeFile.publish

        def swapped(file, *args):
            (self.root / "file.txt").unlink()
            (self.root / "file.txt").symlink_to(target)
            return original(file, *args)

        with patch.object(SafeFile, "publish", swapped), self.assertRaises(OSError):
            homebrew_harness.tools.files.run_edit("file.txt", "old", "new", self.root)
        self.assertEqual(target.read_text(), "protected")

    def test_concurrent_editor_change_is_not_overwritten(self):
        path = self.root / "file.txt"
        path.write_text("old")
        original = SafeFile.publish

        def changed(file, *args):
            path.write_text("editor's later version")
            return original(file, *args)

        with (
            patch.object(SafeFile, "publish", changed),
            self.assertRaisesRegex(ValueError, "changed"),
        ):
            homebrew_harness.tools.files.run_edit("file.txt", "old", "new", self.root)
        self.assertEqual(path.read_text(), "editor's later version")

    def test_failed_atomic_publication_keeps_original_and_cleans_temporary(self):
        path = self.root / "file.txt"
        path.write_text("old")
        with (
            patch(
                "homebrew_harness.security.safe_files.os.replace",
                side_effect=OSError("fixture"),
            ),
            self.assertRaises(OSError),
        ):
            homebrew_harness.tools.files.run_write("file.txt", "new", self.root)
        self.assertEqual(path.read_text(), "old")
        self.assertEqual(list(self.root.glob(".harness-write-*")), [])

    def test_new_file_publication_is_exclusive(self):
        with SafeFile(self.root, "new.txt") as file:
            (self.root / "new.txt").write_text("concurrent creation")
            with self.assertRaises(ValueError):
                file.publish(b"overwrite", None, 0o644)
        self.assertEqual((self.root / "new.txt").read_text(), "concurrent creation")

    def test_cooperative_writers_use_exclusive_file_lock(self):
        (self.root / "file.txt").write_text("old")
        with (
            file_update_lease(self.root, "file.txt"),
            self.assertRaisesRegex(ValueError, "live host"),
        ):
            homebrew_harness.tools.files.run_edit("file.txt", "old", "new", self.root)
        self.assertEqual((self.root / "file.txt").read_text(), "old")

    def test_credential_paths_and_internal_aliases_are_not_exposed(self):
        (self.root / ".env").write_text("fixture credential")
        (self.root / "alias").symlink_to(self.root / ".env")
        for path in (".env", "alias", "nested/.ssh/key", "nested/private.key"):
            self.assertEqual(self.tool("read_file", path=path)["permission"], "deny")
        (self.root / ".env.example").write_text("configuration placeholder")
        self.assertEqual(
            self.tool("read_file", path=".env.example")["content"],
            "configuration placeholder",
        )
        self.assertNotIn(".env", self.tool("glob", pattern="*")["paths"])

    def test_read_cannot_follow_parent_swapped_before_open(self):
        (self.root / "parent").mkdir()
        (self.root / "parent/file.txt").write_text("inside")
        (self.root / "other").mkdir()
        (self.root / "other/file.txt").write_text("unrelated")
        with SafeFile(self.root, "parent/file.txt") as target:
            (self.root / "parent").rename(self.root / "detached")
            (self.root / "parent").symlink_to(
                self.root / "other", target_is_directory=True
            )
            with self.assertRaises(ValueError):
                with target.open_read():
                    self.fail("A replaced parent must not be readable")

    def test_fifo_read_is_rejected_without_blocking(self):
        os.mkfifo(self.root / "fifo")
        started = time.monotonic()
        with self.assertRaises(ValueError):
            homebrew_harness.tools.files.run_read("fifo", self.root)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_atomic_edit_preserves_executable_mode(self):
        path = self.root / "script.py"
        path.write_text("old")
        path.chmod(0o751)
        homebrew_harness.tools.files.run_edit("script.py", "old", "new", self.root)
        self.assertEqual(path.stat().st_mode & 0o777, 0o751)


class AuthorizationTests(Fixture):
    def test_spawn_requires_approval_and_worker_cannot_request_it(self):
        manager = TeamManager(self.root, lambda member: "done")
        self.addCleanup(manager.close)
        with permissions.noninteractive_permissions():
            result = self.tool(
                "spawn_teammate",
                manager=manager,
                name="worker",
                role="reader",
                prompt="Read only",
            )
        self.assertEqual(result["permission"], "deny")
        self.assertEqual(manager.members, {})
        with self.assertRaisesRegex(ValueError, "approval_required"):
            manager.spawn("worker", "reader", "Read only")

    def test_exact_host_approval_cannot_authorize_changed_prompt_or_replay(self):
        manager = TeamManager(self.root, lambda member: "done")
        self.addCleanup(manager.close)
        manager.authorize_spawn(dict(name="worker", role="reader", prompt="Read only"))
        with self.assertRaisesRegex(ValueError, "approval_required"):
            manager.spawn("worker", "reader", "Write files")
        manager.spawn("worker", "reader", "Read only")
        with self.assertRaisesRegex(ValueError, "approval_required"):
            manager.spawn("worker", "reader", "Read only")
        self.assertEqual(len(manager.approval_history), 1)

    def test_approved_unified_call_starts_only_one_teammate(self):
        manager = TeamManager(self.root, lambda member: "done")
        self.addCleanup(manager.close)
        with patch(
            "homebrew_harness.security.permissions.ask_user",
            return_value=permissions.PermissionDecision(
                "allow", "Fixture human approval"
            ),
        ) as ask:
            result = self.tool(
                "spawn_teammate",
                manager=manager,
                name="worker",
                role="reader",
                prompt="Read only",
            )
        self.assertEqual(result["name"], "worker")
        ask.assert_called_once()
        self.assertEqual(len(manager.approval_history), 1)

    def test_model_payload_and_archive_are_redacted_without_printing_results(self):
        private = "fixture-private-credential-value"
        register_private_values(private)
        client, snapshots = fake_client([reply("done")])
        model_request(
            client, model="test", messages=[{"role": "user", "content": private}]
        )
        self.assertNotIn(private, json.dumps(snapshots))
        store = homebrew_harness.context.compaction.ArchiveStore(self.root)
        path = store.save("tool-results", private)
        self.assertNotIn(private, (self.root / path).read_text())
        capture = io.StringIO()
        with redirect_stderr(capture):
            homebrew_harness.runtime.hooks.tool_result_hook(
                homebrew_harness.runtime.hooks.ToolEvent(
                    "read_file", {}, self.root, "fixture"
                ),
                "non-secret result body",
            )
        self.assertNotIn("non-secret result body", capture.getvalue())

    def test_privileged_command_wrappers_still_receive_hard_denial(self):
        for command in (
            "sudo true",
            "command sudo true",
            "exec /usr/bin/sudo true",
            "env sudo true",
        ):
            self.assertIsNotNone(permissions.check_deny_list(command))


class LeaseTests(Fixture):
    def store(self, **kwargs):
        store = TaskStore(self.root, **kwargs)
        self.addCleanup(store.close)
        return store

    def test_same_owner_live_instance_is_not_owned_or_resumable(self):
        first, second = self.store(), self.store()
        task = first.create("Work")
        first.claim(task.id)
        self.assertNotEqual(first.instance, second.instance)
        self.assertIsNone(second.owned())
        with self.assertRaisesRegex(ValueError, "live host"):
            second.resume(task.id)
        with self.assertRaises(ValueError):
            second.complete(task.id)
        second.release(task.id)
        self.assertEqual(first.load(task.id).status, "in_progress")

    def test_explicit_resume_restores_intent_after_old_host_closes(self):
        first, second = self.store(), self.store()
        first.intent = TaskIntent("Build without editing tests")
        first.intent.update("Keep dependencies unchanged")
        task = first.create("Work")
        first.claim(task.id)
        first.close()
        self.assertIsNone(second.owned())
        second.intent = TaskIntent()
        second.resume(task.id)
        self.assertEqual(second.intent.to_dict(), first.intent.to_dict())
        second.complete(task.id)

    def test_resume_keeps_new_user_amendments_as_well_as_saved_constraints(self):
        first, second = self.store(), self.store()
        first.intent = TaskIntent("Build without editing tests")
        task = first.create("Work")
        first.claim(task.id)
        first.close()
        second.intent = TaskIntent("Resume this task; preserve the public API")
        second.resume(task.id)
        text = second.intent.message()["content"]
        self.assertIn("without editing tests", text)
        self.assertIn("preserve the public API", text)

    def test_task_summary_distinguishes_live_local_and_recovery_required(self):
        first, second = self.store(), self.store()
        task = first.create("Work")
        first.claim(task.id)
        local = json.loads(first.run_tool("list_tasks", workdir=self.root))["tasks"][0]
        other = json.loads(second.run_tool("list_tasks", workdir=self.root))["tasks"][0]
        self.assertTrue(local["resumable"])
        self.assertFalse(other["resumable"])
        self.assertTrue(other["recoveryRequired"])

    def test_different_tasks_cannot_use_same_workspace_from_independent_hosts(self):
        first, second = self.store(), self.store(owner="worker")
        a, b = first.create("A"), first.create("B")
        first.claim(a.id)
        with self.assertRaisesRegex(ValueError, "live host"):
            second.claim(b.id)
        self.assertEqual(first.load(b.id).status, "pending")
        first.complete(a.id)
        second.claim(b.id)

    def test_failed_claim_rolls_back_all_execution_locks(self):
        first, second = self.store(), self.store()
        task = first.create("A")
        with (
            patch.object(first, "_write", side_effect=OSError("fixture")),
            self.assertRaises(OSError),
        ):
            first.claim(task.id)
        self.assertIsNone(first.owned())
        second.claim(task.id)

    def test_real_process_crash_releases_os_lock_for_explicit_recovery(self):
        store = self.store()
        task = store.create("Recover")
        script = "from pathlib import Path; from homebrew_harness.orchestration.task_store import TaskStore; import os,sys; s=TaskStore(Path(sys.argv[1])); s.claim(sys.argv[2]); os._exit(0)"
        subprocess.run(
            [sys.executable, "-c", script, str(self.root), task.id],
            cwd=PROJECT_ROOT,
            timeout=10,
            check=True,
            capture_output=True,
        )
        self.assertEqual(store.load(task.id).status, "in_progress")
        self.assertIsNone(store.owned())
        store.resume(task.id)
        store.complete(task.id)

    def test_session_lease_blocks_second_lead_and_releases_on_close(self):
        first, _, _ = self.session([reply("first")])
        second, _, _ = self.session([reply("second")])
        first.run_user("First task")
        with self.assertRaisesRegex(ValueError, "live host"):
            second.run_user("Second task")
        first.close()
        self.assertEqual(second.run_user("Second task"), "second")


class AsyncExitTests(Fixture):
    def test_one_shot_waits_for_background_then_delivers_final_observation(self):
        release = threading.Event()

        def command(*args, **kwargs):
            release.wait(2)
            return '{"exit_code":0,"stdout":"verified","stderr":""}'

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
                reply("Accepted"),
                reply("Verified completion"),
            ],
            background_manager=background,
            mode="one_shot",
        )
        with patch(
            "homebrew_harness.security.permissions.ask_user",
            return_value=permissions.PermissionDecision("allow", "Fixture approval"),
        ):
            session.run_user("Run a slow check")
        release.set()
        self.assertEqual(session.drain(timeout=2), 0)
        self.assertEqual(client.chat.completions.create.call_count, 3)
        self.assertTrue(
            any(
                message.get("name") == "background_notification"
                for message in snapshots[-1]
            )
        )
        self.assertEqual(background.snapshot()[0]["status"], "completed")

    def test_failed_background_never_reports_success(self):
        session, _, _ = self.session([reply("Observed failure")], mode="one_shot")
        session.background.run_command = lambda *args, **kwargs: (
            '{"exit_code":9,"stdout":"","stderr":"failed"}'
        )
        session.background.start("fixture")
        self.assertEqual(session.drain(timeout=2), 1)

    def test_drain_timeout_reports_pending_then_cancels_command(self):
        release = threading.Event()
        self.addCleanup(release.set)
        session, _, _ = self.session(mode="one_shot")
        session.background.run_command = lambda *args, **kwargs: (
            (release.wait(2) or True) and '{"exit_code":0}'
        )
        session.background.start("fixture")
        self.assertEqual(session.drain(timeout=0.02), 2)
        session.close()
        self.assertEqual(session.background.snapshot()[0]["status"], "cancelled")

    def test_wait_deadline_includes_lock_acquisition(self):
        session, _, _ = self.session()
        session.lock.acquire()
        try:
            started = time.monotonic()
            self.assertIn("timeout", session.wait_for_goal(timeout=0.02))
            self.assertLess(time.monotonic() - started, 0.2)
        finally:
            session.lock.release()

    def test_drain_deadline_includes_team_lock_acquisition(self):
        session, _, _ = self.session(mode="one_shot")
        session.team.acquire_session()
        locked, release = threading.Event(), threading.Event()

        def hold():
            with session.team.lock:
                locked.set()
                release.wait(2)

        worker = threading.Thread(target=hold)
        worker.start()
        self.assertTrue(locked.wait(1))
        started = time.monotonic()
        try:
            # Test pending-work inspection independently from the dispatcher.
            with patch.object(session, "start"):
                self.assertEqual(session.drain(timeout=0.02), 2)
            self.assertLess(time.monotonic() - started, 0.2)
        finally:
            release.set()
            worker.join(1)

    def test_ordinary_auto_failure_stops_without_replaying_model_request(self):
        session, client, _ = self.session(mode="one_shot")
        client.chat.completions.create.side_effect = RuntimeError(
            "fixture provider failure"
        )
        session.team.bus.send("worker", "lead", "Finding", "result")
        self.assertEqual(session.drain(timeout=2), 1)
        self.assertFalse(session.process_once())
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertTrue(session._pending_team)

    def test_unfinished_lead_task_returns_pending_without_waiting_for_an_event(self):
        session, _, _ = self.session(mode="one_shot")
        task = session.team.store.create("Unfinished")
        session.team.lead_claim(task.id, workdir=self.root)
        started = time.monotonic()
        self.assertEqual(session.drain(timeout=2), 2)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_standalone_loop_does_not_expose_unmanaged_background_mode(self):
        client, _ = fake_client([reply("Done")])
        homebrew_harness.runtime.loop.agent_loop(
            client,
            "test",
            [{"role": "user", "content": "Read"}],
            self.root,
            capabilities=Capabilities.full(memory_mode="off"),
        )
        tool = next(
            tool
            for tool in client.chat.completions.create.call_args.kwargs["tools"]
            if tool["function"]["name"] == "bash"
        )
        self.assertNotIn(
            "run_in_background", tool["function"]["parameters"]["properties"]
        )

    def test_close_is_bounded_but_retains_active_worker_lease(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        session, client, _ = self.session(
            [
                reply(
                    calls=[
                        call(
                            "late",
                            name="write_file",
                            arguments='{"path":"late.txt","content":"no"}',
                        )
                    ]
                )
            ]
        )
        create = client.chat.completions.create.side_effect

        def blocked(**kwargs):
            entered.set()
            release.wait(3)
            return create(**kwargs)

        client.chat.completions.create.side_effect = blocked
        errors = []

        def run():
            try:
                session.run_user("Work")
            except homebrew_harness.runtime.errors.AgentError:
                errors.append("stopped")

        worker = threading.Thread(target=run)
        worker.start()
        self.assertTrue(entered.wait(2))
        started = time.monotonic()
        session.close()
        self.assertLess(time.monotonic() - started, 0.8)
        contender = ExecutionLeases(self.root)
        with self.assertRaises(ValueError):
            contender.acquire("session")
        release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, ["stopped"])
        self.assertTrue(session.wait_closed(timeout=2))
        self.assertFalse((self.root / "late.txt").exists())
