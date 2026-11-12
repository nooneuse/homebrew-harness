"""Automatic input ownership exercises real Session and SQLite boundaries."""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from homebrew_harness.execution.model_gateway import ModelGateway
from homebrew_harness.execution.model_registry import ModelRegistry
from homebrew_harness.orchestration.workflows import default_registry
from homebrew_harness.runtime.capabilities import Capabilities
from homebrew_harness.runtime.interaction_config import InteractionConfig
from homebrew_harness.runtime.interaction_host import InteractionHost
from homebrew_harness.runtime.interaction_state import InteractionState
from homebrew_harness.runtime.session import HarnessSession
from homebrew_harness.runtime.task_routing import TaskRoute, TaskRouter, validate_route
from tests.support.clients import fake_client, reply


def eventually(check, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("Expected state did not arrive before deadline.")


class LocalAssembler:
    def __init__(self, root, client):
        self.workdir = root
        self.config = InteractionConfig(model_planning=False, shutdown_seconds=2)
        self.registry = default_registry()
        self.allowed = set(self.config.allowed)
        self.binding = ModelRegistry().select(client=client, model="test-model")
        self.gateway = ModelGateway(client)
        self.client = client

    def session(self, route, *, resume=None, conversation_mode=None, fork_seed=None):
        return HarnessSession(
            self.client,
            "test-model",
            self.workdir,
            capabilities=Capabilities(route.capabilities),
            resume_session=resume,
            conversation_mode=conversation_mode,
            fork_seed=fork_seed,
            shutdown_timeout=2,
            dispatch_interval=0.01,
        )

    def close(self):
        self.gateway.close()


class InteractionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def host(self, responses, *, trusted=True, router=None):
        client, snapshots = fake_client(responses)
        host = InteractionHost(
            LocalAssembler(self.root, client), trusted=trusted, router=router
        )
        self.addCleanup(host.close)
        return host, client, snapshots

    def test_simple_task_uses_session_and_closes_before_next_task(self):
        host, client, snapshots = self.host([reply("first"), reply("second")])
        first = host.submit("Explain the module")
        eventually(lambda: host.state.get(first["task_id"])["state"] == "finished")
        second = host.submit("Explain another module")
        eventually(lambda: host.state.get(second["task_id"])["state"] == "finished")
        self.assertNotEqual(
            host.state.get(first["task_id"])["target"],
            host.state.get(second["task_id"])["target"],
        )
        self.assertEqual(client.chat.completions.create.call_count, 2)
        reference = next(
            message
            for message in snapshots[1]
            if message.get("name") == "previous_task_reference"
        )
        self.assertIn('"answer_preview": "first"', reference["content"])

    def test_input_id_replay_does_not_create_or_execute_another_task(self):
        host, client, _ = self.host([reply("done")])
        identity = "usr_" + "a" * 32
        first = host.submit("Explain code", input_id=identity)
        eventually(lambda: host.state.get(first["task_id"])["state"] == "finished")
        self.assertEqual(first, host.submit("Explain code", input_id=identity))
        with self.assertRaises(ValueError):
            host.submit("Different code", input_id=identity)
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_active_input_steers_and_independent_task_queues(self):
        entered, release = threading.Event(), threading.Event()
        host, client, snapshots = self.host([])
        calls = []

        def create(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                entered.set()
                release.wait(4)
                return reply("old response")
            return reply("updated response")

        client.chat.completions.create.side_effect = create
        first = host.submit("Original requirement")
        self.assertTrue(entered.wait(5))
        amendment = host.submit("Keep the output in Chinese")
        queued = host.submit("A separate task", mode="queue")
        self.assertEqual(amendment["task_id"], first["task_id"])
        self.assertNotEqual(queued["task_id"], first["task_id"])
        release.set()
        eventually(lambda: host.state.get(queued["task_id"])["state"] == "finished")
        intent = host.state.get(first["task_id"])["intent"]
        self.assertEqual(
            intent,
            {
                "original": "Original requirement",
                "amendments": ["Keep the output in Chinese"],
            },
        )
        self.assertTrue(
            any("Keep the output in Chinese" in str(k["messages"]) for k in calls[1:])
        )

    def test_new_input_invalidates_inflight_routing(self):
        entered, release = threading.Event(), threading.Event()

        class Router:
            calls = []

            def propose(self, intent, **kwargs):
                self.calls.append(json.loads(json.dumps(intent)))
                if len(self.calls) == 1:
                    entered.set()
                    release.wait(4)
                return TaskRoute()

        router = Router()
        host, _, _ = self.host([reply("done")], router=router)
        task = host.submit("Original")
        self.assertTrue(entered.wait(5))
        host.submit("Amendment")
        release.set()
        eventually(lambda: host.state.get(task["task_id"])["state"] == "finished")
        self.assertEqual(len(router.calls), 2)
        self.assertEqual(router.calls[-1]["amendments"], ["Amendment"])

    def test_status_and_unknown_commands_do_not_call_model(self):
        host, client, _ = self.host([])
        self.assertEqual(host.submit("/status"), {"state": "idle"})
        with self.assertRaises(ValueError):
            host.submit("/unknown")
        self.assertEqual(client.chat.completions.create.call_count, 0)

    def test_mixed_confirmation_steers_instead_of_approving(self):
        entered, release = threading.Event(), threading.Event()
        host, client, _ = self.host([])

        def create(**_kwargs):
            entered.set()
            release.wait(4)
            return reply("done")

        client.chat.completions.create.side_effect = create
        receipt = host.submit("Original task")
        self.assertTrue(entered.wait(5))
        request = {"id": "approval_test", "revision": 0, "kind": "tool"}
        host.focus = host._focus_key(request)
        with (
            patch.object(host, "requests", return_value=[request]),
            patch.object(host, "respond") as respond,
        ):
            amended = host.submit("可以，但先修改计划")
            respond.assert_not_called()
            self.assertEqual(amended["task_id"], receipt["task_id"])
        release.set()

    def test_changed_focus_does_not_approve_replacement_request(self):
        host, _, _ = self.host([])
        host.focus = (None, "old", 0, None)
        with (
            patch.object(
                host,
                "requests",
                return_value=[{"id": "new", "revision": 0, "kind": "tool"}],
            ),
            patch.object(host, "respond") as respond,
        ):
            with self.assertRaises(ValueError):
                host.submit("yes")
            respond.assert_not_called()

    def test_test_grant_short_reply_never_renews_authorization(self):
        host, _, _ = self.host([])
        request = {"id": "grant", "revision": 0, "kind": "test_grant"}
        host.focus = host._focus_key(request)
        with (
            patch.object(host, "requests", return_value=[request]),
            patch.object(host, "respond") as respond,
        ):
            with self.assertRaises(ValueError):
                host.submit("yes")
            respond.assert_not_called()

    def test_lost_session_input_commit_recovers_same_input_once(self):
        from homebrew_harness.storage.sqlite_store import SQLiteDatabase

        host, client, _ = self.host([reply("done")])
        original = SQLiteDatabase.submit_input
        failed = []

        def submit(db, record, expected_turn=None):
            result = original(db, record, expected_turn)
            if not failed:
                failed.append(True)
                raise OSError("Fixture commit acknowledgement lost")
            return result

        with patch.object(SQLiteDatabase, "submit_input", submit):
            receipt = host.submit("Original requirement")
            eventually(lambda: host.state.get(receipt["task_id"])["state"] == "blocked")
        self.assertEqual(host.close()["state"], "closed")
        reopened = InteractionHost(LocalAssembler(self.root, client), trusted=True)
        self.addCleanup(reopened.close)
        reopened.resume()
        eventually(
            lambda: reopened.state.get(receipt["task_id"])["state"] == "finished"
        )
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_close_during_planning_retains_lease_until_real_thread_ends(self):
        entered, release = threading.Event(), threading.Event()

        class Router:
            def propose(self, *_args, **_kwargs):
                entered.set()
                release.wait(4)
                return TaskRoute()

        host, client, _ = self.host([], router=Router())
        receipt = host.submit("Original")
        self.assertTrue(entered.wait(5))
        self.assertEqual(host.close()["state"], "cleanup_pending")
        self.assertTrue(host.leases.held("interaction-host"))
        release.set()
        eventually(lambda: not host.leases.held("interaction-host"))
        self.assertIsNone(host.state.get(receipt["task_id"])["target"])
        self.assertEqual(client.chat.completions.create.call_count, 0)

    def test_cancel_during_planning_does_not_accept_late_route(self):
        entered, release = threading.Event(), threading.Event()

        class Router:
            def propose(self, *_args, **_kwargs):
                entered.set()
                release.wait(4)
                return TaskRoute()

        host, client, _ = self.host([], router=Router())
        receipt = host.submit("Original")
        self.assertTrue(entered.wait(5))
        host.cancel()
        release.set()
        self.assertTrue(host.workers.wait(5))
        task = host.state.get(receipt["task_id"])
        self.assertEqual(task["state"], "cancelled")
        self.assertIsNone(task["target"])
        self.assertEqual(client.chat.completions.create.call_count, 0)

    def test_answer_to_preparation_question_replans_without_resume_command(self):
        class Router:
            def propose(self, intent, **_kwargs):
                return (
                    TaskRoute(missing_requirements=("Choose an output format",))
                    if not intent["amendments"]
                    else TaskRoute()
                )

        host, _, _ = self.host([reply("done")], router=Router())
        receipt = host.submit("Explain the module")
        eventually(lambda: host.state.get(receipt["task_id"])["state"] == "blocked")
        host.submit("Use plain text")
        eventually(lambda: host.state.get(receipt["task_id"])["state"] == "finished")

    def test_accepting_next_task_during_old_owner_exit_always_kicks_it(self):
        host, client, _ = self.host([reply("first"), reply("second")])
        original = host.state.finish

        def finish(identity, **fields):
            original(identity, **fields)
            if fields.get("result") == "first":
                host.submit("Second task")

        with patch.object(host.state, "finish", side_effect=finish):
            host.submit("First task")
            eventually(lambda: client.chat.completions.create.call_count == 2)
            eventually(lambda: host.state.active() is None)

    def test_unmet_goal_with_no_active_tool_is_blocked_not_completed(self):
        goal = {
            "condition": "Create result.txt",
            "predicates": [
                {"id": "result", "type": "file_exists", "path": "result.txt"}
            ],
        }

        class Router:
            def propose(self, *_args, **_kwargs):
                return TaskRoute(goal_candidate=goal)

        host, client, _ = self.host(
            [reply("Cannot finish in this turn")], router=Router()
        )
        with patch.object(
            host.assembler,
            "session",
            side_effect=lambda route, **kwargs: HarnessSession(
                client,
                "test-model",
                self.root,
                max_turns=1,
                stop_block_cap=1,
                shutdown_timeout=2,
            ),
        ):
            receipt = host.submit("Create result.txt")
            eventually(lambda: host.state.get(receipt["task_id"])["state"] == "blocked")
        self.assertTrue(host.session.goal.pending)
        self.assertNotEqual(host.state.get(receipt["task_id"])["state"], "finished")

    def test_single_host_lease(self):
        host, client, _ = self.host([])
        with self.assertRaises(ValueError):
            InteractionHost(LocalAssembler(self.root, client))
        self.assertEqual(host.close()["state"], "closed")

    def test_unique_focus_approves_only_full_short_response(self):
        host, _, _ = self.host([])
        request = {"id": "approval_test", "revision": 0, "kind": "tool"}
        host.focus = host._focus_key(request)
        with (
            patch.object(host, "requests", return_value=[request]),
            patch.object(
                host, "respond", return_value={"state": "approved"}
            ) as respond,
        ):
            self.assertEqual(host.submit("允许这次操作"), {"state": "approved"})
            respond.assert_called_once_with("approval_test", 0, "approve")
        with patch.object(host, "requests", return_value=[request, request]):
            with self.assertRaises(ValueError):
                host.submit("yes")

    def test_untrusted_focus_cannot_approve(self):
        host, _, _ = self.host([], trusted=False)
        with patch.object(
            host, "requests", return_value=[{"id": "a", "revision": 0, "kind": "tool"}]
        ):
            with self.assertRaises(ValueError):
                host.submit("yes")

    def test_acceptance_is_atomic_and_stale_revision_rejected(self):
        state = InteractionState(self.root)
        receipt = state.accept_input("Original", "auto", "lead")
        task = state.get(receipt["task_id"])
        state.accept_input("Update", "auto", "lead")
        with self.assertRaises(ValueError):
            state.accept(task, TaskRoute(), {}, {"kind": "session", "id": "fake"})
        self.assertIsNone(state.get(task["id"])["target"])


class RoutingTests(unittest.TestCase):
    def validate(self, raw, **kwargs):
        return validate_route(
            raw,
            allowed={"workflow", "subagents", "mcp", "cron"},
            registry=default_registry(),
            prompt="Original full requirement",
            **kwargs,
        )

    def test_unknown_fields_capabilities_and_run_mcp_rejected(self):
        for raw in (
            {"endpoint": "test"},
            {"capabilities": ["unknown"]},
            {"execution_mode": "managed", "capabilities": ["mcp"]},
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.validate(raw)

    def test_lifetime_requires_explicit_host_choice(self):
        with self.assertRaises(ValueError):
            self.validate({"execution_mode": "managed", "lifetime": "detached"})

    def test_goal_cannot_replace_original_or_disable_judge(self):
        goal = {
            "condition": "Weaker requirement",
            "judge": False,
            "predicates": [
                {"id": "output", "type": "file_exists", "path": "answer.txt"}
            ],
        }
        route = self.validate({"goal_candidate": goal})
        self.assertEqual(route.goal_candidate["condition"], "Original full requirement")
        self.assertTrue(route.goal_candidate["judge"])

    def test_workflow_names_and_arguments_checked(self):
        with self.assertRaises(Exception):
            self.validate(
                {
                    "capabilities": ["workflow"],
                    "workflows": [
                        {"name": "review-changes", "args": {"command": "anything"}}
                    ],
                }
            )

    def test_complete_model_proposal_uses_small_catalog_without_tool_schema(self):
        client, _ = fake_client([reply(json.dumps({"capabilities": ["subagents"]}))])
        binding = ModelRegistry().select(client=client, model="test-model")
        router = TaskRouter(
            binding,
            ModelGateway(client),
            allowed={"subagents"},
            registry=default_registry(),
        )
        result = router.propose(
            {
                "original": "Explain the term team in quoted documentation",
                "amendments": [],
            }
        )
        self.assertEqual(result.capabilities, ("subagents",))
        kwargs = client.chat.completions.create.call_args.kwargs
        self.assertNotIn("tools", kwargs)
        self.assertLess(len(str(kwargs["messages"])), 5000)


class HandoffArchiveTests(unittest.TestCase):
    def test_target_reads_delegated_archive_and_keeps_its_own_namespace(self):
        from homebrew_harness.context.compaction import ArchiveStore, CompactionError

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = ArchiveStore(root, "source"), ArchiveStore(root, "target")
            reference = source.save("tool-results", "known source result")
            target.inherit_checkpoint(source.checkpoint())
            self.assertTrue(target.owns(reference))
            new_reference = target.save("tool-results", "new result")
            self.assertIn("/target/", new_reference)
            self.assertFalse(source.owns(new_reference))
            recovered = ArchiveStore(root)
            recovered.restore_checkpoint(target.checkpoint())
            self.assertTrue(recovered.owns(reference))
            with self.assertRaises(CompactionError):
                target.inherit_checkpoint(
                    {"namespace": "source", "saved": [".harness/leases/private.lock"]}
                )
