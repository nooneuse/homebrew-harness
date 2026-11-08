import json, threading, time, unittest
from unittest.mock import patch
from tests.runs import test_managed_team as fixture
from tests.support.clients import reply, call
from homebrew_harness.runs import RunSpec


def one_member():
    data = fixture.team_spec().data()
    data["members"] = data["members"][:1]
    data["stages"] = [data["stages"][0], data["stages"][2]]
    data["stages"][1]["depends_on"] = ["alpha"]
    data["stages"][1]["integrates"] = ["alpha"]
    data["stages"][1]["acceptance"]["predicates"] = data["stages"][1]["acceptance"][
        "predicates"
    ][:1]
    data["goal"]["predicates"] = data["goal"]["predicates"][:1]
    return RunSpec.from_dict(data)


class MemberControlTests(unittest.TestCase):
    setUp = fixture.ManagedTeamTests.setUp
    setup_run = fixture.ManagedTeamTests.setup_run

    def model(self, c, s, client, *, pause_first=False):
        child = [0]
        sent = [False]
        lead_wait = [False]

        def respond(**kwargs):
            actor = (
                "alpha"
                if "You are teammate alpha" in json.dumps(kwargs["messages"])
                else "lead"
            )
            tasks = {v["stage"]["key"]: v for v in c.stages.records().values()}

            def tool(name, args):
                return reply(
                    calls=[
                        call(
                            actor + str(child[0]) + name,
                            name=name,
                            arguments=json.dumps(args),
                        )
                    ]
                )

            if actor == "alpha":
                step = child[0]
                child[0] += 1
                if pause_first and step == 0:
                    return reply("Need further direction to continue.")
                step -= int(pause_first)
                if step == 0:
                    return tool(
                        "write_file", {"path": "alpha.txt", "content": "alpha done"}
                    )
                if step == 1:
                    return tool("complete_task", {"task_id": tasks["alpha"]["id"]})
                raise AssertionError("A verified member requested another model turn")
            if tasks["alpha"]["status"] != "completed":
                if (
                    pause_first
                    and not sent[0]
                    and s.team.members["alpha"].status == "idle"
                ):
                    if not lead_wait[0]:
                        lead_wait[0] = True
                        return reply("Waiting for member results.")
                    sent[0] = True
                    return tool(
                        "send_message",
                        {
                            "to": "alpha",
                            "content": "Continue the assigned task: write alpha done and submit it.",
                        },
                    )
                return reply("Waiting for actual member work.")
            join = tasks["join"]
            if join["status"] == "pending":
                return tool("claim_task", {"task_id": join["id"]})
            records = list(c.store.runtime.items("run_transfers").values())
            if not any(r["kind"] == "integrate" for r in records):
                return tool("integrate_stage", {"task_id": join["id"]})
            if join["status"] == "in_progress":
                return tool("complete_task", {"task_id": join["id"]})
            return tool("publish_stage", {"task_id": join["id"]})

        client.chat.completions.create.side_effect = respond
        return child, sent, lead_wait

    def start(self, c, s):
        args = c.team_control.arguments("alpha")
        c.team_control.authorize(args)
        s.team.spawn(**args)
        return s.team.members["alpha"]

    def test_steering_waits_in_same_thread_and_wake_is_atomic_with_input(self):
        c, s, client = self.setup_run(one_member())
        c.prepare()
        self.store.submit(c.id, "Preserve the output contract")
        child, _, _ = self.model(c, s, client)
        member = self.start(c, s)
        end = time.monotonic() + 3
        while member.status != "waiting_input" and time.monotonic() < end:
            time.sleep(0.01)
        self.assertEqual(member.status, "waiting_input")
        original_thread = member.thread
        c.team_control.tick()
        self.assertIs(s.team.members["alpha"], member)
        self.assertEqual(child[0], 0)
        original = s._checkpoint_writer.save
        gaps = []

        def save(*args, **kwargs):
            result = original(*args, **kwargs)
            if (kwargs.get("boundary") or {}).get("receipts"):
                gaps.append(not c.team_control.input_ready(member))
                self.assertTrue(
                    any(
                        r["type"] == "input_applied"
                        for r in self.store.runtime.mailbox("inspect", "alpha")
                    )
                )
            return result

        with patch.object(s._checkpoint_writer, "save", side_effect=save):
            outcome = c.execute()
        self.assertEqual(outcome.status, "completed", outcome)
        self.assertEqual(gaps, [True])
        self.assertIs(member.thread, original_thread)
        record = self.store.runtime.get("run_members", c.id + ":alpha")
        self.assertEqual(record["attempts"], 1)
        self.assertEqual(record["recoveries"], 0)
        self.assertIn("Preserve the output contract", s.intent.amendments[-1])

    def test_idle_work_segment_returns_to_lead_direction_then_completes(self):
        c, s, client = self.setup_run(one_member())
        c.prepare()
        child, sent, waited = self.model(c, s, client, pause_first=True)
        member = self.start(c, s)
        end = time.monotonic() + 3
        while member.status != "idle" and time.monotonic() < end:
            time.sleep(0.01)
        self.assertEqual(member.last_outcome["kind"], "needs_direction")
        outcome = c.execute()
        self.assertEqual(outcome.status, "completed", outcome)
        self.assertTrue(sent[0])
        self.assertTrue(waited[0])
        self.assertEqual(child[0], 3)
        self.assertEqual(
            self.store.runtime.get("run_members", c.id + ":alpha")["recoveries"], 0
        )
        self.assertEqual((self.root / "alpha.txt").read_text(), "alpha done")

    def test_input_wait_outcome_and_checkpoint_ack_commit_together(self):
        c, s, client = self.setup_run(one_member())
        c.prepare()
        self.store.submit(c.id, "A clarification")
        self.start(c, s)
        end = time.monotonic() + 3
        record = None
        while record is None and time.monotonic() < end:
            record = self.store.runtime.get("run_member_outcomes", c.id + ":alpha")
            time.sleep(0.01)
        self.assertEqual(record["kind"], "waiting_input")
        saved = self.store.runtime.get("checkpoints", record["result_ref"])
        self.assertEqual(
            saved["team"]["members"]["alpha"]["last_outcome"]["id"], record["id"]
        )
        self.assertEqual(client.chat.completions.create.call_count, 0)

    def test_episode_yield_waits_for_inflight_member_before_applying_steering(self):
        from homebrew_harness.runs.spec import EpisodeYield

        data = one_member().data()
        data["episode_model_calls"] = 1
        c, s, client = self.setup_run(RunSpec.from_dict(data))
        c.prepare()
        self.model(c, s, client)
        normal = client.chat.completions.create.side_effect
        started, release, yielded = (threading.Event() for _ in range(3))
        first = [True]

        def respond(**kwargs):
            is_member = "You are teammate alpha" in json.dumps(kwargs["messages"])
            if is_member and first[0]:
                first[0] = False
                started.set()
                if not release.wait(3):
                    raise AssertionError("Member response was not released")
                return reply(
                    calls=[
                        call(
                            "obsolete",
                            name="write_file",
                            arguments=json.dumps(
                                {"path": "alpha.txt", "content": "obsolete"}
                            ),
                        )
                    ]
                )
            if not is_member:
                self.assertTrue(release.is_set())
            return normal(**kwargs)

        client.chat.completions.create.side_effect = respond
        member = self.start(c, s)
        self.addCleanup(release.set)
        self.assertTrue(started.wait(3))
        self.store.submit(c.id, "Keep the assigned output contract")
        original_thread = member.thread
        run_episode, finish = s.run_episode, c.finish
        first_episode = [True]

        def episode():
            if first_episode[0]:
                first_episode[0] = False
                raise EpisodeYield()
            return run_episode()

        def finish_segment(*args, **kwargs):
            result = finish(*args, **kwargs)
            if args[0] == "yielded":
                yielded.set()
            return result

        def unblock():
            if yielded.wait(3):
                time.sleep(0.15)
            release.set()

        worker = threading.Thread(target=unblock)
        worker.start()
        self.addCleanup(worker.join, 3)
        with (
            patch.object(s, "run_episode", side_effect=episode),
            patch.object(c, "finish", side_effect=finish_segment),
        ):
            outcome = c.execute()
        self.assertEqual(outcome.status, "completed", outcome)
        self.assertIs(member.thread, original_thread)
        self.assertEqual((self.root / "alpha.txt").read_text(), "alpha done")
        self.assertEqual(
            self.store.runtime.get("run_members", c.id + ":alpha")["recoveries"], 0
        )
        self.assertIn("Keep the assigned output contract", s.intent.amendments)
        self.assertTrue(
            any(
                message.get("tool_call_id") == "obsolete"
                and "superseded" in message.get("content", "").lower()
                for message in member.messages
            )
        )
