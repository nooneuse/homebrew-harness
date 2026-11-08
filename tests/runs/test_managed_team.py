import io
import json
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

from homebrew_harness import HarnessSession, Capabilities
from homebrew_harness.runs import RunSpec, RunStore, RunController
from homebrew_harness.execution.model_protocol import ProviderError
from tests.support.clients import fake_client, reply, call


def team_spec():
    def acceptance(path, text):
        return {
            "condition": "Expected content",
            "judge": False,
            "predicates": [
                {"id": "content", "type": "file_contains", "path": path, "text": text}
            ],
        }

    return RunSpec(
        "Implement alpha and beta in owned files, integrate and publish the verified result.",
        context_chars=80000,
        episode_model_calls=3,
        total_seconds=30,
        shutdown_seconds=3,
        write_paths=("alpha.txt", "beta.txt"),
        members=(
            {
                "name": "alpha",
                "role": "writer",
                "prompt": "Write alpha.txt as alpha done; submit the assigned stage.",
                "retries": 1,
            },
            {
                "name": "beta",
                "role": "writer",
                "prompt": "Write beta.txt as beta done; submit the assigned stage.",
                "retries": 1,
            },
        ),
        stages=(
            {
                "key": "alpha",
                "subject": "Implement alpha",
                "executor": "alpha",
                "isolated": True,
                "write_paths": ["alpha.txt"],
                "acceptance": acceptance("alpha.txt", "alpha done"),
            },
            {
                "key": "beta",
                "subject": "Implement beta",
                "executor": "beta",
                "isolated": True,
                "write_paths": ["beta.txt"],
                "acceptance": acceptance("beta.txt", "beta done"),
            },
            {
                "key": "join",
                "subject": "Integrate",
                "isolated": True,
                "depends_on": ["alpha", "beta"],
                "integrates": ["alpha", "beta"],
                "write_paths": ["alpha.txt", "beta.txt"],
                "publish": True,
                "acceptance": {
                    "condition": "Both outputs integrated",
                    "judge": False,
                    "predicates": [
                        {
                            "id": "alpha",
                            "type": "file_contains",
                            "path": "alpha.txt",
                            "text": "alpha done",
                        },
                        {
                            "id": "beta",
                            "type": "file_contains",
                            "path": "beta.txt",
                            "text": "beta done",
                        },
                    ],
                },
            },
        ),
        goal={
            "condition": "Published result",
            "judge": False,
            "predicates": [
                {
                    "id": "alpha",
                    "type": "file_contains",
                    "path": "alpha.txt",
                    "text": "alpha done",
                },
                {
                    "id": "beta",
                    "type": "file_contains",
                    "path": "beta.txt",
                    "text": "beta done",
                },
            ],
        },
    )


class ManagedTeamTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        (self.root / ".gitignore").write_text(".harness/\n.worktrees/\n")
        for name in ("alpha", "beta"):
            (self.root / (name + ".txt")).write_text("initial")
        for args in [("init", "-q"), ("add", "."), ("commit", "-qm", "fixture")]:
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=fixture",
                    "-c",
                    "user.email=fixture@invalid",
                    "-c",
                    "commit.gpgsign=false",
                    "-c",
                    "core.hooksPath=/dev/null",
                    *args,
                ],
                cwd=self.root,
                check=True,
                capture_output=True,
            )
        self.store = RunStore(self.root)
        self.addCleanup(self.store.close)
        for context in (redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)

    def setup_run(self, spec=None):
        state = self.store.claim(self.store.create(spec or team_spec())["id"])
        control = RunController(
            self.store, state["id"], state["owner"], state["generation"]
        )
        client, requests = fake_client([])
        session = HarnessSession(
            client,
            "fixture",
            self.root,
            capabilities=Capabilities(control.spec.enabled),
            run_control=control,
            shutdown_timeout=3,
        )
        self.addCleanup(control.close_heartbeat)
        self.addCleanup(lambda: self.assertTrue(session.wait_closed(5)))
        self.addCleanup(session.close)
        return control, session, client

    def test_team_recovers_one_member_and_integrates_without_root_writes(self):
        control, session, client = self.setup_run()
        steps = {"lead": 0, "alpha": 0, "beta": 0}
        lock = threading.Lock()
        requests = []

        def response(**kwargs):
            raw = json.dumps(kwargs["messages"])
            actor = (
                "alpha"
                if "You are teammate alpha" in raw
                else "beta"
                if "You are teammate beta" in raw
                else "lead"
            )
            with lock:
                n = steps[actor]
                steps[actor] += 1
                requests.append(actor)
            tasks = {v["stage"]["key"]: v for v in control.stages.records().values()}

            def tool(name, args):
                return reply(
                    calls=[call(actor + str(n), name=name, arguments=json.dumps(args))]
                )

            if actor != "lead":
                if actor == "alpha" and n == 0:
                    raise ProviderError("transport")
                index = n - 1 if actor == "alpha" else n
                if index == 0:
                    return tool(
                        "write_file",
                        {"path": actor + ".txt", "content": actor + " done"},
                    )
                if index == 1:
                    return tool("complete_task", {"task_id": tasks[actor]["id"]})
                return reply("Verified output ready.")
            if n == 0:
                return tool("spawn_teammate", control.team_control.arguments("alpha"))
            if n == 1:
                return tool("spawn_teammate", control.team_control.arguments("beta"))
            if any(tasks[name]["status"] != "completed" for name in ("alpha", "beta")):
                return reply("Awaiting member results.")
            join = tasks["join"]
            if join["status"] == "pending":
                return tool("claim_task", {"task_id": join["id"]})
            transfers = list(self.store.runtime.items("run_transfers").values())
            if not any(t["kind"] == "integrate" for t in transfers):
                return tool("integrate_stage", {"task_id": join["id"]})
            if join["status"] == "in_progress":
                return tool("complete_task", {"task_id": join["id"]})
            if not any(t["kind"] == "publish" for t in transfers):
                return tool("publish_stage", {"task_id": join["id"]})
            return reply("Published verified output.")

        client.chat.completions.create.side_effect = response
        outcome = control.execute()
        self.assertEqual(
            outcome.status,
            "completed",
            (
                outcome,
                [m for m in session.messages if m.get("role") == "tool"],
                {
                    n: [m for m in member.messages if m.get("role") == "tool"]
                    for n, member in session.team.members.items()
                },
            ),
        )
        self.assertEqual((self.root / "alpha.txt").read_text(), "alpha done")
        self.assertEqual((self.root / "beta.txt").read_text(), "beta done")
        record = self.store.runtime.get("run_members", control.id + ":alpha")
        self.assertEqual(record["attempts"], 2)
        self.assertEqual(record["recoveries"], 1)
        self.assertTrue(
            all(t.status == "completed" for t in session.workspace.store.list())
        )
        self.assertLess(len(requests), 30)

    def test_members_have_fixed_disjoint_ownership(self):
        control, session, _ = self.setup_run()
        control.prepare()
        for name in ("alpha", "beta"):
            task = control.team_control.task_for(name)
            cwd = session.workspace.store.workspace(task)
            self.assertFalse(control.write_allowed(cwd, name + ".txt"))
        self.assertFalse(control.write_allowed(self.root, "alpha.txt"))
        wrong = control.team_control.arguments("alpha")
        wrong["prompt"] = "A different instruction"
        with self.assertRaisesRegex(ValueError, "declared"):
            control.team_control.authorize(wrong)

    def accepted_members(self):
        from homebrew_harness.orchestration.task_store import TaskStore
        from homebrew_harness.runs.stages import MemberStageAccess

        control, session, client = self.setup_run()
        control.prepare()
        session.intent.update(control.spec.prompt, new_task=True)
        for name in ("alpha", "beta"):
            task = control.team_control.task_for(name)
            store = TaskStore(self.root, owner=name, leases=session.workspace.leases)
            store.intent = session.intent
            store.stage_control = MemberStageAccess(control.stages, name, store)
            self.addCleanup(store.close)
            control.team_control._tokens[name] = session.inputs.token()
            store.claim(task.id)
            (store.workspace(task) / (name + ".txt")).write_text(name + " done")
            store.complete(task.id)
        join = next(
            t for t in session.workspace.store.list() if t.stage["key"] == "join"
        )
        session.workspace.lead_assignment = session.workspace.store.claim(join.id)
        return control, session, join

    def test_integration_preflight_preserves_all_targets_on_conflict(self):
        control, session, join = self.accepted_members()
        target = session.workspace.store.workspace(join)
        (target / "beta.txt").write_text("host change")
        with self.assertRaisesRegex(ValueError, "baseline"):
            control.transfers.integrate(workdir=target, task_id=join.id)
        self.assertEqual((target / "alpha.txt").read_text(), "initial")
        self.assertEqual((target / "beta.txt").read_text(), "host change")
        self.assertEqual(self.store.runtime.items("run_transfers"), {})

    def test_partial_transfer_requires_actual_file_inspection(self):
        from unittest.mock import patch
        from homebrew_harness.security.safe_files import SafeFile
        from homebrew_harness.tools.tool_outcome import ToolExecutionError
        from homebrew_harness.runs.transfers import resolve_transfer

        control, session, join = self.accepted_members()
        target = session.workspace.store.workspace(join)
        original = SafeFile.publish

        def fault(item, *args, **kwargs):
            if item.root == target and item.parts[-1] == "beta.txt":
                raise OSError("injected publication fault")
            return original(item, *args, **kwargs)

        with (
            patch.object(SafeFile, "publish", fault),
            self.assertRaises(ToolExecutionError),
        ):
            control.transfers.integrate(workdir=target, task_id=join.id)
        record = next(iter(self.store.runtime.items("run_transfers").values()))
        self.assertEqual(record["state"], "partial")
        self.assertEqual(record["applied"], ["alpha.txt"])
        session.close()
        self.assertTrue(session.wait_closed(5))
        self.store.update(control.header, {"status": "blocked"})
        self.store.ended(control.header, cleanup=True)
        with self.assertRaisesRegex(ValueError, "target files"):
            resolve_transfer(
                self.store, control.id, record["id"], record["revision"], "applied"
            )
        (target / "beta.txt").write_text("beta done")
        self.assertTrue(
            resolve_transfer(
                self.store, control.id, record["id"], record["revision"], "applied"
            )["resolved"]
        )
        self.assertEqual(
            self.store.runtime.get("run_transfers", record["id"])["state"], "done"
        )

    def test_unknown_member_operation_is_never_restarted(self):
        control, session, _ = self.setup_run()
        control.prepare()
        task = control.team_control.task_for("alpha")
        key = "member:" + session.session_id + ":alpha"
        self.store.runtime.put(
            "operations",
            "unknown",
            {
                "checkpoint": key,
                "call_id": "unknown",
                "state": "unknown",
                "revision": 0,
            },
        )
        self.assertTrue(control.team_control.unknown("alpha"))
        with self.assertRaises(Exception):
            control.team_control.before_spawn(
                {**control.team_control.arguments("alpha"), "model_profile": None}
            )
        self.assertEqual(self.store.runtime.items("run_members"), {})
        self.assertEqual(session.workspace.store.load(task.id).status, "pending")

    def test_pending_input_cannot_become_a_fresh_token_for_old_member_constraints(self):
        from types import SimpleNamespace
        from homebrew_harness.storage.control_boundary import ProposalSuperseded

        control, session, _ = self.setup_run()
        control.prepare()
        member = SimpleNamespace(name="alpha")
        control.team_control.before_model(member)
        self.store.submit(control.id, "Preserve a new requirement")
        with self.assertRaises(ProposalSuperseded):
            control.team_control.verify_before_model(member)
        with self.assertRaises(ProposalSuperseded):
            control.team_control.before_model(member)

    def test_recovery_limit_persists_and_completed_records_are_not_new_attempts(self):
        control, session, _ = self.setup_run()
        control.prepare()
        control.team_control._start_record("alpha")
        control.team_control._start_record("alpha", recovery=True)
        with self.assertRaisesRegex(Exception, "member_recovery_exhausted"):
            control.team_control._start_record("alpha", recovery=True)
        record = self.store.runtime.get("run_members", control.id + ":alpha")
        self.assertEqual(record["recoveries"], 1)
        self.assertEqual(record["attempts"], 2)

    def test_transfer_cannot_bypass_fixed_test_protection(self):
        from homebrew_harness.execution.test_runner import TestRecipe
        import sys

        control, session, join = self.accepted_members()
        recipe = TestRecipe(
            "protected", (sys.executable, "-c", "pass"), locked_globs=("alpha.txt",)
        )
        session.tests.recipes["protected"] = recipe
        target = session.workspace.store.workspace(join)
        with self.assertRaisesRegex(ValueError, "protected"):
            control.transfers.integrate(workdir=target, task_id=join.id)
        self.assertEqual(self.store.runtime.items("run_transfers"), {})

    def test_preflight_does_not_expand_registered_test_roles(self):
        import sys
        from homebrew_harness.execution.test_runner import TestRecipe
        from homebrew_harness.runs.spec import RunStopped

        data = team_spec().data()
        data["allow_tests"] = ["restricted"]
        data["stages"][0]["acceptance"] = {
            "condition": "Run restricted check",
            "judge": False,
            "predicates": [
                {"id": "check", "type": "tests_passed", "recipe": "restricted"}
            ],
        }
        control, session, _ = self.setup_run(RunSpec.from_dict(data))
        session.tests.recipes["restricted"] = TestRecipe(
            "restricted", (sys.executable, "-c", "pass")
        )
        with self.assertRaisesRegex(RunStopped, "test_role_not_authorized"):
            control.prepare()
        self.assertTrue(
            all(task.worktree is None for task in session.workspace.store.list())
        )

    def test_isolated_member_scope_still_intersects_run_write_prefixes(self):
        from homebrew_harness.orchestration.task_store import TaskStore
        from homebrew_harness.runs.stages import MemberStageAccess

        data = team_spec().data()
        data["write_paths"] = ["alpha.txt"]
        control, session, _ = self.setup_run(RunSpec.from_dict(data))
        control.prepare()
        task = control.team_control.task_for("beta")
        store = TaskStore(self.root, owner="beta", leases=session.workspace.leases)
        store.stage_control = MemberStageAccess(control.stages, "beta", store)
        self.addCleanup(store.close)
        store.claim(task.id)
        self.assertFalse(control.write_allowed(store.workspace(task), "beta.txt"))
