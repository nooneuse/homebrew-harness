"""R56: actual dependencies precede recall and survive as one effective snapshot."""

import io
import json
import tempfile
import unittest
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from homebrew_harness.runtime.loop_components import LoopComponents
import homebrew_harness.context.compaction
import homebrew_harness.context.skill_loader
import homebrew_harness.context.todo
import homebrew_harness.orchestration.goal_loop
import homebrew_harness.orchestration.task_store
import homebrew_harness.orchestration.teams
import homebrew_harness.runtime.assembly
import homebrew_harness.runtime.errors
import homebrew_harness.runtime.hooks
import homebrew_harness.runtime.loop
import homebrew_harness.runtime.turns
import homebrew_harness.tools.catalog
import homebrew_harness.tools.registry
import homebrew_harness.tools.shell
from homebrew_harness.execution.background import BackgroundManager, ShellRunner
from homebrew_harness.runtime.capabilities import Capabilities, capability_scope
from homebrew_harness.orchestration.cron_scheduler import CronScheduler
from homebrew_harness.tools.mcp.runtime import ToolPolicy
from homebrew_harness.security.permissions import PermissionDecision
from homebrew_harness.orchestration.task_store import TaskStore
from homebrew_harness.execution.test_runner import TestRunner
from homebrew_harness.orchestration.workspace_runtime import WorkspaceRuntime
from tests.support.clients import fake_client, reply, call


class PreparedLoopTests(unittest.TestCase):
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
        self.hooks = homebrew_harness.runtime.hooks.create_hooks()
        self.skills = homebrew_harness.context.skill_loader.SkillLoader(
            self.root / "skills"
        )
        self.memory = SimpleNamespace(recall=Mock(return_value=""), finish_turn=Mock())
        self.pools, self.components = [], []

    @contextmanager
    def capture(self):
        build = homebrew_harness.tools.registry.build_tool_pool

        def compile_pool(*args, **kwargs):
            pool = build(*args, **kwargs)
            self.components.append(args[3])
            self.pools.append(pool)
            return pool

        with patch(
            "homebrew_harness.tools.registry.build_tool_pool", side_effect=compile_pool
        ):
            yield

    @contextmanager
    def owned_resources(self):
        created = {}
        with ExitStack() as stack:
            for name, target, constructor in (
                (
                    "tests",
                    "homebrew_harness.runtime.assembly.TestRunner.default",
                    TestRunner.default,
                ),
                ("shell", "homebrew_harness.runtime.assembly.ShellRunner", ShellRunner),
                ("tasks", "homebrew_harness.runtime.assembly.TaskStore", TaskStore),
                (
                    "background",
                    "homebrew_harness.runtime.assembly.BackgroundManager",
                    BackgroundManager,
                ),
            ):

                def create(*args, _name=name, _constructor=constructor, **kwargs):
                    value = _constructor(*args, **kwargs)
                    value.close = Mock(wraps=value.close)
                    created.setdefault(_name, []).append(value)
                    return value

                stack.enter_context(patch(target, side_effect=create))
            yield created

    def user(self, client, *, compactor=None, **kwargs):
        compactor = compactor or homebrew_harness.context.compaction.ContextCompactor(
            client, "fixture", self.root
        )
        messages = [{"role": "system", "content": "Fixture instructions"}]
        parts = {
            name: kwargs.pop(name)
            for name in tuple(kwargs)
            if name in LoopComponents.__dataclass_fields__
        }
        result = homebrew_harness.runtime.turns.run_user_turn(
            client,
            "fixture",
            messages,
            "Read fixture",
            self.root,
            memory_runtime=self.memory,
            **kwargs,
            components=LoopComponents(
                hooks=self.hooks,
                todo_manager=homebrew_harness.context.todo.TodoManager(),
                skill_loader=self.skills,
                compactor=compactor,
                **parts,
            ),
        )
        return result, messages, compactor

    def assert_closed(self, created):
        for values in created.values():
            self.assertEqual(len(values), 1)
            values[0].close.assert_called_once_with()

    def assert_recall_matches(self, client, compactor, query):
        tools = client.chat.completions.create.call_args_list[0].kwargs["tools"]
        self.assertEqual(self.pools[0].tools, tools)
        expected = (
            compactor.context_chars
            - compactor.estimate_chars(
                [
                    {"role": "system", "content": "Fixture instructions"},
                    {"role": "user", "content": query},
                ],
                tools,
            )
            - 2000
        )
        self.memory.recall.assert_called_once_with(query, max_chars=max(0, expected))

    def test_standalone_user_recall_sees_all_sixteen_actual_tools(self):
        client, _ = fake_client([reply("done")])
        with (
            capability_scope(Capabilities({"tasks", "background"})),
            self.capture(),
            self.owned_resources() as created,
        ):
            result, _, compactor = self.user(client)
        self.assertEqual(result, "done")
        self.assertEqual(len(self.pools), 1)
        self.assertEqual(len(self.pools[0].tools), 16)
        self.assertTrue(
            set(homebrew_harness.orchestration.task_store.TASK_TOOL_NAMES)
            <= self.pools[0].names
        )
        self.assertIn("check_background", self.pools[0].names)
        self.assert_recall_matches(client, compactor, "Read fixture")
        self.assertFalse(created["background"][0].allow_async)
        bash = next(
            t["function"]
            for t in self.pools[0].tools
            if t["function"]["name"] == "bash"
        )
        self.assertNotIn("run_in_background", bash["parameters"]["properties"])
        self.assert_closed(created)

    def test_core_user_omits_optional_resources_and_uses_actual_recall_capacity(self):
        client, _ = fake_client([reply("done")])
        with (
            capability_scope(Capabilities()),
            self.capture(),
            self.owned_resources() as created,
        ):
            result, _, compactor = self.user(client)
        self.assertEqual(result, "done")
        self.assertEqual(len(self.pools[0].tools), 8)
        self.assertEqual(set(created), {"tests", "shell"})
        self.assert_recall_matches(client, compactor, "Read fixture")
        self.assert_closed(created)

    def test_invalid_stop_cap_is_rejected_before_resource_construction(self):
        client, _ = fake_client([])
        with self.owned_resources() as created:
            with self.assertRaises(homebrew_harness.orchestration.goal_loop.GoalError):
                homebrew_harness.runtime.loop.agent_loop(
                    client, "fixture", [], self.root, stop_block_cap=0
                )
        self.assertFalse(created)
        client.chat.completions.create.assert_not_called()

    def test_direct_loop_creates_default_components_once_across_rounds(self):
        (self.root / "fixture.txt").write_text("value")
        client, _ = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "read", name="read_file", arguments='{"path":"fixture.txt"}'
                        )
                    ]
                ),
                reply("done"),
            ]
        )
        with (
            self.capture(),
            self.owned_resources() as created,
            patch(
                "homebrew_harness.runtime.assembly.SkillLoader",
                wraps=homebrew_harness.context.skill_loader.SkillLoader,
            ) as skills,
            patch(
                "homebrew_harness.runtime.assembly.ContextCompactor",
                wraps=homebrew_harness.context.compaction.ContextCompactor,
            ) as compactor,
            patch(
                "homebrew_harness.runtime.assembly.TodoManager",
                wraps=homebrew_harness.context.todo.TodoManager,
            ) as todo,
        ):
            result = homebrew_harness.runtime.loop.agent_loop(
                client,
                "fixture",
                [{"role": "user", "content": "Read fixture"}],
                self.root,
                capabilities=Capabilities({"tasks", "background", "cron"}),
            )
        self.assertEqual(result, "done")
        self.assertEqual(len(self.pools), 2)
        skills.assert_called_once()
        compactor.assert_called_once()
        todo.assert_called_once()
        for name in (
            "todo_manager",
            "compactor",
            "skill_loader",
            "test_runner",
            "task_store",
            "cron_scheduler",
            "shell_runner",
            "background_manager",
        ):
            self.assertIs(
                getattr(self.components[0], name), getattr(self.components[1], name)
            )
        self.assertIs(
            self.pools[0].handlers["load_skill"].__self__,
            self.components[0].skill_loader,
        )
        self.assertIs(
            self.pools[0].handlers["compact"].__self__, self.components[0].compactor
        )
        self.assertIs(
            self.pools[0].handlers["todo_write"].__self__,
            self.components[0].todo_manager,
        )
        self.assertIs(
            self.pools[0].handlers["check_background"].__self__,
            created["background"][0],
        )
        self.assert_closed(created)

    def test_builder_never_creates_runtime_components(self):
        client, _ = fake_client([])
        compactor = homebrew_harness.context.compaction.ContextCompactor(
            client, "fixture", self.root
        )
        todo = homebrew_harness.context.todo.TodoManager()
        tests = TestRunner(self.root)
        self.addCleanup(tests.close)
        with ExitStack() as stack:
            for name in (
                "SkillLoader",
                "ContextCompactor",
                "TodoManager",
                "TaskStore",
                "BackgroundManager",
                "ShellRunner",
                "CronScheduler",
            ):
                stack.enter_context(
                    patch(
                        "homebrew_harness.runtime.assembly." + name,
                        side_effect=AssertionError("builder constructed " + name),
                    )
                )
            stack.enter_context(
                patch(
                    "homebrew_harness.runtime.assembly.TestRunner.default",
                    side_effect=AssertionError("builder constructed tests"),
                )
            )
            pool = homebrew_harness.tools.registry.build_tool_pool(
                client,
                "fixture",
                self.root,
                LoopComponents(
                    hooks=self.hooks,
                    todo_manager=todo,
                    skill_loader=self.skills,
                    compactor=compactor,
                    test_runner=tests,
                    capabilities=Capabilities.full(memory_mode="off"),
                ),
            )
        self.assertFalse(
            set(homebrew_harness.orchestration.task_store.TASK_TOOL_NAMES) & pool.names
        )
        self.assertNotIn("check_background", pool.names)
        self.assertNotIn("schedule_cron", pool.names)

    def test_scheduled_recall_and_execution_share_defaults_and_plan(self):
        scheduler = CronScheduler(self.root)
        scheduler.schedule("* * * * *", "Scheduled fixture", recurring=False)
        scheduler.poll_due(datetime(2026, 1, 1, 12, 0))
        job = scheduler.take_pending()
        client, _ = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "todo",
                            name="todo_write",
                            arguments='{"todos":[{"content":"Read fixture","status":"in_progress"}]}',
                        )
                    ]
                ),
                reply("done"),
            ]
        )
        compactor = homebrew_harness.context.compaction.ContextCompactor(
            client, "fixture", self.root
        )
        saved_intent = compactor.intent
        saved_intent.update("Original user task")
        messages = [{"role": "system", "content": "Fixture instructions"}]
        with (
            capability_scope(Capabilities({"tasks", "background", "cron"})),
            self.capture(),
            self.owned_resources() as created,
        ):
            result = homebrew_harness.runtime.turns.run_scheduled_turn(
                job,
                client=client,
                model="fixture",
                messages=messages,
                workdir=self.root,
                scheduler=scheduler,
                memory_runtime=self.memory,
                components=LoopComponents(
                    hooks=self.hooks, skill_loader=self.skills, compactor=compactor
                ),
            )
        self.assertEqual(result, "done")
        self.assert_recall_matches(client, compactor, job.prompt)
        self.assertIs(self.components[0].todo_manager, self.components[1].todo_manager)
        self.assertIn("Read fixture", self.components[0].todo_manager.render())
        self.assertIs(compactor.intent, saved_intent)
        self.assertFalse(scheduler.list())
        self.assert_closed(created)

    def host(self, capabilities, background=None):
        host = WorkspaceRuntime(
            self.root, tasks=capabilities.has("tasks"), background_manager=background
        )
        host.capabilities = capabilities
        host.test_runner = TestRunner(self.root)
        self.addCleanup(host.test_runner.close)
        self.addCleanup(host.close)
        return host

    def test_team_recall_prepares_background_and_cron_before_first_request(self):
        host = self.host(Capabilities({"tasks", "background", "cron"}))
        client, _ = fake_client([reply("done")])
        compactor = homebrew_harness.context.compaction.ContextCompactor(
            client, "fixture", self.root
        )
        messages = [{"role": "system", "content": "Fixture instructions"}]
        with self.capture(), self.owned_resources() as created:
            result = homebrew_harness.runtime.turns.run_team_turn(
                [],
                client=client,
                model="fixture",
                messages=messages,
                workdir=self.root,
                active_request="Team fixture",
                memory_runtime=self.memory,
                components=LoopComponents(
                    team_manager=host,
                    hooks=self.hooks,
                    skill_loader=self.skills,
                    compactor=compactor,
                ),
            )
        self.assertEqual(result, "done")
        self.assert_recall_matches(client, compactor, "Team fixture")
        self.assertIn("schedule_cron", self.pools[0].names)
        self.assertIn("check_background", self.pools[0].names)
        self.assertEqual(host.lead_busy, 0)
        self.assertEqual(set(created), {"background"})
        self.assert_closed(created)
        self.assertFalse(host.test_runner.closed.is_set())
        self.assertFalse(host.shell_runner._closed)

    def test_injected_host_resources_are_borrowed_and_remain_open(self):
        background = BackgroundManager(self.root, homebrew_harness.tools.shell.run_bash)
        self.addCleanup(background.close)
        host = self.host(Capabilities({"tasks", "background"}), background)
        host.store.close = Mock(wraps=host.store.close)
        background.close = Mock(wraps=background.close)
        client, _ = fake_client([reply("done")])
        with self.capture(), self.owned_resources() as created:
            result, _, compactor = self.user(client, team_manager=host)
        self.assertEqual(result, "done")
        self.assert_recall_matches(client, compactor, "Read fixture")
        self.assertFalse(created)
        self.assertIs(self.components[0].test_runner, host.test_runner)
        self.assertIs(self.components[0].shell_runner, host.shell_runner)
        self.assertIs(self.components[0].background_manager, background)
        host.store.close.assert_not_called()
        background.close.assert_not_called()
        self.assertFalse(host.test_runner.closed.is_set())
        self.assertFalse(host.shell_runner._closed)
        self.assertEqual(host.lead_busy, 0)

    def test_recall_failure_closes_owned_components_without_model_or_extraction(self):
        client, _ = fake_client([])
        error = RuntimeError("fixture recall failure")
        self.memory.recall.side_effect = error
        with (
            capability_scope(Capabilities({"tasks", "background"})),
            self.owned_resources() as created,
        ):
            with self.assertRaises(RuntimeError) as caught:
                self.user(client)
        self.assertIs(caught.exception, error)
        self.assert_closed(created)
        client.chat.completions.create.assert_not_called()
        self.memory.finish_turn.assert_not_called()

    def test_prompt_hook_interruption_closes_owned_components_before_loop(self):
        self.hooks.register_hook(
            "UserPromptSubmit", Mock(side_effect=KeyboardInterrupt("fixture hook"))
        )
        client, _ = fake_client([])
        with (
            capability_scope(Capabilities({"tasks", "background"})),
            self.owned_resources() as created,
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.user(client)
        self.assert_closed(created)
        client.chat.completions.create.assert_not_called()
        self.memory.finish_turn.assert_not_called()

    def test_model_failure_closes_owned_components(self):
        client, _ = fake_client([])
        client.chat.completions.create.side_effect = RuntimeError(
            "fixture model failure"
        )
        with (
            capability_scope(Capabilities({"tasks", "background"})),
            self.owned_resources() as created,
        ):
            with self.assertRaises(RuntimeError):
                self.user(client)
        self.assert_closed(created)
        self.memory.finish_turn.assert_not_called()

    def test_partial_initialization_failure_closes_previously_created_components(self):
        client, _ = fake_client([])
        with (
            self.owned_resources() as created,
            patch(
                "homebrew_harness.runtime.assembly.CronScheduler",
                side_effect=RuntimeError("fixture cron initialization"),
            ),
        ):
            with self.assertRaises(RuntimeError):
                homebrew_harness.runtime.loop.agent_loop(
                    client,
                    "fixture",
                    [{"role": "user", "content": "fixture"}],
                    self.root,
                    capabilities=Capabilities({"tasks", "background", "cron"}),
                )
        self.assertEqual(set(created), {"tests", "shell", "tasks", "background"})
        self.assert_closed(created)
        client.chat.completions.create.assert_not_called()

    def test_cleanup_failure_does_not_mask_recall_error_or_skip_other_closes(self):
        client, _ = fake_client([])
        error = RuntimeError("fixture recall")
        self.memory.recall.side_effect = error
        close = BackgroundManager.close

        def fail_after_close(value):
            close(value)
            raise ValueError("fixture close")

        with (
            capability_scope(Capabilities({"tasks", "background"})),
            self.owned_resources() as created,
            patch.object(BackgroundManager, "close", fail_after_close),
        ):
            with self.assertRaises(RuntimeError) as caught:
                self.user(client)
        self.assertIs(caught.exception, error)
        self.assertIn("background", error.loop_cleanup_errors)
        self.assert_closed(created)

    def test_cleanup_failure_after_normal_execution_is_reported(self):
        client, _ = fake_client([reply("done")])
        close = ShellRunner.close

        def fail_after_close(value):
            close(value)
            raise ValueError("fixture close")

        with (
            self.owned_resources() as created,
            patch.object(ShellRunner, "close", fail_after_close),
        ):
            with self.assertRaisesRegex(
                homebrew_harness.runtime.errors.AgentError, "cleanup failed: shell"
            ):
                homebrew_harness.runtime.loop.agent_loop(
                    client,
                    "fixture",
                    [{"role": "user", "content": "fixture"}],
                    self.root,
                    capabilities=Capabilities(),
                )
        self.assert_closed(created)

    def test_snapshot_failure_balances_lead_turn_without_closing_host(self):
        host = self.host(Capabilities({"tasks"}))
        client, _ = fake_client([])
        with patch(
            "homebrew_harness.tools.registry.build_tool_pool",
            side_effect=ValueError("fixture snapshot"),
        ):
            with self.assertRaises(ValueError):
                self.user(client, team_manager=host)
        self.assertEqual(host.lead_busy, 0)
        self.assertFalse(host.shell_runner._closed)
        self.assertFalse(host.test_runner.closed.is_set())
        self.memory.recall.assert_not_called()

    def test_scheduled_recall_failure_restores_delivery_and_original_intent(self):
        scheduler = CronScheduler(self.root)
        scheduler.schedule("* * * * *", "Scheduled fixture", recurring=False)
        scheduler.poll_due(datetime(2026, 1, 1, 12, 0))
        job = scheduler.take_pending()
        client, _ = fake_client([])
        compactor = homebrew_harness.context.compaction.ContextCompactor(
            client, "fixture", self.root
        )
        original = compactor.intent
        original.update("Original task")
        self.memory.recall.side_effect = RuntimeError("fixture recall")
        messages = [{"role": "system", "content": "Fixture instructions"}]
        with (
            capability_scope(Capabilities({"tasks", "background", "cron"})),
            self.owned_resources() as created,
        ):
            with self.assertRaises(RuntimeError):
                homebrew_harness.runtime.turns.run_scheduled_turn(
                    job,
                    client=client,
                    model="fixture",
                    messages=messages,
                    workdir=self.root,
                    scheduler=scheduler,
                    memory_runtime=self.memory,
                    components=LoopComponents(
                        hooks=self.hooks, skill_loader=self.skills, compactor=compactor
                    ),
                )
        self.assertIs(compactor.intent, original)
        self.assertEqual(scheduler.take_pending().id, job.id)
        self.assertEqual(
            messages, [{"role": "system", "content": "Fixture instructions"}]
        )
        self.assert_closed(created)
        client.chat.completions.create.assert_not_called()

    def test_team_recall_failure_is_retryable_and_balances_lead_turn(self):
        host = self.host(Capabilities({"tasks", "background"}))
        client, _ = fake_client([])
        self.memory.recall.side_effect = RuntimeError("fixture recall")
        with self.owned_resources() as created:
            with self.assertRaises(
                homebrew_harness.orchestration.teams.TeamTurnError
            ) as caught:
                homebrew_harness.runtime.turns.run_team_turn(
                    [],
                    client=client,
                    model="fixture",
                    messages=[],
                    workdir=self.root,
                    memory_runtime=self.memory,
                    components=LoopComponents(
                        team_manager=host,
                        hooks=self.hooks,
                        skill_loader=self.skills,
                        compactor=homebrew_harness.context.compaction.ContextCompactor(
                            client, "fixture", self.root
                        ),
                    ),
                )
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(host.lead_busy, 0)
        self.assert_closed(created)
        client.chat.completions.create.assert_not_called()

    def test_subagent_never_constructs_parent_defaults(self):
        client, _ = fake_client([reply("done")])
        with ExitStack() as stack:
            for name in (
                "TodoManager",
                "TaskStore",
                "BackgroundManager",
                "CronScheduler",
            ):
                stack.enter_context(
                    patch(
                        "homebrew_harness.runtime.assembly." + name,
                        side_effect=AssertionError("child constructed " + name),
                    )
                )
            result = homebrew_harness.runtime.loop.agent_loop(
                client,
                "fixture",
                [{"role": "user", "content": "fixture"}],
                self.root,
                subagent=True,
                capabilities=Capabilities.full(memory_mode="off"),
            )
        self.assertEqual(result, "done")
        names = {
            t["function"]["name"]
            for t in client.chat.completions.create.call_args.kwargs["tools"]
        }
        self.assertFalse(
            names
            & {"todo_write", "task", "create_task", "check_background", "schedule_cron"}
        )

    def test_teammate_reuses_host_tests_shell_and_plan_without_background_expansion(
        self,
    ):
        host = self.host(Capabilities.full(memory_mode="off"))
        member = SimpleNamespace(
            manager=host,
            name="worker",
            version=0,
            user_revision=0,
            todo_manager=homebrew_harness.context.todo.TodoManager(),
            store=None,
            handlers=lambda: {},
            before_model=lambda: None,
            assignment=None,
            allowed_tasks=frozenset(),
            gate="open",
        )
        client, _ = fake_client([reply("done")])
        with self.capture(), self.owned_resources() as created:
            result = homebrew_harness.runtime.loop.agent_loop(
                client,
                "fixture",
                [{"role": "user", "content": "fixture"}],
                self.root,
                team_member=member,
            )
        self.assertEqual(result, "done")
        self.assertFalse(created)
        self.assertIs(self.components[0].test_runner, host.test_runner)
        self.assertIs(self.components[0].shell_runner, host.shell_runner)
        self.assertIs(self.components[0].todo_manager, member.todo_manager)
        names = self.pools[0].names
        self.assertFalse(
            names
            & {
                "task",
                "create_task",
                "schedule_cron",
                "connect_mcp",
                "workflow",
                "check_background",
            }
        )
        bash = next(
            t["function"]
            for t in self.pools[0].tools
            if t["function"]["name"] == "bash"
        )
        self.assertNotIn("run_in_background", bash["parameters"]["properties"])

    def test_recall_and_first_dispatch_keep_snapshot_then_mcp_refreshes(self):
        definitions = [
            homebrew_harness.tools.catalog.define_tool(
                "mcp__fixture__read", version, {}, []
            )
            for version in ("first", "next")
        ]
        bindings = [
            SimpleNamespace(
                definition=definition, policy=ToolPolicy("allow", True), validate=Mock()
            )
            for definition in definitions
        ]
        current = [0]
        runtime = SimpleNamespace(
            specs={}, call=Mock(return_value="fixture result"), run_connect=Mock()
        )
        runtime.snapshot = Mock(
            side_effect=lambda: (
                [definitions[current[0]]],
                {"mcp__fixture__read": bindings[current[0]]},
            )
        )

        def recall(*args, **kwargs):
            current[0] = 1
            return ""

        self.memory.recall.side_effect = recall
        client, _ = fake_client(
            [
                reply(calls=[call("first", name="mcp__fixture__read", arguments="{}")]),
                reply(calls=[call("next", name="mcp__fixture__read", arguments="{}")]),
                reply("done"),
            ]
        )
        with self.capture():
            result, _, _ = self.user(client, mcp_runtime=runtime)
        self.assertEqual(result, "done")
        self.assertEqual(runtime.snapshot.call_count, 3)
        requests = client.chat.completions.create.call_args_list
        self.assertEqual(
            next(
                t["function"]["description"]
                for t in requests[0].kwargs["tools"]
                if t["function"]["name"] == "mcp__fixture__read"
            ),
            "first",
        )
        self.assertEqual(
            next(
                t["function"]["description"]
                for t in requests[1].kwargs["tools"]
                if t["function"]["name"] == "mcp__fixture__read"
            ),
            "next",
        )
        self.assertIs(runtime.call.call_args_list[0].args[0], bindings[0])
        self.assertIs(runtime.call.call_args_list[1].args[0], bindings[1])

    def test_connect_mcp_discovers_tools_on_following_round(self):
        definition = homebrew_harness.tools.catalog.define_tool(
            "mcp__fixture__read", "fixture", {}, []
        )
        connected = [False]
        runtime = SimpleNamespace(specs={"fixture": object()}, call=Mock())
        runtime.snapshot = Mock(
            side_effect=lambda: (
                ([definition], {"mcp__fixture__read": object()})
                if connected[0]
                else ([], {})
            )
        )

        def connect(**kwargs):
            connected[0] = True
            return "connected"

        runtime.run_connect = Mock(side_effect=connect)
        client, _ = fake_client(
            [
                reply(
                    calls=[
                        call(
                            "connect",
                            name="connect_mcp",
                            arguments='{"name":"fixture"}',
                        )
                    ]
                ),
                reply("done"),
            ]
        )
        with (
            self.capture(),
            patch(
                "homebrew_harness.security.permissions.ask_user",
                return_value=PermissionDecision("allow", "fixture"),
            ),
        ):
            result, _, _ = self.user(client, mcp_runtime=runtime)
        self.assertEqual(result, "done")
        self.assertNotIn("mcp__fixture__read", self.pools[0].names)
        self.assertIn("mcp__fixture__read", self.pools[1].names)
        self.assertEqual(runtime.snapshot.call_count, 2)
        runtime.run_connect.assert_called_once()
