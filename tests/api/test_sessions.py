"""Application-facing query identities, approvals, literals and real cleanup."""

import io
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path
from contextlib import redirect_stdout, redirect_stderr

from homebrew_harness import Harness, HostConfig
from homebrew_harness.api import ResultNotReady, StaleReference
from homebrew_harness.api import SessionOptions, SDKError
from tests.support.clients import fake_client, reply, call
from tests.runtime.test_interaction import eventually


class APISessions(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def host(self, responses, **opts):
        client, requests = fake_client(responses)
        client.close = lambda: None
        config = HostConfig.from_components(
            client=client, model="fixture", shutdown_seconds=3, **opts
        )
        host = Harness(workdir=self.root, config=config)
        self.addCleanup(host.close)
        return host, client, requests

    def test_results_do_not_follow_latest_query_and_request_replay_is_idempotent(self):
        host, client, _ = self.host([reply("first"), reply("second")])
        session = host.sessions.open()
        first = session.submit("First request", request_id="one")
        self.assertTrue(first.wait(5).condition_met)
        second = session.submit("Second request", request_id="two")
        self.assertTrue(second.wait(5).condition_met)
        self.assertEqual(first.result().text, "first")
        self.assertEqual(second.result().text, "second")
        self.assertEqual(first.id, session.submit("First request", request_id="one").id)
        self.assertEqual(client.chat.completions.create.call_count, 2)
        with self.assertRaises(ValueError):
            session.submit("Different request", request_id="one")

    def test_literals_and_sdk_default_output_do_not_act_as_console_commands(self):
        host, client, messages = self.host(
            [reply("literal command explained"), reply("ordinary yes")]
        )
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            session = host.sessions.open()
            turn = session.submit("/approve arbitrary yes")
            self.assertEqual(turn.wait(5).snapshot.state, "completed")
            yes = session.submit("yes")
            yes.wait(5)
            host.close()
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertTrue(
            any(m.get("content") == "/approve arbitrary yes" for m in messages[0])
        )

    def test_active_steering_discards_old_tool_and_retains_original_requirements(self):
        host, client, requests = self.host([])
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        count = 0

        def create(**kwargs):
            nonlocal count
            count += 1
            requests.append(kwargs["messages"])
            if count == 1:
                entered.set()
                release.wait(4)
                return reply(
                    calls=[
                        call(
                            "stale",
                            name="write_file",
                            arguments='{"path":"bad.txt","content":"old"}',
                        )
                    ]
                )
            return reply("Updated answer")

        client.chat.completions.create.side_effect = create
        session = host.sessions.open()
        turn = session.submit("Keep the public API")
        self.assertTrue(entered.wait(5))
        turn.steer("Also describe failure handling", request_id="steer-one")
        release.set()
        self.assertEqual(turn.wait(8).snapshot.state, "completed")
        self.assertFalse((self.root / "bad.txt").exists())
        self.assertEqual(session.runtime.intent.original, "Keep the public API")
        self.assertIn(
            "Also describe failure handling", session.runtime.intent.amendments
        )

    def test_explicit_approval_resolves_only_the_displayed_request(self):
        host, client, _ = self.host(
            [
                reply(
                    calls=[
                        call(
                            "shell",
                            name="bash",
                            arguments='{"command":"printf api-approved"}',
                        )
                    ]
                ),
                reply("Approved command finished"),
            ]
        )
        session = host.sessions.open()
        turn = session.submit("Run the approved command")
        request = eventually(lambda: turn.requests() and turn.requests()[0])
        self.assertFalse(turn.wait(0).snapshot.settled)
        with self.assertRaises(StaleReference):
            from dataclasses import replace

            host.requests.approve(replace(request.ref, revision=1))
        host.requests.approve(request.ref, decision_id="explicit-one")
        self.assertEqual(turn.wait(5).snapshot.state, "completed")
        self.assertEqual(
            host.requests.approve(request.ref, decision_id="explicit-one")["state"],
            "approve",
        )

    def test_wait_timeout_does_not_cancel_work(self):
        host, client, _ = self.host([])
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def create(**_):
            entered.set()
            release.wait(4)
            return reply("done")

        client.chat.completions.create.side_effect = create
        session = host.sessions.open()
        turn = session.submit("Work")
        self.assertTrue(entered.wait(5))
        self.assertTrue(turn.wait(0.01).timed_out)
        with self.assertRaises(ResultNotReady):
            turn.result()
        release.set()
        self.assertEqual(turn.wait(5).snapshot.state, "completed")

    def test_subagent_tools_share_assembly_and_retain_independent_context(self):
        host, client, messages = self.host(
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
                            arguments='{"path":"child.txt","content":"done"}',
                        )
                    ]
                ),
                reply("Child done"),
                reply("Lead done"),
            ]
        )
        session = host.sessions.open(SessionOptions(enabled=("subagents",)))
        turn = session.submit("Delegate the local change")
        self.assertEqual(turn.wait(8).snapshot.state, "completed")
        self.assertEqual((self.root / "child.txt").read_text(), "done")
        self.assertFalse(
            any(
                m.get("tool_call_id") == "child-write" for m in session.runtime.messages
            )
        )

    def test_structured_goal_uses_the_native_file_verifier(self):
        host, client, _ = self.host(
            [
                reply(
                    calls=[
                        call(
                            "write",
                            name="write_file",
                            arguments='{"path":"result.txt","content":"verified"}',
                        )
                    ]
                ),
                reply("Done"),
            ]
        )
        goal = {
            "condition": "Result exists",
            "judge": False,
            "predicates": [
                {
                    "id": "result",
                    "type": "file_contains",
                    "path": "result.txt",
                    "text": "verified",
                }
            ],
        }
        session = host.sessions.open()
        turn = session.submit("Write the result", goal=goal)
        self.assertEqual(turn.wait(8).snapshot.state, "completed")
        self.assertEqual(session.goal_status()["status"], "achieved")

    def test_immediate_decision_replay_after_persistence_return_is_lost(self):
        host, client, _ = self.host(
            [
                reply(
                    calls=[
                        call(
                            "shell",
                            name="bash",
                            arguments='{"command":"printf decision"}',
                        )
                    ]
                ),
                reply("Done"),
            ]
        )
        session = host.sessions.open()
        turn = session.submit("Run a command")
        request = eventually(lambda: turn.requests() and turn.requests()[0])
        original = host.store.put

        def lost(section, *args, **kw):
            value = original(section, *args, **kw)
            if section == "api_decisions":
                raise OSError("return lost")
            return value

        with (
            patch.object(host.store, "put", side_effect=lost),
            self.assertRaises(SDKError),
        ):
            host.requests.approve(request.ref, decision_id="one")
        self.assertTrue(turn.requests())
        result = host.requests.approve(request.ref, decision_id="one")
        self.assertEqual(result["delivery"], "delivered")
        self.assertEqual(turn.wait(8).snapshot.state, "completed")

    def test_queued_steering_and_cancellation_stay_bound_to_the_original_query(self):
        host, client, messages = self.host([reply("first"), reply("second")])
        session = host.sessions.open()
        with patch.object(session.runtime, "start"):
            first = session.submit("First", request_id="one")
            first.steer("Keep the interface", request_id="amendment")
            cancelled = session.submit("Never execute", request_id="cancelled")
            cancelled.steer(
                "Never apply this amendment", request_id="cancelled-amendment"
            )
            cancelled.cancel()
            second = session.submit("Second", request_id="two")
            second.steer("Only amend the second request", request_id="second-amendment")
        from homebrew_harness.runtime.output import output_scope
        from homebrew_harness.runtime.input_router import approval_scope

        with output_scope(), approval_scope(session.broker):
            session.runtime.start(include_clock=False)
        self.assertEqual(first.wait(5).snapshot.state, "completed")
        self.assertEqual(second.wait(5).snapshot.state, "completed")
        self.assertEqual(cancelled.result().state, "cancelled")
        self.assertTrue(cancelled.wait(0, until="settled").condition_met)
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertTrue(any("Keep the interface" in str(m) for m in messages[0]))
        self.assertFalse(
            any("Only amend the second request" in str(m) for m in messages[0])
        )
        self.assertTrue(
            any("Only amend the second request" in str(m) for m in messages[1])
        )
        with self.assertRaises(StaleReference):
            first.steer("Too late")

    def test_active_cancel_does_not_close_the_session_or_execute_stale_tools(self):
        host, client, _ = self.host([])
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        count = 0

        def create(**_):
            nonlocal count
            count += 1
            if count == 1:
                entered.set()
                release.wait(4)
                return reply(
                    calls=[
                        call(
                            "stale",
                            name="write_file",
                            arguments='{"path":"bad.txt","content":"old"}',
                        )
                    ]
                )
            return reply("next")

        client.chat.completions.create.side_effect = create
        session = host.sessions.open()
        turn = session.submit("First")
        self.assertTrue(entered.wait(5))
        turn.cancel()
        release.set()
        self.assertEqual(turn.wait(5).snapshot.state, "cancelled")
        self.assertFalse((self.root / "bad.txt").exists())
        self.assertFalse(session.runtime._closed)
        next_turn = session.submit("Next")
        self.assertEqual(next_turn.wait(5).snapshot.state, "completed")

    def test_finished_turn_stays_settled_while_another_query_runs(self):
        host, client, _ = self.host([reply("first")])
        session = host.sessions.open()
        first = session.submit("First")
        self.assertEqual(first.wait(5).snapshot.state, "completed")
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def create(**_):
            entered.set()
            release.wait(5)
            return reply("second")

        client.chat.completions.create.side_effect = create
        second = session.submit("Second")
        self.assertTrue(entered.wait(5))
        self.assertTrue(first.wait(0, until="settled").condition_met)
        release.set()
        self.assertEqual(second.wait(5).snapshot.state, "completed")

    def test_get_is_read_only_and_resume_does_not_repeat_a_completed_query(self):
        host, client, _ = self.host([reply("done")])
        session = host.sessions.open()
        turn = session.submit("First")
        turn.wait(5)
        identity = session.id
        turnid = turn.id
        host.close()
        other = Harness(workdir=self.root, config=host.config)
        self.addCleanup(other.close)
        self.assertEqual(other.sessions.get(identity).snapshot().state, "closed")
        self.assertEqual(other.sessions.turn(turnid).result().text, "done")
        self.assertEqual(other.sessions.live, {})
        resumed = other.sessions.resume(identity)
        self.assertEqual(resumed.id, identity)
        self.assertEqual(client.chat.completions.create.call_count, 1)
