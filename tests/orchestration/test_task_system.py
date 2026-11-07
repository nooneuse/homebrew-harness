"""Task graph invariants, disk recovery and the existing dispatch boundary."""

from __future__ import annotations
from tests.support.paths import PROJECT_ROOT
from homebrew_harness.runtime.capabilities import Capabilities

import fcntl
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch

from tests.support.tools import execute_fixture_tool
import homebrew_harness.runtime.hooks
import homebrew_harness.runtime.loop
import homebrew_harness.tools.registry
import homebrew_harness.security.permissions as permissions
from tests.support.clients import call, fake_client, reply
from homebrew_harness.orchestration.task_store import TASK_TOOL_NAMES, TaskStore
from homebrew_harness.context.todo import TodoManager


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workdir = Path(temporary.name).resolve()
        self.store = TaskStore(self.workdir)

    def record_path(self, task_id):
        return self.workdir / ".tasks" / (task_id + ".json")

    def test_empty_read_does_not_create_storage_and_workspaces_are_isolated(self):
        self.assertEqual(self.store.list(), [])
        self.assertFalse(self.store.directory.exists())
        task = self.store.create("Parent task", "Full recovery instructions")
        other = self.workdir / "other"
        other.mkdir()
        self.assertEqual(TaskStore(other).list(), [])
        self.assertEqual(TaskStore(self.workdir).load(task.id), task)
        self.assertEqual(
            json.loads(self.record_path(task.id).read_text()),
            {
                "id": task.id,
                "subject": task.subject,
                "description": task.description,
                "status": "pending",
                "owner": None,
                "blockedBy": [],
                "worktree": None,
                "task_intent": None,
            },
        )

    def test_ids_are_generated_exclusively_and_collision_never_overwrites(self):
        with patch(
            "homebrew_harness.orchestration.task_store.secrets.token_hex",
            side_effect=["aaaaaaaa", "aaaaaaaa", "bbbbbbbb"],
        ):
            first = self.store.create("First")
            second = self.store.create("Second")
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(self.store.load(first.id).subject, "First")
        with patch(
            "homebrew_harness.orchestration.task_store.secrets.token_hex",
            return_value="aaaaaaaa",
        ):
            with self.assertRaisesRegex(ValueError, "unique"):
                self.store.create("Cannot replace")
        self.assertEqual(self.store.load(first.id), first)
        self.assertEqual(list(self.store.directory.glob("*.tmp")), [])

    def test_invalid_subject_description_and_ids_never_write_records(self):
        for subject, description in ((" ", ""), (None, ""), ("Valid", [])):
            with self.subTest(subject=subject), self.assertRaises(ValueError):
                self.store.create(subject, description)
        for task_id in (
            "../outside",
            "/outside",
            "task_1234",
            "task_ABCDEFGH",
            None,
            [],
        ):
            with self.subTest(task_id=task_id), self.assertRaises(ValueError):
                self.store.load(task_id)
        self.assertFalse(self.store.directory.exists())

    def test_two_phase_graph_deduplicates_edges(self):
        schema, api = self.store.create("Schema"), self.store.create("API")
        updated = self.store.update_dependencies(api.id, [schema.id, schema.id])
        self.assertEqual(updated.blockedBy, (schema.id,))
        self.assertEqual(self.store.update_dependencies(api.id, [schema.id]), updated)
        self.assertEqual(self.store.load(api.id).blockedBy, (schema.id,))

    def test_self_missing_and_transitive_cycles_reject_whole_update(self):
        a, b, c = [self.store.create(name) for name in ("A", "B", "C")]
        self.store.update_dependencies(b.id, [a.id])
        self.store.update_dependencies(c.id, [b.id])
        before = self.record_path(a.id).read_bytes()
        for edges in (
            [a.id],
            [b.id],
            [c.id],
            ["task_00000000"],
            [c.id, "../bad"],
            "bad",
        ):
            with self.subTest(edges=edges), self.assertRaises(ValueError):
                self.store.update_dependencies(a.id, edges)
            self.assertEqual(self.record_path(a.id).read_bytes(), before)
        d = self.store.create("D")
        with self.assertRaises(ValueError):
            self.store.update_dependencies(d.id, [a.id, "task_00000000"])
        self.assertEqual(self.store.load(d.id).blockedBy, ())

    def test_claim_requires_all_predecessors_and_completion_unlocks_only_new_tasks(
        self,
    ):
        a, b, join, already_ready = [
            self.store.create(name) for name in ("A", "B", "Join", "Ready")
        ]
        self.store.update_dependencies(join.id, [a.id, b.id])
        with self.assertRaisesRegex(ValueError, "blocked"):
            self.store.claim(join.id)
        self.store.claim(a.id)
        _, unlocked = self.store.complete(a.id)
        self.assertEqual(unlocked, [])
        with self.assertRaisesRegex(ValueError, b.id):
            self.store.claim(join.id)
        self.store.claim(b.id)
        completed, unlocked = self.store.complete(b.id)
        self.assertEqual(completed.status, "completed")
        self.assertEqual([task.id for task in unlocked], [join.id])
        self.assertEqual(self.store.load(join.id).status, "pending")
        self.assertEqual(self.store.claim(join.id).status, "in_progress")
        self.assertEqual(self.store.load(already_ready.id).status, "pending")

    def test_lifecycle_owner_and_post_claim_dependency_changes_are_guarded(self):
        task = self.store.create("Work")
        other = TaskStore(self.workdir, owner="other-agent")
        with self.assertRaisesRegex(ValueError, "cannot complete"):
            self.store.complete(task.id)
        self.assertEqual(self.store.claim(task.id).owner, "agent")
        for action in (
            lambda: other.claim(task.id),
            lambda: other.complete(task.id),
            lambda: self.store.update_dependencies(task.id, []),
        ):
            before = self.record_path(task.id).read_bytes()
            with self.assertRaises(ValueError):
                action()
            self.assertEqual(self.record_path(task.id).read_bytes(), before)
        # Logical identity is insufficient while the first host is still live.
        recovered = TaskStore(self.workdir)
        with self.assertRaises(ValueError):
            recovered.resume(task.id)
        self.store.close()
        recovered.resume(task.id)
        completed, _ = recovered.complete(task.id)
        self.assertEqual(completed.owner, "agent")
        for action in (
            lambda: self.store.claim(task.id),
            lambda: self.store.complete(task.id),
            lambda: self.store.update_dependencies(task.id, []),
        ):
            with self.assertRaises(ValueError):
                action()

    def test_missing_or_corrupt_predecessor_blocks_claim(self):
        first, next_task = self.store.create("First"), self.store.create("Next")
        self.store.update_dependencies(next_task.id, [first.id])
        path = self.record_path(first.id)
        for corrupt in (None, "invalid json", '{"status":"completed"}'):
            if corrupt is None:
                path.unlink()
            else:
                path.write_text(corrupt)
            with (
                self.subTest(corrupt=corrupt),
                self.assertRaisesRegex(ValueError, "blocked"),
            ):
                self.store.claim(next_task.id)
        self.assertEqual(self.store.load(next_task.id).status, "pending")

    def test_record_schema_and_owner_coherence_are_validated_on_reload(self):
        task = self.store.create("Valid")
        original = task.to_dict()
        changes = [
            {"id": "task_00000000"},
            {"subject": " "},
            {"description": []},
            {"status": "unknown"},
            {"owner": "unexpected"},
            {"blockedBy": "bad"},
            {"status": "in_progress", "owner": None},
            {"status": "completed", "owner": ""},
            {"blockedBy": [task.id]},
            {"blockedBy": ["task_00000000", "task_00000000"]},
            {"blockedBy": ["../outside"]},
            {"extra": True},
        ]
        for change in changes:
            self.record_path(task.id).write_text(json.dumps({**original, **change}))
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.store.load(task.id)

    def test_symlink_directory_records_and_lock_cannot_escape(self):
        outside = self.workdir / "outside"
        outside.mkdir()
        self.store.directory.symlink_to(outside, target_is_directory=True)
        for action in (lambda: self.store.create("No write"), self.store.list):
            with self.assertRaisesRegex(ValueError, "symlink"):
                action()
        self.assertEqual(list(outside.iterdir()), [])
        self.store.directory.unlink()
        task = self.store.create("Original")
        outside_file = outside / "record.json"
        outside_file.write_text(json.dumps(task.to_dict()))
        self.record_path(task.id).unlink()
        self.record_path(task.id).symlink_to(outside_file)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.store.load(task.id)
        self.assertEqual(json.loads(outside_file.read_text()), task.to_dict())
        lock = self.store.directory / ".lock"
        lock.unlink()
        lock.symlink_to(outside_file)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.store.create("No lock write")

    def test_failed_publication_leaves_old_state_and_releases_lock(self):
        task = self.store.create("Pending")
        before = self.record_path(task.id).read_bytes()
        with patch(
            "homebrew_harness.orchestration.task_store.os.replace",
            side_effect=OSError("Simulated failure"),
        ):
            with self.assertRaises(OSError):
                self.store.claim(task.id)
        self.assertEqual(self.record_path(task.id).read_bytes(), before)
        self.assertEqual(list(self.store.directory.glob("*.tmp")), [])
        self.assertEqual(self.store.claim(task.id).status, "in_progress")

    def test_busy_store_fails_without_mutation_then_recovers(self):
        task = self.store.create("Pending")
        with (self.store.directory / ".lock").open("r+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, "busy"):
                self.store.claim(task.id)
        self.assertEqual(self.store.load(task.id).status, "pending")
        self.assertEqual(self.store.claim(task.id).status, "in_progress")

    def test_fresh_python_process_recovers_description_owner_and_graph(self):
        schema, api = (
            self.store.create("Schema", "Inspect the saved output first"),
            self.store.create("API"),
        )
        self.store.update_dependencies(api.id, [schema.id])
        self.store.claim(schema.id)
        self.store.close()
        script = """
import json, sys
from pathlib import Path
from homebrew_harness.orchestration.task_store import TaskStore
store = TaskStore(Path(sys.argv[1]))
task = store.load(sys.argv[2])
assert task.status == 'in_progress' and task.owner == 'agent'
assert task.description == 'Inspect the saved output first'
store.resume(task.id)
done, unblocked = store.complete(task.id)

assert [t.id for t in unblocked] == [sys.argv[3]]
store.claim(sys.argv[3])
print(json.dumps([t.to_dict() for t in store.list()]))
"""
        result = subprocess.run(
            [sys.executable, "-c", script, str(self.workdir), schema.id, api.id],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            timeout=10,
            check=True,
        )
        records = {task["id"]: task for task in json.loads(result.stdout)}
        self.assertEqual(records[schema.id]["status"], "completed")
        self.assertEqual(records[api.id]["blockedBy"], [schema.id])
        self.assertEqual(self.store.load(api.id).status, "in_progress")


class TaskDispatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workdir = Path(temporary.name).resolve()
        self.store = TaskStore(self.workdir)
        self.hooks = homebrew_harness.runtime.hooks.create_hooks()
        self.handlers = homebrew_harness.tools.registry.create_tool_handlers(
            TodoManager(), task_store=self.store
        )
        capture = redirect_stderr(io.StringIO())
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def invoke(self, name, **arguments):
        tool_call = (
            reply(calls=[call(name, name=name, arguments=json.dumps(arguments))])
            .choices[0]
            .message.tool_calls[0]
        )
        return json.loads(
            execute_fixture_tool(
                tool_call, self.workdir, hooks=self.hooks, handlers=self.handlers
            )
        )

    def test_six_tools_return_structured_tasks_readiness_and_unblocked_ids(self):
        with patch("homebrew_harness.security.permissions.ask_user") as ask:
            first = self.invoke(
                "create_task", subject="Schema", description="Detailed instructions"
            )
            second = self.invoke("create_task", subject="API")
            updated = self.invoke(
                "update_task", task_id=second["id"], addBlockedBy=[first["id"]]
            )
            self.assertEqual(updated["blockedBy"], [first["id"]])
            self.assertEqual(self.invoke("get_task", task_id=first["id"]), first)
            summaries = {t["id"]: t for t in self.invoke("list_tasks")["tasks"]}
            self.assertTrue(summaries[first["id"]]["ready"])
            self.assertFalse(summaries[second["id"]]["ready"])
            self.assertNotIn("description", summaries[first["id"]])
            self.assertIn("error", self.invoke("claim_task", task_id=second["id"]))
            claimed = self.invoke("claim_task", task_id=first["id"])
            self.assertEqual(claimed["owner"], "agent")
            summaries = {t["id"]: t for t in self.invoke("list_tasks")["tasks"]}
            self.assertTrue(summaries[first["id"]]["resumable"])
            complete = self.invoke("complete_task", task_id=first["id"])
            self.assertEqual([t["id"] for t in complete["unblocked"]], [second["id"]])
        ask.assert_not_called()

    def test_unknown_input_cannot_override_owner_status_id_or_storage(self):
        for extra in (
            {"owner": "other"},
            {"status": "completed"},
            {"id": "task_aaaaaaaa"},
            {"directory": "../outside"},
            {"blockedBy": []},
        ):
            with self.subTest(extra=extra):
                self.assertIn(
                    "error", self.invoke("create_task", subject="No write", **extra)
                )
        self.assertFalse(self.store.directory.exists())
        task = self.invoke("create_task", subject="Valid")
        for extra in ({"owner": "other"}, {"status": "completed"}):
            self.assertIn(
                "error", self.invoke("claim_task", task_id=task["id"], **extra)
            )
        self.assertEqual(self.store.load(task["id"]).status, "pending")
        self.assertIn(
            "error", self.invoke("update_task", task_id=task["id"], addBlockedBy="bad")
        )

    def test_permission_and_hooks_run_before_task_state_mutations(self):
        events = []
        self.hooks.register_hook(
            "PreToolUse", lambda event: events.append(("pre", event.name))
        )
        self.hooks.register_hook(
            "PostToolUse", lambda event, output: events.append(("post", event.name))
        )
        first = self.invoke("create_task", subject="First")
        self.assertEqual(events, [("pre", "create_task"), ("post", "create_task")])
        self.hooks.register_hook(
            "PreToolUse",
            lambda event: "Blocked" if event.name == "claim_task" else None,
        )
        tool_call = (
            reply(
                calls=[
                    call(
                        "deny",
                        name="claim_task",
                        arguments=json.dumps({"task_id": first["id"]}),
                    )
                ]
            )
            .choices[0]
            .message.tool_calls[0]
        )
        self.assertEqual(
            execute_fixture_tool(
                tool_call, self.workdir, hooks=self.hooks, handlers=self.handlers
            ),
            "Blocked",
        )
        self.assertEqual(self.store.load(first["id"]).status, "pending")
        for name in TASK_TOOL_NAMES:
            self.assertEqual(
                permissions.check_rules(name, {}, self.workdir).action, "allow"
            )

    def test_default_dispatch_and_host_binding(self):
        tool_call = (
            reply(
                calls=[
                    call(
                        "create", name="create_task", arguments='{"subject":"Default"}'
                    )
                ]
            )
            .choices[0]
            .message.tool_calls[0]
        )
        self.assertEqual(
            json.loads(execute_fixture_tool(tool_call, self.workdir))["error"],
            "Unknown tool.",
        )
        task = json.loads(
            execute_fixture_tool(tool_call, self.workdir, handlers=self.handlers)
        )
        self.assertEqual(self.store.load(task["id"]).subject, "Default")
        with self.assertRaisesRegex(ValueError, "different working"):
            self.store.run_tool("list_tasks", workdir=self.workdir.parent)

    def test_parent_loop_discovers_all_tools_and_pairs_task_failures(self):
        with patch(
            "homebrew_harness.orchestration.task_store.secrets.token_hex",
            side_effect=["aaaaaaaa", "bbbbbbbb"],
        ):
            client, snapshots = fake_client(
                [
                    reply(
                        calls=[
                            call(
                                "create-a",
                                name="create_task",
                                arguments='{"subject":"A"}',
                            ),
                            call(
                                "create-b",
                                name="create_task",
                                arguments='{"subject":"B"}',
                            ),
                        ]
                    ),
                    reply(
                        calls=[
                            call(
                                "edge",
                                name="update_task",
                                arguments='{"task_id":"task_bbbbbbbb","addBlockedBy":["task_aaaaaaaa"]}',
                            )
                        ]
                    ),
                    reply(
                        calls=[
                            call(
                                "blocked",
                                name="claim_task",
                                arguments='{"task_id":"task_bbbbbbbb"}',
                            ),
                            call(
                                "claim-a",
                                name="claim_task",
                                arguments='{"task_id":"task_aaaaaaaa"}',
                            ),
                        ]
                    ),
                    reply(
                        calls=[
                            call(
                                "complete-a",
                                name="complete_task",
                                arguments='{"task_id":"task_aaaaaaaa"}',
                            )
                        ]
                    ),
                    reply("A is complete; B is ready."),
                ]
            )
            history = [{"role": "user", "content": "Build and execute the graph."}]
            self.assertEqual(
                homebrew_harness.runtime.loop.agent_loop(
                    client,
                    "test-model",
                    history,
                    self.workdir,
                    hooks=self.hooks,
                    capabilities=Capabilities.full(memory_mode="off"),
                ),
                "A is complete; B is ready.",
            )
        tools = {
            t["function"]["name"]
            for t in client.chat.completions.create.call_args_list[0].kwargs["tools"]
        }
        self.assertTrue(set(TASK_TOOL_NAMES).issubset(tools))
        results = {
            m["tool_call_id"]: json.loads(m["content"])
            for m in history
            if m["role"] == "tool"
        }
        self.assertIn("error", results["blocked"])
        self.assertEqual(results["claim-a"]["status"], "in_progress")
        self.assertEqual(results["complete-a"]["unblocked"][0]["id"], "task_bbbbbbbb")
        self.assertEqual(self.store.load("task_bbbbbbbb").status, "pending")
        self.assertTrue(snapshots)

    def test_subagent_cannot_invent_persistent_tools_or_rewrite_records(self):
        parent_task = self.store.create("Parent owned task")
        calls = [call(name, name=name, arguments="{}") for name in TASK_TOOL_NAMES]
        calls.extend(
            [
                call(
                    "write",
                    name="write_file",
                    arguments=json.dumps(
                        {"path": f".tasks/{parent_task.id}.json", "content": "bypass"}
                    ),
                ),
                call(
                    "edit",
                    name="edit_file",
                    arguments=json.dumps(
                        {
                            "path": f".tasks/{parent_task.id}.json",
                            "old_text": "pending",
                            "new_text": "completed",
                        }
                    ),
                ),
            ]
        )
        client, _ = fake_client([reply(calls=calls), reply("Report to parent.")])
        history = [{"role": "user", "content": "Focused task"}]
        homebrew_harness.runtime.loop.agent_loop(
            client,
            "test-model",
            history,
            self.workdir,
            hooks=self.hooks,
            subagent=True,
            capabilities=Capabilities.full(memory_mode="off"),
        )
        tools = client.chat.completions.create.call_args_list[0].kwargs["tools"]
        self.assertFalse(
            set(TASK_TOOL_NAMES).intersection(t["function"]["name"] for t in tools)
        )
        results = [json.loads(m["content"]) for m in history if m["role"] == "tool"]
        self.assertTrue(
            all(r["error"] == "Unknown tool." for r in results[: len(TASK_TOOL_NAMES)])
        )
        self.assertTrue(
            all(r["permission"] == "deny" for r in results[len(TASK_TOOL_NAMES) :])
        )
        self.assertEqual(self.store.load(parent_task.id), parent_task)

    def test_file_writes_cannot_bypass_task_transitions_through_an_alias(self):
        task = self.store.create("Protected")
        (self.workdir / "alias").symlink_to(
            self.store.directory, target_is_directory=True
        )
        result = self.invoke(
            "write_file", path=f"alias/{task.id}.json", content="invalid"
        )
        self.assertEqual(result["permission"], "deny")
        self.assertEqual(self.store.load(task.id), task)
