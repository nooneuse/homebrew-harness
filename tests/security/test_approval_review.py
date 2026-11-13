"""Independent approval judgments preserve native permission and request boundaries."""

import json
import threading
import time
import unittest
from types import SimpleNamespace

from homebrew_harness import HarnessSession
from homebrew_harness.execution.model_registry import ModelRegistry
from homebrew_harness.execution.model_gateway import ensure_gateway
from homebrew_harness.runtime.input_router import ApprovalBroker, approval_scope
from homebrew_harness.security.approval_review import (
    ApprovalReviewer,
    ReviewConfig,
)
from tests.runtime import test_slash_commands as helpers
from tests.support.clients import fake_client, reply, call, model_binding


def verdict(decision="approve", authorization="explicit"):
    return reply(
        json.dumps(
            {
                "decision": decision,
                "reason": "Exact action assessed against user goal",
                "risk": "low",
            }
        )
    )


class ApprovalReviewTests(unittest.TestCase):
    setUp = helpers.SlashTests.setUp

    def session(self, answers=(), **kwargs):
        client, payloads = fake_client(answers)
        session = HarnessSession(
            client,
            "fixture",
            self.root,
            approval_review=kwargs.pop("approval_review", {"mode": "auto_review"}),
            shutdown_timeout=0.2,
            **kwargs,
        )
        session._ensure_runtime()
        self.addCleanup(lambda: (session.close(), session.wait_closed(5)))
        return session, client, payloads

    def request(self):
        return {
            "id": "request-fixture",
            "revision": 0,
            "kind": "tool",
            "tool": "bash",
            "arguments": {"command": "printf ok"},
        }

    def context(self):
        return {
            "cwd": str(self.root),
            "intent": {"original": "Print ok", "amendments": []},
            "policy": "fixed",
        }

    def reviewer(self, answers, config=None):
        client, payloads = fake_client(answers)
        binding = model_binding(client, "review-model")
        gateway = ensure_gateway(client, workdir=self.root)
        from homebrew_harness.storage.durable_runtime import DurableStore

        reviewer = ApprovalReviewer(
            config or {"mode": "auto_review"},
            DurableStore(self.root),
            ModelRegistry(),
            gateway,
            lambda: binding,
        )
        self.addCleanup(
            lambda: (gateway.close(), gateway.wait_idle(5), reviewer.close())
        )
        return reviewer, client, payloads

    def test_config_supports_three_model_sources_and_rejects_invalid_scope(self):
        for source in ("lead", "requester", "profile"):
            config = {"mode": "auto_review", "model_source": source}
            if source == "profile":
                config["model_profile"] = "safety"
            self.assertEqual(ReviewConfig.parse(config).model_source, source)
        for config in (
            {"mode": "allow_all"},
            {"model_source": "profile"},
            {"kinds": ["attention"]},
            {"model_source": "lead", "model_profile": "safety"},
            {"timeout_seconds": float("nan")},
        ):
            with self.assertRaises(ValueError):
                ReviewConfig.parse(config)

    def test_rules_allow_and_hard_deny_never_call_reviewer(self):
        session, client, _ = self.session(
            [
                reply(
                    calls=[
                        call(
                            "read",
                            name="read_file",
                            arguments=json.dumps({"path": "source.txt"}),
                        ),
                        call("bad", command="sudo true"),
                    ]
                ),
                reply("Done"),
            ]
        )
        (self.root / "source.txt").write_text("source")
        session.run_user("Read source")
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertFalse(session.runtime_store.items("approval_reviews"))

    def test_automatic_approval_executes_exact_command(self):
        session, client, payloads = self.session(
            [reply(calls=[call("bash", command="printf ok")]), verdict(), reply("Done")]
        )
        self.assertEqual(session.run_user("Print ok"), "Done")
        output = json.loads(
            next(m["content"] for m in session.messages if m.get("role") == "tool")
        )
        self.assertEqual(output["stdout"], "ok")
        row = next(iter(session.runtime_store.items("approval_reviews").values()))
        self.assertEqual(row["state"], "approved")
        self.assertIn("Print ok", str(payloads[1]))
        self.assertNotIn("role=assistant", str(payloads[1]))

    def test_automatic_denial_does_not_execute(self):
        session, client, _ = self.session(
            [
                reply(calls=[call("bash", command="touch denied")]),
                verdict("deny", "insufficient"),
                reply("Denied"),
            ]
        )
        session.run_user("Discuss task")
        self.assertFalse((self.root / "denied").exists())
        self.assertEqual(
            next(iter(session.runtime_store.items("approval_reviews").values()))[
                "state"
            ],
            "denied",
        )

    def test_uncertainty_uses_same_manual_broker(self):
        session, client, _ = self.session(
            [
                reply(calls=[call("bash", command="printf ok")]),
                verdict("human_required", "unknown"),
                reply("Done"),
            ]
        )
        broker = ApprovalBroker(
            trusted=True, owner=session, announce=lambda _text: None
        )
        self.addCleanup(broker.close)
        output = []

        def run():
            with approval_scope(broker):
                output.append(session.run_user("Print ok"))

        worker = threading.Thread(target=run)
        worker.start()
        helpers.eventually(lambda: bool(broker.records()))
        helpers.eventually(
            lambda: any(
                r["state"] == "human_required"
                for r in session.runtime_store.items("approval_reviews").values()
            )
        )
        request = broker.records()[0]
        broker.respond(request["id"], True)
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(output, ["Done"])

    def test_legacy_authorization_field_is_corrected_before_approval(self):
        legacy = reply(json.dumps({'decision': 'approve', 'reason': 'Goal permits action',
                                  'authorization': 'unknown'}))
        reviewer, client, payloads = self.reviewer([legacy, verdict()])
        context = self.context()
        published = []
        reviewer.review(self.request(), context, lambda: context,
                        lambda value, identity: published.append(value))
        self.assertEqual(published, ['approve'])
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertIn('Do not include authorization', str(payloads[-1]))

    def test_changed_context_or_human_decision_invalidates_verdict(self):
        reviewer, client, _ = self.reviewer([verdict()])
        context = self.context()
        published = []
        self.assertIsNone(
            reviewer.review(
                self.request(),
                context,
                lambda: None,
                lambda *args: published.append(args),
            )
        )
        self.assertFalse(published)
        self.assertEqual(
            next(iter(reviewer.store.items("approval_reviews").values()))["state"],
            "superseded",
        )

    def test_bad_output_is_corrected_within_limit_and_explicit_retry_is_fresh(self):
        reviewer, client, payloads = self.reviewer([reply('not JSON')] * 3 + [verdict()])
        context = self.context()
        published = []
        for _ in range(2):
            reviewer.review(self.request(), context, lambda: context,
                            lambda value, identity: published.append(value))
        self.assertEqual(client.chat.completions.create.call_count, 3)
        self.assertEqual(published, [])
        reviewer.review(self.request(), context, lambda: context,
                        lambda value, identity: published.append(value), retry=True)
        self.assertEqual(published, ['approve'])
        self.assertEqual(client.chat.completions.create.call_count, 4)

    def test_requester_model_source_uses_actual_binding_and_explicit_fallback(self):
        reviewer, client, _ = self.reviewer(
            [verdict()], {"mode": "auto_review", "model_source": "requester"}
        )
        other, other_payloads = fake_client([verdict()])
        binding = model_binding(other, "worker-model")
        context = self.context()
        result = reviewer.review(
            self.request(),
            context,
            lambda: context,
            lambda decision, _id: decision,
            requester=binding,
        )
        self.assertEqual(result, "approve")
        self.assertEqual(client.chat.completions.create.call_count, 0)
        self.assertEqual(
            other.chat.completions.create.call_args.kwargs["model"], "worker-model"
        )
        missing, client, _ = self.reviewer(
            [verdict()], {"mode": "auto_review", "model_source": "requester"}
        )
        self.assertIsNone(
            missing.review(
                {**self.request(), "id": "missing"},
                context,
                lambda: context,
                lambda *args: True,
            )
        )
        self.assertEqual(client.chat.completions.create.call_count, 0)

    def test_profile_source_selects_host_registry_binding(self):
        reviewer, client, _ = self.reviewer(
            [verdict()],
            {
                "mode": "auto_review",
                "model_source": "profile",
                "model_profile": "safety",
            },
        )
        other, _ = fake_client([verdict()])
        binding = model_binding(other, "safety-model")
        reviewer.registry = SimpleNamespace(
            select=lambda alias: binding if alias == "safety" else None
        )
        context = self.context()
        reviewer.review(
            self.request(), context, lambda: context, lambda decision, _id: decision
        )
        self.assertEqual(client.chat.completions.create.call_count, 0)
        self.assertEqual(
            other.chat.completions.create.call_args.kwargs["model"], "safety-model"
        )

    def test_readonly_evidence_has_no_execution_tool(self):
        (self.root / "script.py").write_text('print("ok")')
        reads = reply(
            calls=[
                call(
                    "read",
                    name="read_review_file",
                    arguments=json.dumps({"path": "script.py"}),
                )
            ]
        )
        reviewer, client, payloads = self.reviewer([reads, verdict()])
        context = self.context()
        self.assertEqual(
            reviewer.review(
                self.request(), context, lambda: context, lambda decision, _id: decision
            ),
            "approve",
        )
        tools = client.chat.completions.create.call_args.kwargs["tools"]
        self.assertEqual([t["function"]["name"] for t in tools], ["read_review_file"])
        self.assertIn("print", str(payloads[-1]))

    def test_long_evidence_is_read_in_pages_before_native_approval(self):
        content = '证据🌱\r\n' * 2500
        (self.root / 'guide.md').write_bytes(content.encode())
        answers = [reply(calls=[call('first', name='read_review_file', arguments=json.dumps(
            {'path': 'guide.md'}))]), reply(calls=[call('second', name='read_review_file',
            arguments=json.dumps({'path': 'guide.md', 'offset': 10000}))]), verdict()]
        reviewer, client, payloads = self.reviewer(answers)
        context = self.context()
        self.assertEqual(reviewer.review(self.request(), context, lambda: context,
                         lambda decision, identity: decision), 'approve')
        pages = [json.loads(message['content']) for message in payloads[-1]
                 if message['role'] == 'tool']
        self.assertEqual(''.join(page['content'] for page in pages), content)
        self.assertEqual([page['read_complete'] for page in pages], [False, True])
        record = next(iter(reviewer.store.items('approval_reviews').values()))
        self.assertEqual(record['state'], 'approved')
        self.assertTrue(record['evidence'][0]['read_complete'])
        self.assertEqual(client.chat.completions.create.call_count, 3)

    def test_partial_evidence_sufficiency_is_the_models_decision(self):
        (self.root / 'guide').write_text('x' * 12000)
        for decision in ('approve', 'deny'):
            with self.subTest(decision=decision):
                reviewer, client, _ = self.reviewer([reply(calls=[call(
                    'read', name='read_review_file', arguments='{"path":"guide"}')]), verdict(decision)])
                context = self.context()
                published = []
                reviewer.review({**self.request(), 'id': decision}, context, lambda: context,
                                lambda value, identity: published.append(value))
                self.assertEqual(published, [decision])
                record = next(iter(reviewer.store.items('approval_reviews').values()))
                self.assertFalse(record['evidence'][0]['read_complete'])
                self.assertEqual(client.chat.completions.create.call_count, 2)

    def test_file_change_between_pages_is_reported_to_model(self):
        path = self.root / 'guide'
        path.write_text('original' * 2000)
        reviewer, client, _ = self.reviewer([])
        answers = iter([reply(calls=[call('first', name='read_review_file',
                                        arguments='{"path":"guide"}')]),
                        reply(calls=[call('second', name='read_review_file',
                                        arguments='{"path":"guide","offset":10000}')]), verdict()])
        def model(**kwargs):
            result = next(answers)
            if client.chat.completions.create.call_count == 2:
                path.write_text('modified' * 2000)
            return result
        client.chat.completions.create.side_effect = model
        context = self.context()
        published = []
        reviewer.review(self.request(), context, lambda: context,
                        lambda *args: published.append(args))
        self.assertEqual(len(published), 1)
        self.assertEqual(next(iter(reviewer.store.items('approval_reviews').values()))['state'],
                         'approved')

    def test_reviewer_cannot_read_private_state_or_run_bash(self):
        reviewer, client, _ = self.reviewer(
            [
                reply(
                    calls=[
                        call(
                            "bad",
                            name="bash",
                            arguments='{"command":"touch forbidden"}',
                        )
                    ]
                )
            ]
        )
        context = self.context()
        self.assertIsNone(
            reviewer.review(
                self.request(), context, lambda: context, lambda *args: True
            )
        )
        self.assertFalse((self.root / "forbidden").exists())

    def test_policy_change_after_approval_blocks_execution_at_dispatch(self):
        session, client, _ = self.session(
            [reply(calls=[call("bash", command="printf ok")]), verdict(), reply("Done")]
        )

        def change(event):
            if event.name == "bash":
                session.approval_reviewer.config = ReviewConfig(mode="manual")

        session.hooks.register_hook("PreToolUse", change)
        session.run_user("Print ok")
        result = json.loads(
            next(m["content"] for m in session.messages if m.get("role") == "tool")
        )
        self.assertFalse(result.get("executed", True))
        self.assertIn("stale", result["error"])

    def test_unrelated_file_change_does_not_invalidate_plain_command(self):
        session, client, _ = self.session(
            [reply(calls=[call("bash", command="printf ok")]), verdict(), reply("Done")])
        def change(event):
            if event.name == "bash":
                (self.root / "unrelated.md").write_text("New documentation")
        session.hooks.register_hook("PreToolUse", change)
        session.run_user("Print ok")
        result = json.loads(next(m["content"] for m in session.messages if m.get("role") == "tool"))
        self.assertNotIn("error", result)
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("ok", result["stdout"])

    def test_selected_execution_evidence_is_still_guarded_after_approval(self):
        import sys, shlex
        script = self.root / 'script.py'
        script.write_text("print('safe')")
        decision = reply(json.dumps({'decision': 'approve', 'reason': 'Reviewed this script.', 'evidence_paths': ['script.py']}))
        session, client, _ = self.session([
            reply(calls=[call('bash', command=shlex.quote(sys.executable)+' '+shlex.quote(str(script)))]),
            reply(calls=[call('read', name='read_review_file', arguments='{"path":"script.py"}')]),
            decision, reply('Done')])
        def change(event):
            if event.name == 'bash':
                script.write_text("open('should-not-exist','w').write('unsafe')")
        session.hooks.register_hook('PreToolUse', change)
        session.run_user('Run the reviewed script')
        result = json.loads(next(m['content'] for m in session.messages if m.get('role') == 'tool'))
        self.assertFalse(result.get('executed', True))
        self.assertIn('stale', result['error'])
        self.assertFalse((self.root / 'should-not-exist').exists())

    def test_discarded_optional_evidence_does_not_block_later_execution(self):
        (self.root / 'guide').write_text('Optional guidance')
        decision = reply(json.dumps({'decision': 'approve', 'reason': 'The guide is irrelevant to printing ok.', 'evidence_paths': []}))
        session, client, _ = self.session([
            reply(calls=[call('bash', command='printf ok')]),
            reply(calls=[call('read', name='read_review_file', arguments='{"path":"guide"}')]),
            decision, reply('Done')])
        def change(event):
            if event.name == 'bash':
                (self.root / 'guide').write_text('Updated optional guidance')
        session.hooks.register_hook('PreToolUse', change)
        session.run_user('Print ok')
        result = json.loads(next(m['content'] for m in session.messages if m.get('role') == 'tool'))
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(result['stdout'], 'ok')

    def test_timeout_leaves_manual_request_without_second_model_call(self):
        reviewer, client, _ = self.reviewer(
            [], {"mode": "auto_review", "timeout_seconds": 0.05}
        )
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def slow(**kwargs):
            started.set()
            release.wait(5)
            return verdict()

        client.chat.completions.create.side_effect = slow
        context = self.context()
        published = []
        began = time.monotonic()
        self.assertIsNone(
            reviewer.review(
                self.request(),
                context,
                lambda: context,
                lambda *args: published.append(args),
            )
        )
        self.assertLess(time.monotonic() - began, 0.5)
        self.assertFalse(published)
        release.set()
        self.assertTrue(reviewer.gateway.wait_idle(5))
        reviewer.review(
            self.request(),
            context,
            lambda: context,
            lambda *args: published.append(args),
        )
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_explicit_requester_fallback_uses_lead(self):
        reviewer, client, _ = self.reviewer(
            [verdict()],
            {
                "mode": "auto_review",
                "model_source": "requester",
                "requester_fallback": "lead",
            },
        )
        context = self.context()
        self.assertEqual(
            reviewer.review(
                self.request(), context, lambda: context, lambda decision, _id: decision
            ),
            "approve",
        )
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_manual_denial_wins_while_review_is_running(self):
        session, client, _ = self.session()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        count = 0

        def model(**kwargs):
            nonlocal count
            count += 1
            if count == 1:
                return reply(calls=[call("bash", command="touch should-not-exist")])
            if count == 2:
                entered.set()
                release.wait(5)
                return verdict()
            return reply("Denied")

        client.chat.completions.create.side_effect = model
        broker = ApprovalBroker(
            trusted=True, owner=session, announce=lambda _text: None
        )
        self.addCleanup(broker.close)
        results = []

        def run():
            with approval_scope(broker):
                results.append(session.run_user("Discuss changes"))

        worker = threading.Thread(target=run)
        worker.start()
        self.assertTrue(entered.wait(3))
        request = broker.records()[0]
        broker.respond(request["id"], False)
        helpers.eventually(lambda: bool(results))
        self.assertFalse((self.root / "should-not-exist").exists())
        self.assertTrue(
            any(
                row["state"] == "superseded"
                for row in session.runtime_store.items("approval_reviews").values()
            )
        )
        release.set()
        worker.join(5)
        self.assertTrue(session.gateway.wait_idle(5))

    def test_resume_requires_same_reviewer_policy(self):
        session, client, _ = self.session()
        sid = session.session_id
        session.close()
        self.assertTrue(session.wait_closed(5))
        changed = HarnessSession(client, "fixture", self.root, resume_session=sid)
        self.addCleanup(lambda: (changed.close(), changed.wait_closed(5)))
        with self.assertRaises(Exception):
            changed._ensure_runtime()

    def test_commit_confirmation_loss_never_republishes_after_restart(self):
        reviewer, client, _ = self.reviewer([verdict()])
        context = self.context()
        published = []

        def publish(decision, identity):
            published.append(decision)
            raise OSError("Commit confirmation lost")

        reviewer.review(self.request(), context, lambda: context, publish)
        self.assertEqual(
            next(iter(reviewer.store.items("approval_reviews").values()))["state"],
            "publication_unknown",
        )
        restarted = ApprovalReviewer(
            reviewer.config,
            reviewer.store,
            reviewer.registry,
            reviewer.gateway,
            reviewer.lead_binding,
        )
        self.addCleanup(restarted.close)
        restarted.review(self.request(), context, lambda: context, publish)
        self.assertEqual(published, ["approve"])
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_changed_required_file_allows_model_to_request_human(self):
        (self.root / "script.py").write_text('print("ok")')
        read = reply(
            calls=[
                call("read", name="read_review_file", arguments='{"path":"script.py"}')
            ]
        )
        reviewer, client, _ = self.reviewer([])
        responses = iter([read, verdict(), verdict("human_required")])
        published = []

        def model(**kwargs):
            response = next(responses)
            if client.chat.completions.create.call_count == 2:
                (self.root / "script.py").write_text("changed")
            return response

        client.chat.completions.create.side_effect = model
        context = self.context()
        reviewer.review(
            self.request(),
            context,
            lambda: context,
            lambda *args: published.append(args),
        )
        self.assertFalse(published)
        self.assertEqual(
            next(iter(reviewer.store.items("approval_reviews").values()))["state"],
            "human_required",
        )

        self.assertEqual(client.chat.completions.create.call_count, 3)
        self.assertEqual(next(iter(reviewer.store.items("approval_reviews").values()))["verdict"]["decision"], "human_required")

    def test_private_evidence_and_oversized_context_fall_back(self):
        reviewer, client, _ = self.reviewer(
            [
                reply(
                    calls=[
                        call(
                            "read",
                            name="read_review_file",
                            arguments='{"path":".harness/state.json"}',
                        )
                    ]
                )
            ]
        )
        context = self.context()
        published = []
        reviewer.review(
            self.request(),
            context,
            lambda: context,
            lambda *args: published.append(args),
        )
        self.assertFalse(published)
        other, client, _ = self.reviewer([verdict()])
        huge = {**self.context(), "intent": "x" * 200000}
        other.review(
            {**self.request(), "id": "large"},
            huge,
            lambda: huge,
            lambda *args: published.append(args),
        )
        self.assertEqual(client.chat.completions.create.call_count, 0)
        self.assertFalse(published)

    def test_subagent_requester_scope_is_the_actual_child_binding(self):
        from homebrew_harness import Capabilities
        from homebrew_harness.security.approval_review import requester_binding
        from homebrew_harness.execution.model_gateway import current_scope

        session, client, _ = self.session(
            [
                reply(
                    calls=[
                        call(
                            "task",
                            name="task",
                            arguments='{"prompt":"Read the project"}',
                        )
                    ]
                ),
                reply("Child result"),
                reply("Done"),
            ],
            capabilities=Capabilities({"subagents"}),
        )
        seen = []
        original = client.chat.completions.create.side_effect

        def model(**kwargs):
            if current_scope().role == "subagent":
                seen.append(requester_binding())
            return original(**kwargs)

        client.chat.completions.create.side_effect = model
        session.run_user("Read the project with a subagent")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].identity(), session.lead_binding.identity())

    def test_sdk_configuration_uses_auto_review_without_human_broker_input(self):
        from homebrew_harness import Harness, HostConfig

        client, _ = fake_client(
            [reply(calls=[call("bash", command="printf ok")]), verdict(), reply("Done")]
        )
        host = Harness(
            workdir=self.root,
            config=HostConfig.from_components(
                client=client,
                model="fixture",
                approval_review={"mode": "auto_review"},
                shutdown_seconds=3,
            ),
        )
        self.addCleanup(host.close)
        session = host.sessions.open()
        turn = session.submit("Print ok")
        helpers.eventually(lambda: turn.snapshot().state == "completed", 5)
        self.assertEqual(turn.result().text, "Done")
        self.assertEqual(client.chat.completions.create.call_count, 3)

    def test_review_records_retire_and_restore_with_session(self):
        from homebrew_harness.storage.checkpoint_retirement import (
            preview,
            retire,
            unretire,
        )

        session, client, _ = self.session(
            [reply(calls=[call("bash", command="printf ok")]), verdict(), reply("Done")]
        )
        session.run_user("Print ok")
        session.close()
        self.assertTrue(session.wait_closed(5))
        store = session.runtime_store
        sid = session.session_id
        before = store.items("approval_reviews")
        self.assertEqual(len(before), 1)
        store.set_meta("latest_session", "newer-fixture")
        now = time.time() + 30 * 86400
        plan = preview(self.root, selected=[sid], now=now)
        retire(self.root, plan, now=now)
        self.assertFalse(store.items("approval_reviews"))
        unretire(self.root, sid, now=now + 1)
        self.assertEqual(store.items("approval_reviews"), before)

    def test_stale_review_does_not_leave_a_test_grant(self):
        import sys
        from homebrew_harness.execution.test_runner import TestRunner, TestRecipe

        runner = TestRunner(
            self.root,
            [
                TestRecipe(
                    "checks",
                    (sys.executable, "-c", "print('ok')"),
                    locked_globs=(),
                    max_runs=1,
                )
            ],
        )
        session, client, _ = self.session(
            [
                reply(
                    calls=[
                        call("test", name="run_tests", arguments='{"name":"checks"}')
                    ]
                ),
                verdict(),
                reply("Stopped"),
            ],
            test_runner=runner,
        )

        def change(event):
            if event.name == "run_tests":
                (self.root / "changed").write_text("after review")

        session.hooks.register_hook("PreToolUse", change)
        session.run_user("Run the checks")
        self.assertEqual(runner.runs, 0)
        self.assertFalse(runner.grants)

    def test_review_uses_actual_task_and_member_amendments(self):
        from homebrew_harness import Capabilities
        from homebrew_harness.execution.test_runner import TestContext
        from homebrew_harness.context.task_intent import TaskIntent
        from homebrew_harness.security.review_context import (
            permission_scope,
            session_context,
        )

        session, client, _ = self.session(capabilities=Capabilities({"tasks"}))
        task = session.workspace.store.create("Actual member task")
        intent = TaskIntent("Keep the public API")
        intent.update("Also retain the file format", new_task=False)
        observed = []
        session.mcp = SimpleNamespace(
            descriptor=lambda name, **kw: (
                observed.append(kw) or {"task_id": kw["task_id"]}
            )
        )
        with permission_scope(TestContext("teammate", task.id), intent):
            context = session_context(session, "mcp__fixture__query", {}, self.root)
        self.assertEqual(observed[0]["task_id"], task.id)
        self.assertEqual(context["role"], "teammate")
        self.assertEqual(context["task_id"], task.id)
        self.assertIn("retain the file format", str(context["requester_intent"]))
        session.mcp = None
