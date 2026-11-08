import json
from tests.runs import test_human_control as support
from tests.support.clients import reply, call
from homebrew_harness.runs.progress import ProgressTracker
from homebrew_harness.runs import RunSpec


class ProgressTests(support.ManagedRunFixture):
    def test_repeated_ineffective_writes_replan_then_block_across_episodes(self):
        writes = [
            reply(
                calls=[
                    call(
                        "write" + str(i),
                        name="write_file",
                        arguments=json.dumps({"path": "same.txt", "content": "same"}),
                    )
                ]
            )
            for i in range(7)
        ]
        control, session, api, _ = self.session(
            writes, spec=RunSpec("Keep writing same.txt", episode_model_calls=1)
        )
        self.assertEqual(control.execute().reason_code, "no_progress")
        state = ProgressTracker(self.store).get(control.id)
        self.assertTrue(state["blocked"])
        self.assertEqual(state["replans"], 1)
        self.assertEqual(api.chat.completions.create.call_count, 7)
        self.close(control, session)
        self.store.control(control.id, "run")
        resumed, again, client, _ = self.session([reply("done")], identity=control.id)
        self.assertEqual(resumed.execute().reason_code, "no_progress")
        client.chat.completions.create.assert_not_called()

    def test_readonly_analysis_is_not_counted_as_no_progress(self):
        (self.root / "info.txt").write_text("information")
        reads = [
            reply(
                calls=[
                    call(
                        "read" + str(i),
                        name="read_file",
                        arguments='{"path":"info.txt"}',
                    )
                ]
            )
            for i in range(10)
        ]
        control, _, _, _ = self.session([*reads, reply("done")])
        self.assertEqual(control.execute().status, "completed")
        state = ProgressTracker(self.store).get(control.id)
        self.assertEqual(state["count"], 0)
        self.assertIsNone(state["feedback"])
