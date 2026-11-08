"""Independent persistent loops, typed controls and automatic Lead delivery."""

from __future__ import annotations
from tests.support.paths import PROJECT_ROOT

import copy
import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch

from homebrew_harness.runtime.loop_components import LoopComponents
from tests.support.tools import execute_fixture_tool
import homebrew_harness.cli
import homebrew_harness.configuration
import homebrew_harness.context.skill_loader
import homebrew_harness.execution.test_runner
import homebrew_harness.runtime.hooks
import homebrew_harness.runtime.prompts
import homebrew_harness.runtime.turns
import homebrew_harness.tools.catalog
import homebrew_harness.tools.registry
from homebrew_harness.context.compaction import ContextCompactor
from homebrew_harness.storage.message_bus import MessageBus
from homebrew_harness.security.permissions import (
    noninteractive_permissions,
    PermissionDecision,
)
from homebrew_harness.context.skill_loader import SkillLoader
from homebrew_harness.orchestration.task_store import TaskStore
from homebrew_harness.orchestration.teams import (
    TEAM_TOOL_NAMES,
    TeamManager,
    TeamTurnError,
    TeammateRuntime,
)
from tests.support.clients import call, fake_client, reply


class TeamFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.hooks = homebrew_harness.runtime.hooks.create_hooks()
        self.loader = SkillLoader(self.root / "skills")
        capture = redirect_stderr(io.StringIO())
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def wait(self, condition):
        deadline = time.monotonic() + 4
        while not condition() and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        self.assertTrue(condition(), "Expected team state was not reached")

    def manager(self, runner=None):
        manager = TeamManager(
            self.root, runner or Mock(return_value="Ready"), scan_interval=0.02
        )
        self.addCleanup(manager.close)
        return manager

    def member(self, manager, name="worker", *, require_plan=False):
        member = TeammateRuntime(
            manager, name, "fixture worker", "Focused task", require_plan
        )
        manager.members[name] = member
        task = manager.store.create("Work", "Write and verify the assigned output")
        member.bind(member.store.claim(task.id))
        return member

    def invoke(self, member, name, **arguments):
        block = (
            reply(calls=[call(name, name=name, arguments=json.dumps(arguments))])
            .choices[0]
            .message.tool_calls[0]
        )
        handlers = {
            **homebrew_harness.tools.registry.TOOL_HANDLERS,
            **member.handlers(),
        }
        return json.loads(
            execute_fixture_tool(
                block,
                self.root,
                hooks=self.hooks,
                handlers=handlers,
                team_member=member,
            )
        )


class MessageBusTests(TeamFixture):
    def test_invalid_names_cannot_register_messages(self):
        bus = MessageBus(self.root)
        for name in ("../outside", "", "/absolute"):
            with self.assertRaises(ValueError):
                bus.send("lead", name, "No write")
        self.assertFalse(bus.store.read()["inbox"])

    def test_atomic_concurrent_send_and_destructive_consumption(self):
        bus = MessageBus(self.root)
        errors = []

        def send(index):
            try:
                bus.send("worker", "lead", str(index))
            except Exception as error:
                errors.append(error)

        writers = [threading.Thread(target=send, args=(index,)) for index in range(12)]
        for writer in writers:
            writer.start()
        for writer in writers:
            writer.join(timeout=3)
        self.assertEqual(errors, [])
        records = bus.read_inbox("lead")
        self.assertEqual(
            {record["content"] for record in records},
            {str(index) for index in range(12)},
        )
        self.assertEqual(bus.read_inbox("lead"), [])
        self.assertFalse(bus.peek("lead"))

    def test_condition_wakes_for_message_and_other_consumers_do_not_share_context(self):
        bus = MessageBus(self.root)
        received = []
        reader = threading.Thread(
            target=lambda: received.extend(bus.wait_for_messages("worker", 2))
        )
        reader.start()
        bus.send("lead", "worker", "Work item")
        reader.join(timeout=3)
        self.assertEqual(received[0]["content"], "Work item")
        self.assertEqual(bus.read_inbox("other"), [])


class AssignmentAndProtocolTests(TeamFixture):
    def test_one_atomic_claim_across_threads_and_processes(self):
        task = TaskStore(self.root).create("Shared")
        owners = []
        barrier = threading.Barrier(2)

        def claim(name):
            barrier.wait(timeout=2)
            try:
                TaskStore(self.root, owner=name).claim(task.id)
                owners.append(name)
            except ValueError:
                pass

        workers = [threading.Thread(target=claim, args=(name,)) for name in ("a", "b")]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=3)
        self.assertEqual(len(owners), 1)
        script = """
import sys
from pathlib import Path
from homebrew_harness.orchestration.task_store import TaskStore
try:
    TaskStore(Path(sys.argv[1]), owner='other-process').claim(sys.argv[2])
except ValueError:
    print('blocked')
else:
    raise SystemExit(1)
"""
        result = subprocess.run(
            [sys.executable, "-c", script, str(self.root), task.id],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        self.assertEqual(result.stdout.strip(), "blocked")

    def test_owner_must_finish_current_task_before_claiming_next(self):
        store = TaskStore(self.root, owner="worker")
        first, second = store.create("First"), store.create("Second")
        store.claim(first.id)
        with self.assertRaises(ValueError):
            store.claim(second.id)
        self.assertIsNone(store.claim_next())
        store.complete(first.id)
        self.assertEqual(store.claim_next().id, second.id)

    def test_workspace_requires_assignment_and_sender_cannot_be_forged(self):
        manager = self.manager()
        member = TeammateRuntime(manager, "worker", "worker", "Focused", False)
        manager.members["worker"] = member
        self.assertIn(
            "error",
            self.invoke(member, "write_file", path="forbidden.txt", content="no"),
        )
        self.assertFalse((self.root / "forbidden.txt").exists())
        block = (
            reply(
                calls=[
                    call(
                        "forge",
                        name="send_message",
                        arguments='{"to":"lead","content":"x","from":"other"}',
                    )
                ]
            )
            .choices[0]
            .message.tool_calls[0]
        )
        self.assertIn(
            "error",
            json.loads(
                execute_fixture_tool(
                    block, self.root, handlers=member.handlers(), team_member=member
                )
            ),
        )

    def test_mailbox_and_git_files_cannot_be_overwritten_by_model_tools(self):
        manager = self.manager()
        member = self.member(manager)
        for path in (".mailboxes/lead.jsonl", ".git/config"):
            result = self.invoke(member, "write_file", path=path, content="forged")
            self.assertEqual(result["permission"], "deny")

    def test_required_pending_approved_plan_gate_and_turn_boundary_lease(self):
        manager = self.manager()
        member = self.member(manager, require_plan=True)
        task_id = member.assignment.id
        self.assertIn(
            "required",
            self.invoke(member, "write_file", path="output.txt", content="ready")[
                "error"
            ],
        )
        plan = self.invoke(
            member, "submit_plan", plan="Write output.txt, then read it and verify."
        )
        self.assertEqual(member.gate, "pending")
        self.assertIn("error", self.invoke(member, "complete_task", task_id=task_id))
        manager.review_plan(plan["request_id"], True)
        member.handle_messages(manager.bus.read_inbox(member.name))
        self.assertEqual(member.gate, "approved")
        self.assertNotIn(
            "error",
            self.invoke(member, "write_file", path="output.txt", content="ready"),
        )
        self.assertEqual(
            self.invoke(member, "complete_task", task_id=task_id)["task"]["status"],
            "completed",
        )
        self.assertNotIn("error", self.invoke(member, "read_file", path="output.txt"))
        member.finish_work_turn("Verified output")
        self.assertIsNone(member.assignment)
        self.assertIn("error", self.invoke(member, "read_file", path="output.txt"))
        self.assertEqual(
            [message["type"] for message in manager.consume_lead_inbox()],
            ["plan_approval_request", "result"],
        )

    def test_rejected_and_forged_plan_responses_do_not_unlock_changes(self):
        manager = self.manager()
        member = self.member(manager, require_plan=True)
        request = self.invoke(member, "submit_plan", plan="Plan")
        manager.bus.send(
            "peer",
            member.name,
            "fake",
            "plan_approval_response",
            {"request_id": request["request_id"], "approve": True},
        )
        member.handle_messages(manager.bus.read_inbox(member.name))
        self.assertEqual(member.gate, "pending")
        manager.review_plan(request["request_id"], False, "Revise the plan")
        member.handle_messages(manager.bus.read_inbox(member.name))
        self.assertEqual(member.gate, "rejected")
        self.assertIn(
            "error", self.invoke(member, "write_file", path="x.txt", content="no")
        )
        next_request = self.invoke(member, "submit_plan", plan="Revised plan")
        self.assertNotEqual(next_request["request_id"], request["request_id"])

    def test_approval_from_previous_assignment_is_ignored(self):
        manager = self.manager()
        member = self.member(manager, require_plan=True)
        request = self.invoke(member, "submit_plan", plan="Plan")
        manager.review_plan(request["request_id"], True)
        response = manager.bus.read_inbox(member.name)
        member.handle_messages(response)
        self.invoke(member, "complete_task", task_id=member.assignment.id)
        member.finish_work_turn("First complete")
        next_task = manager.store.create("Next")
        member.allowed_tasks = frozenset(
            {next_task.id}
        )  # Host authorizes the next assignment, not its old plan.
        member.claim(next_task.id, workdir=self.root)
        member.handle_messages(response)
        self.assertEqual(member.gate, "required")
        self.assertIn(
            "error", self.invoke(member, "write_file", path="x.txt", content="no")
        )
        with self.assertRaises(ValueError):
            manager.review_plan(request["request_id"], True)

    def test_new_plan_request_invalidates_pending_approval(self):
        manager = self.manager()
        member = self.member(manager, require_plan=True)
        request = self.invoke(member, "submit_plan", plan="Old plan")
        manager.request_plan(member.name, "Use another approach")
        self.assertEqual(member.gate, "required")
        with self.assertRaises(ValueError):
            manager.review_plan(request["request_id"], True)

    def test_plan_requirement_added_by_pre_hook_blocks_the_same_write(self):
        manager = self.manager()
        member = self.member(manager)
        self.hooks.register_hook(
            "PreToolUse",
            lambda event: manager.request_plan(member.name, "Review first") and None,
        )
        result = self.invoke(
            member, "write_file", path="must-not-write.txt", content="no"
        )
        self.assertIn("required", result["error"])
        self.assertFalse((self.root / "must-not-write.txt").exists())

    def test_default_member_dispatch_does_not_expose_graph_mutations(self):
        manager = self.manager()
        member = self.member(manager)
        block = (
            reply(
                calls=[
                    call(
                        "new", name="create_task", arguments='{"subject":"not allowed"}'
                    )
                ]
            )
            .choices[0]
            .message.tool_calls[0]
        )
        result = json.loads(execute_fixture_tool(block, self.root, team_member=member))
        self.assertEqual(result["error"], "Unknown tool.")

    def test_shutdown_matches_type_identity_and_request_id_once(self):
        manager = self.manager()
        member = self.member(manager)
        manager.bus.send(
            "peer", member.name, "fake", "shutdown_request", {"request_id": "req_fake"}
        )
        manager.bus.send(
            "lead",
            member.name,
            "invalid identifier",
            "shutdown_request",
            {"request_id": {}},
        )
        member.handle_messages(manager.bus.read_inbox(member.name))
        self.assertFalse(member.stop_event.is_set())
        request = manager.request_shutdown(member.name)
        member.handle_messages(manager.bus.read_inbox(member.name))
        self.assertTrue(member.stop_event.is_set())
        self.assertEqual(manager.protocols[request["request_id"]].status, "pending")
        manager.consume_lead_inbox()
        self.assertEqual(manager.protocols[request["request_id"]].status, "approved")
        manager.bus.send(
            member.name,
            "lead",
            "duplicate",
            "shutdown_response",
            {"request_id": request["request_id"], "approve": False},
        )
        manager.consume_lead_inbox()
        self.assertEqual(manager.protocols[request["request_id"]].status, "approved")

    def test_schema_limits_teammates_to_their_shared_task_operations(self):
        manager = self.manager()
        member = self.member(manager)
        forbidden = [
            "task",
            "create_task",
            "update_task",
            "spawn_teammate",
            "schedule_cron",
            "create_worktree",
            "request_shutdown",
        ]
        client, _ = fake_client(
            [
                reply(
                    calls=[call(name, name=name, arguments="{}") for name in forbidden]
                ),
                reply("Unavailable"),
            ]
        )
        homebrew_harness.runtime.turns.run_member_loop(
            member,
            client=client,
            model="test-model",
            hooks=self.hooks,
            skill_loader=self.loader,
        )
        results = [
            json.loads(message["content"])
            for message in member.messages
            if message["role"] == "tool"
        ]
        self.assertTrue(all(result["error"] == "Unknown tool." for result in results))
        self.assertEqual(
            {
                tool["function"]["name"]
                for tool in client.chat.completions.create.call_args_list[0].kwargs[
                    "tools"
                ]
            },
            {
                tool["function"]["name"]
                for tool in homebrew_harness.tools.catalog.TEAMMATE_TOOLS
            }
            - {
                "list_artifacts",
                "get_artifact",
                "publish_artifacts",
                *homebrew_harness.execution.test_runner.TEST_TOOL_NAMES,
            },
        )

    def test_worker_bash_approval_cannot_read_the_terminal(self):
        manager = self.manager()
        member = self.member(manager)
        with (
            patch(
                "homebrew_harness.security.permissions.sys.stdin.isatty",
                return_value=True,
            ),
            patch("builtins.input") as ask,
        ):
            with noninteractive_permissions():
                result = self.invoke(member, "bash", command="printf no")
        self.assertEqual(result["permission"], "deny")
        ask.assert_not_called()

    def test_lead_turn_pauses_claiming_until_dependencies_are_built(self):
        manager = self.manager()
        member = TeammateRuntime(manager, "worker", "worker", "Task", False)
        manager.members[member.name] = member
        manager.begin_lead_turn()
        first, second = manager.store.create("First"), manager.store.create("Second")
        with self.assertRaises(ValueError):
            member.claim(second.id, workdir=self.root)
        manager.store.update_dependencies(second.id, [first.id])
        manager.end_lead_turn()
        self.assertEqual(member.store.claim_next().id, first.id)


class PersistentLoopTests(TeamFixture):
    def make_runner(self, clients):
        return lambda member: homebrew_harness.runtime.turns.run_member_loop(
            member,
            client=clients[member.name],
            model="test-model",
            hooks=self.hooks,
            skill_loader=self.loader,
            max_turns=10,
        )

    def test_spawn_through_unified_dispatch_accepts_member_name_argument(self):
        manager = self.manager()
        task = manager.store.create("Assigned work")
        block = (
            reply(
                calls=[
                    call(
                        "spawn",
                        name="spawn_teammate",
                        arguments=json.dumps(
                            {
                                "name": "worker",
                                "role": "fixture worker",
                                "prompt": "Read assigned task",
                                "task_id": task.id,
                            }
                        ),
                    )
                ]
            )
            .choices[0]
            .message.tool_calls[0]
        )
        with patch(
            "homebrew_harness.security.permissions.ask_user",
            return_value=PermissionDecision("allow", "Fixture user approval"),
        ):
            result = json.loads(
                execute_fixture_tool(block, self.root, team_manager=manager)
            )
        self.assertEqual(result["name"], "worker")
        self.assertEqual(result["task_id"], task.id)
        self.wait(lambda: manager.members["worker"].status == "idle")

    def test_worktree_tool_dispatch_preserves_name_and_task_id(self):
        manager = self.manager()
        task = manager.store.create("Worktree work")
        block = (
            reply(
                calls=[
                    call(
                        "worktree",
                        name="create_worktree",
                        arguments=json.dumps(
                            {
                                "name": "checkout",
                                "task_id": task.id,
                            }
                        ),
                    )
                ]
            )
            .choices[0]
            .message.tool_calls[0]
        )
        with patch.object(
            manager.worktrees, "create", return_value={"worktree": "checkout"}
        ) as create:
            result = json.loads(
                execute_fixture_tool(block, self.root, team_manager=manager)
            )
        self.assertEqual(result["worktree"], "checkout")
        create.assert_called_once_with(name="checkout", task_id=task.id)

    def test_two_workers_run_concurrently_with_independent_contexts(self):
        barrier = threading.Barrier(2)
        entered = []
        clients, requests = {}, {}
        for name in ("a", "b"):
            client, snapshots = fake_client(
                [
                    reply(
                        calls=[
                            call(
                                "write-" + name,
                                name="write_file",
                                arguments=json.dumps(
                                    {"path": name + ".txt", "content": name}
                                ),
                            )
                        ]
                    ),
                    reply("Local finding " + name),
                ]
            )
            create = client.chat.completions.create.side_effect
            first = [True]

            def concurrent_create(_create=create, _name=name, _first=first, **kwargs):
                if _first[0]:
                    _first[0] = False
                    entered.append(_name)
                    barrier.wait(timeout=3)
                return _create(**kwargs)

            client.chat.completions.create.side_effect = concurrent_create
            clients[name], requests[name] = client, snapshots
        manager = self.manager(self.make_runner(clients))
        tasks = {
            name: manager.store.create(name + " task", name.upper() + "_ONLY_CONTEXT")
            for name in clients
        }
        for name in clients:
            manager.authorize_spawn(
                dict(
                    name=name,
                    role="worker",
                    prompt=name.upper() + "_ONLY_CONTEXT",
                    task_id=tasks[name].id,
                )
            )
            manager.spawn(
                name, "worker", name.upper() + "_ONLY_CONTEXT", task_id=tasks[name].id
            )
        self.wait(
            lambda: all(member.status == "idle" for member in manager.members.values())
        )
        self.assertEqual(set(entered), {"a", "b"})
        self.assertIn("A_ONLY_CONTEXT", json.dumps(requests["a"]))
        self.assertNotIn("B_ONLY_CONTEXT", json.dumps(requests["a"]))
        self.assertNotIn("A_ONLY_CONTEXT", json.dumps(requests["b"]))
        self.assertIsNot(manager.members["a"].compactor, manager.members["b"].compactor)
        self.assertEqual((self.root / "a.txt").read_text(), "a")
        self.assertEqual((self.root / "b.txt").read_text(), "b")

    def test_idle_worker_auto_claims_next_task_and_preserves_history(self):
        manager = self.manager()
        first, second = manager.store.create("First"), manager.store.create("Second")
        manager.store.update_dependencies(second.id, [first.id])
        responses = []
        for index, task in enumerate((first, second)):
            responses.extend(
                [
                    reply(
                        calls=[
                            call(
                                f"complete-{index}",
                                name="complete_task",
                                arguments=json.dumps({"task_id": task.id}),
                            )
                        ]
                    ),
                    reply("Verified phase " + str(index)),
                ]
            )
        client, snapshots = fake_client(responses)
        manager.run_member = self.make_runner({"worker": client})
        manager.authorize_spawn(
            dict(
                name="worker",
                role="worker",
                prompt="First task instructions",
                task_id=first.id,
            ),
            task_ids=(second.id,),
        )
        manager.spawn("worker", "worker", "First task instructions", task_id=first.id)
        self.wait(
            lambda: (
                manager.store.load(second.id).status == "completed"
                and manager.members["worker"].status == "idle"
            )
        )
        self.assertIn("Verified phase 0", json.dumps(snapshots[2]))
        self.assertIn("Auto-claimed", json.dumps(snapshots[2]))
        events = manager.consume_lead_inbox()
        self.assertEqual([event["type"] for event in events], ["result", "result"])
        self.assertFalse(manager.members["worker"].stop_event.is_set())

    def test_plan_submission_pauses_model_calls_until_approval_arrives(self):
        manager = self.manager()
        task = manager.store.create("Plan then write")
        client, _ = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "plan",
                            name="submit_plan",
                            arguments='{"plan":"Write output and verify"}',
                        )
                    ]
                ),
                reply(
                    calls=[
                        call(
                            "write",
                            name="write_file",
                            arguments='{"path":"approved.txt","content":"ready"}',
                        ),
                        call(
                            "done",
                            name="complete_task",
                            arguments=json.dumps({"task_id": task.id}),
                        ),
                    ]
                ),
                reply("Verified approved work"),
            ]
        )
        manager.run_member = self.make_runner({"worker": client})
        manager.authorize_spawn(
            dict(
                name="worker",
                role="worker",
                prompt="Submit plan first",
                task_id=task.id,
                require_plan=True,
            )
        )
        manager.spawn(
            "worker", "worker", "Submit plan first", task_id=task.id, require_plan=True
        )
        self.wait(lambda: manager.members["worker"].status == "waiting_approval")
        self.assertEqual(client.chat.completions.create.call_count, 1)
        member = manager.members["worker"]
        manager.review_plan(member.plan_request, True)
        self.wait(lambda: member.status == "idle")
        self.assertEqual((self.root / "approved.txt").read_text(), "ready")
        self.assertEqual(manager.store.load(task.id).status, "completed")

    def test_worker_error_releases_task_and_hides_private_provider_details(self):
        client, _ = fake_client([])
        client.chat.completions.create.side_effect = RuntimeError("private endpoint")
        manager = self.manager(self.make_runner({"worker": client}))
        task = manager.store.create("Work")
        manager.authorize_spawn(
            dict(name="worker", role="worker", prompt="Work", task_id=task.id)
        )
        manager.spawn("worker", "worker", "Work", task_id=task.id)
        self.wait(lambda: manager.members["worker"].status == "failed")
        self.wait(lambda: manager.store.load(task.id).status == "pending")
        events = manager.consume_lead_inbox()
        self.assertEqual(events[0]["type"], "error")
        self.assertNotIn("private endpoint", json.dumps(events))

    def test_failed_spawn_does_not_start_worker_or_leave_task_claimed(self):
        manager = self.manager()
        task = manager.store.create("Work")
        for name in ("lead", "agent", "../name"):
            with self.assertRaises(ValueError):
                manager.authorize_spawn(
                    dict(name=name, role="worker", prompt="Work", task_id=task.id)
                )
                manager.spawn(name, "worker", "Work", task_id=task.id)
        with (
            patch(
                "homebrew_harness.orchestration.teams.threading.Thread.start",
                side_effect=RuntimeError("thread failure"),
            ),
            self.assertRaises(RuntimeError),
        ):
            manager.authorize_spawn(
                dict(name="worker", role="worker", prompt="Work", task_id=task.id)
            )
            manager.spawn("worker", "worker", "Work", task_id=task.id)
        self.assertEqual(manager.store.load(task.id).status, "pending")
        self.assertEqual(manager.members, {})

    def test_close_waits_for_running_request_then_returns_unfinished_task(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        client, _ = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "late-write",
                            name="write_file",
                            arguments='{"path":"late.txt","content":"must not run"}',
                        )
                    ]
                )
            ]
        )
        create = client.chat.completions.create.side_effect

        def wait_for_shutdown(**kwargs):
            entered.set()
            release.wait(timeout=3)
            return create(**kwargs)

        client.chat.completions.create.side_effect = wait_for_shutdown
        manager = self.manager(self.make_runner({"worker": client}))
        task = manager.store.create("Work")
        manager.authorize_spawn(
            dict(name="worker", role="worker", prompt="Work", task_id=task.id)
        )
        manager.spawn("worker", "worker", "Work", task_id=task.id)
        self.assertTrue(entered.wait(timeout=2))
        closer = threading.Thread(target=manager.close)
        closer.start()
        self.wait(lambda: manager.members["worker"].stop_event.is_set())
        self.assertTrue(closer.is_alive())
        release.set()
        closer.join(timeout=3)
        self.assertFalse(closer.is_alive())
        self.assertFalse((self.root / "late.txt").exists())
        self.assertEqual(manager.store.load(task.id).status, "pending")
        self.assertEqual(manager.members["worker"].status, "stopped")


class LeadEventsTests(TeamFixture):
    def test_lead_receives_events_as_observations_with_no_human_input_hook(self):
        manager = self.manager()
        history = [
            {
                "role": "system",
                "content": homebrew_harness.runtime.prompts.build_system_prompt(
                    self.root,
                    skill_loader=homebrew_harness.context.skill_loader.SkillLoader(
                        self.root / "skills"
                    ),
                ),
            }
        ]
        client, snapshots = fake_client([reply("I received the teammate findings")])
        user_hook = Mock()
        self.hooks.register_hook("UserPromptSubmit", user_hook)
        events = [
            {
                "from": "worker",
                "to": "lead",
                "content": "Finding",
                "type": "result",
                "metadata": {},
                "ts": 0,
            }
        ]
        result = homebrew_harness.runtime.turns.run_team_turn(
            events,
            client=client,
            model="test-model",
            messages=history,
            workdir=self.root,
            components=LoopComponents(
                team_manager=manager,
                hooks=self.hooks,
                skill_loader=self.loader,
                compactor=ContextCompactor(client, "test-model", self.root),
            ),
        )
        self.assertEqual(result, "I received the teammate findings")
        self.assertEqual(snapshots[0][-1]["name"], "team_events")
        names = {
            tool["function"]["name"]
            for tool in client.chat.completions.create.call_args.kwargs["tools"]
        }
        self.assertEqual(
            names,
            {
                tool["function"]["name"]
                for tool in manager.capabilities.filter(
                    homebrew_harness.tools.catalog.TOOLS
                )
                if tool["function"]["name"]
                not in {
                    "connect_mcp",
                    "workflow",
                    "list_artifacts",
                    "get_artifact",
                    "publish_artifacts",
                }
                and tool["function"]["name"]
                not in homebrew_harness.execution.test_runner.TEST_TOOL_NAMES
            },
        )
        user_hook.assert_not_called()

    def test_failure_after_receipt_does_not_replay_lead_side_effects(self):
        manager = self.manager()
        client, _ = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "write",
                            name="write_file",
                            arguments='{"path":"lead.txt","content":"ready"}',
                        )
                    ]
                )
            ]
        )
        history, attempts = [], []
        create = client.chat.completions.create.side_effect

        def fail_second(**kwargs):
            attempts.append(1)
            if len(attempts) == 2:
                raise RuntimeError("private endpoint")
            return create(**kwargs)

        client.chat.completions.create.side_effect = fail_second
        with self.assertRaises(TeamTurnError) as failure:
            homebrew_harness.runtime.turns.run_team_turn(
                [],
                client=client,
                model="test-model",
                messages=history,
                workdir=self.root,
                components=LoopComponents(
                    team_manager=manager,
                    hooks=self.hooks,
                    skill_loader=self.loader,
                    compactor=ContextCompactor(client, "test-model", self.root),
                ),
            )
        self.assertFalse(failure.exception.retryable)
        self.assertEqual((self.root / "lead.txt").read_text(), "ready")
        self.assertEqual(history[-1]["name"], "team_status")
        ContextCompactor.groups(history)

    def test_cli_event_thread_runs_a_lead_turn_without_human_input(self):
        # A saved file event is delivered after startup while the terminal waits.
        MessageBus(self.root).send("worker", "lead", "Saved finding", "result")
        client, _ = fake_client([reply("Received saved finding")])
        done = threading.Event()
        self.hooks.register_hook("Stop", lambda messages: done.set())

        def user_input(_prompt):
            self.assertTrue(done.wait(timeout=3))
            return "quit"

        config = {
            name: "test-config"
            for name in homebrew_harness.configuration.CONFIG_VARIABLES
        }
        with (
            patch.dict("os.environ", config),
            patch("homebrew_harness.cli.load_dotenv"),
            patch("homebrew_harness.cli.OpenAI") as factory,
            patch(
                "homebrew_harness.runtime.hooks.create_hooks", return_value=self.hooks
            ),
            patch("builtins.input", side_effect=user_input),
            patch("homebrew_harness.runtime.hooks.submit_prompt") as human_event,
        ):
            factory.return_value.__enter__.return_value = client
            self.assertEqual(
                homebrew_harness.cli.main(
                    ["--profile", "full"]
                    + ["--memory-mode", "off", "--cwd", str(self.root)]
                ),
                0,
            )
        human_event.assert_not_called()
        self.assertEqual(client.chat.completions.create.call_count, 1)
