import json, unittest
from tests.runs import test_managed_team as fixture
from homebrew_harness.runs import RunSpec
from homebrew_harness.orchestration.task_store import TaskStore
from homebrew_harness.runs.stages import MemberStageAccess
from tests.support.clients import reply, call


def input_spec():
    data = fixture.team_spec().data()
    # This fixture exercises real worktrees, transfer and both actor loops.
    data["total_seconds"] = 60
    data["stages"][1]["depends_on"] = ["alpha"]
    data["stages"][1]["inputs"] = [{"stage": "alpha", "paths": ["alpha.txt"]}]
    data["stages"][1]["acceptance"]["predicates"][0]["text"] = "alpha done consumed"
    data["stages"][2]["acceptance"]["predicates"][1]["text"] = "alpha done consumed"
    data["goal"]["predicates"][1]["text"] = "alpha done consumed"
    return RunSpec.from_dict(data)


class InputDeliveryTests(unittest.TestCase):
    setUp = fixture.ManagedTeamTests.setUp
    setup_run = fixture.ManagedTeamTests.setup_run

    def actor_store(self, c, s, name):
        store = TaskStore(self.root, owner=name, leases=s.workspace.leases)
        store.intent = s.intent
        store.stage_control = MemberStageAccess(c.stages, name, store)
        self.addCleanup(store.close)
        c.team_control._tokens[name] = s.inputs.token()
        return store

    def prepared(self):
        c, s, client = self.setup_run(input_spec())
        c.prepare()
        s.intent.update(c.spec.prompt, new_task=True)
        s._save_checkpoint()
        a = c.team_control.task_for("alpha")
        b = c.team_control.task_for("beta")
        own = self.actor_store(c, s, "alpha")
        own.claim(a.id)
        (own.workspace(a) / "alpha.txt").write_text("alpha done")
        own.complete(a.id)
        return c, s, a, b

    def test_input_arrives_before_claim_and_is_excluded_from_output_delta(self):
        c, s, a, b = self.prepared()
        self.assertEqual(
            (s.workspace.store.workspace(b) / "alpha.txt").read_text(), "initial"
        )
        c.stages.prepare_claim(b.id, actor="beta")
        own = self.actor_store(c, s, "beta")
        own.claim(b.id)
        self.assertEqual((own.workspace(b) / "alpha.txt").read_text(), "alpha done")
        self.assertFalse(c.write_allowed(own.workspace(b), "alpha.txt"))
        (own.workspace(b) / "beta.txt").write_text("alpha done consumed")
        accepted, _ = own.complete(b.id)
        submission = self.store.runtime.get(
            "stage_submissions", accepted.stage["submission"]
        )
        self.assertEqual(set(submission["outputs"]["files"]), {"beta.txt"})
        manifest = self.store.runtime.get(
            "stage_inputs", accepted.stage["input_manifest"]
        )
        self.assertEqual(
            manifest["parents"],
            {a.id: s.workspace.store.load(a.id).stage["submission"]},
        )
        self.assertTrue(c.stages.valid(accepted))

    def test_source_change_invalidates_then_refreshes_without_absorbing_outputs(self):
        c, s, a, b = self.prepared()
        c.stages.prepare_claim(b.id, actor="beta")
        own = self.actor_store(c, s, "beta")
        own.claim(b.id)
        (own.workspace(b) / "beta.txt").write_text("alpha done consumed")
        accepted, _ = own.complete(b.id)
        prior = accepted.stage["submission"]
        (s.workspace.store.workspace(a) / "alpha.txt").write_text("alpha done v2")
        c.stages.reconcile()
        self.assertEqual(s.workspace.store.load(b.id).status, "pending")
        source = self.actor_store(c, s, "alpha")
        source.claim(a.id)
        source.complete(a.id)
        c.stages.prepare_claim(b.id, actor="beta")
        self.assertEqual((own.workspace(b) / "alpha.txt").read_text(), "alpha done v2")
        self.assertEqual(
            (own.workspace(b) / "beta.txt").read_text(), "alpha done consumed"
        )
        self.assertEqual(
            c.transfers._changes(s.workspace.store.load(b.id))[2], ["beta.txt"]
        )
        self.assertIsNotNone(self.store.runtime.get("stage_submissions", prior))

    def test_input_conflict_is_rejected_without_overwriting_user_file(self):
        c, s, a, b = self.prepared()
        root = s.workspace.store.workspace(b)
        (root / "alpha.txt").write_text("host change")
        with self.assertRaisesRegex(ValueError, "baseline"):
            c.stages.prepare_claim(b.id, actor="beta")
        self.assertEqual((root / "alpha.txt").read_text(), "host change")
        self.assertIsNone(s.workspace.store.load(b.id).stage["input_manifest"])
        self.assertEqual(s.workspace.store.load(b.id).status, "pending")

    def test_full_dependent_team_reads_handoff_and_publishes_both_outputs(self):
        c, s, client = self.setup_run(input_spec())
        steps = {"alpha": 0, "beta": 0}
        denied = []

        def response(**kwargs):
            raw = json.dumps(kwargs["messages"])
            actor = (
                "alpha"
                if "You are teammate alpha" in raw
                else "beta"
                if "You are teammate beta" in raw
                else "lead"
            )
            tasks = {v["stage"]["key"]: v for v in c.stages.records().values()}

            def tool(name, args):
                return reply(
                    calls=[
                        call(
                            actor + name + str(steps.get(actor, 0)),
                            name=name,
                            arguments=json.dumps(args),
                        )
                    ]
                )

            if actor == "alpha":
                n = steps[actor]
                steps[actor] += 1
                return (
                    tool("write_file", {"path": "alpha.txt", "content": "alpha done"})
                    if n == 0
                    else tool("complete_task", {"task_id": tasks[actor]["id"]})
                )
            if actor == "beta":
                n = steps[actor]
                steps[actor] += 1
                root = s.workspace.store.workspace(
                    s.workspace.store.load(tasks[actor]["id"])
                )
                self.assertEqual((root / "alpha.txt").read_text(), "alpha done")
                if n == 0:
                    return tool(
                        "get_handoff",
                        {"handoff_id": tasks["alpha"]["stage"]["handoff"]},
                    )
                if n == 1:
                    return tool(
                        "write_file", {"path": "alpha.txt", "content": "bypass"}
                    )
                if n == 2:
                    denied.extend(
                        m
                        for m in kwargs["messages"]
                        if m.get("role") == "tool"
                        and "write_boundary" in m.get("content", "")
                    )
                    return tool(
                        "write_file",
                        {"path": "beta.txt", "content": "alpha done consumed"},
                    )
                return tool("complete_task", {"task_id": tasks[actor]["id"]})
            for name in ("alpha", "beta"):
                if name not in s.team.members:
                    if name == "beta" and tasks["alpha"]["status"] != "completed":
                        return reply("Waiting for alpha execution.")
                    return tool("spawn_teammate", c.team_control.arguments(name))
            if any(tasks[n]["status"] != "completed" for n in ("alpha", "beta")):
                return reply("Waiting for actual member work.")
            join = tasks["join"]
            transfers = list(self.store.runtime.items("run_transfers").values())
            if join["status"] == "pending":
                return tool("claim_task", {"task_id": join["id"]})
            if not any(r["kind"] == "integrate" for r in transfers):
                return tool("integrate_stage", {"task_id": join["id"]})
            if join["status"] == "in_progress":
                return tool("complete_task", {"task_id": join["id"]})
            return tool("publish_stage", {"task_id": join["id"]})

        client.chat.completions.create.side_effect = response
        outcome = c.execute()
        self.assertEqual(outcome.status, "completed", outcome)
        self.assertTrue(denied)
        self.assertEqual((self.root / "alpha.txt").read_text(), "alpha done")
        self.assertEqual((self.root / "beta.txt").read_text(), "alpha done consumed")

    def test_lost_input_manifest_commit_is_confirmed_without_replaying_copy(self):
        from contextlib import contextmanager
        from unittest.mock import patch

        c, s, a, b = self.prepared()
        db = self.store.runtime.backend()
        write = db.write
        put = db._put
        changed = [False]

        def mark(conn, section, key, value):
            result = put(conn, section, key, value)
            if section == "stage_inputs":
                changed[0] = True
            return result

        @contextmanager
        def lost():
            changed[0] = False
            with write() as conn:
                yield conn
            if changed[0]:
                raise OSError("input commit response lost")

        with (
            patch.object(db, "_put", side_effect=mark),
            patch.object(db, "write", lost),
        ):
            c.stages.prepare_claim(b.id, actor="beta")
        self.assertTrue(c.transfers.inputs_current(s.workspace.store.load(b.id)))
        self.assertEqual(
            sum(
                r["kind"] == "inputs"
                for r in self.store.runtime.items("run_transfers").values()
            ),
            1,
        )

    def test_partial_inputs_block_replay_until_actual_targets_are_inspected(self):
        from unittest.mock import patch
        from homebrew_harness.runs.transfers import resolve_transfer
        from homebrew_harness.security.safe_files import SafeFile
        from homebrew_harness.tools.tool_outcome import ToolExecutionError

        data = input_spec().data()
        data["stages"][0]["write_paths"] = ["alpha.txt", "extra.txt"]
        data["stages"][1]["inputs"][0]["paths"] = ["alpha.txt", "extra.txt"]
        data["write_paths"] = [*data["write_paths"], "extra.txt"]
        c, s, _ = self.setup_run(RunSpec.from_dict(data))
        c.prepare()
        s.intent.update(c.spec.prompt, new_task=True)
        s._save_checkpoint()
        a = c.team_control.task_for("alpha")
        b = c.team_control.task_for("beta")
        own = self.actor_store(c, s, "alpha")
        own.claim(a.id)
        root = own.workspace(a)
        (root / "alpha.txt").write_text("alpha done")
        (root / "extra.txt").write_text("extra")
        own.complete(a.id)
        target = s.workspace.store.workspace(b)
        publish = SafeFile.publish

        def fault(item, *args, **kwargs):
            if item.root == target and item.parts[-1] == "extra.txt":
                raise OSError("second input write failed")
            return publish(item, *args, **kwargs)

        with (
            patch.object(SafeFile, "publish", fault),
            self.assertRaises(ToolExecutionError),
        ):
            c.stages.prepare_claim(b.id, actor="beta")
        partial = next(
            r
            for r in self.store.runtime.items("run_transfers").values()
            if r["kind"] == "inputs"
        )
        self.assertEqual(partial["state"], "partial")
        with self.assertRaisesRegex(Exception, "transfer_recovery_required"):
            c.stages.prepare_claim(b.id, actor="beta")
        self.assertIsNone(s.workspace.store.load(b.id).stage["input_manifest"])
        s.close()
        self.assertTrue(s.wait_closed(5))
        self.store.update(c.header, {"status": "blocked"})
        self.store.ended(c.header, cleanup=True)
        (target / "extra.txt").write_text("extra")
        resolve_transfer(
            self.store, c.id, partial["id"], partial["revision"], "applied"
        )
        self.store.control(c.id, "run")
        # A fresh execution verifies already-applied bytes, then links the manifest.
        from homebrew_harness import HarnessSession, Capabilities
        from homebrew_harness.runs import RunController
        from tests.support.clients import fake_client

        state = self.store.claim(c.id)
        resumed = RunController(self.store, c.id, state["owner"], state["generation"])
        raw, _ = fake_client([])
        next_session = HarnessSession(
            raw,
            "fixture",
            self.root,
            capabilities=Capabilities(resumed.spec.enabled),
            run_control=resumed,
            shutdown_timeout=1,
        )
        self.addCleanup(next_session.close)
        self.addCleanup(resumed.close_heartbeat)
        resumed.prepare()
        resumed.stages.prepare_claim(b.id, actor="beta")
        self.assertTrue(
            resumed.transfers.inputs_current(next_session.workspace.store.load(b.id))
        )
        next_session.close()
        self.assertTrue(next_session.wait_closed(5))
