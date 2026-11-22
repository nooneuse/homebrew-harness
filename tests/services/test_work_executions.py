"""Final candidate checks expose the same observation and stop controls."""

import json
import unittest
from unittest.mock import patch
from tests.services.delivery import test_flow as fixture
from tests.runtime.test_interaction import eventually
from homebrew_harness.services.agent import ServiceAgent


class WorkExecutionTests(unittest.TestCase):
    setUp = fixture.FlowTests.setUp
    run_git = fixture.FlowTests.run_git
    start = fixture.FlowTests.start

    def test_lead_can_observe_and_stop_actual_final_candidate_test(self):
        path = self.workspace / "tests/test_value.py"
        path.write_text(
            path.read_text()
            .replace("import unittest", "import unittest, time")
            .replace(
                "self.assertGreaterEqual",
                'print("TEST_STARTED",flush=True)\n        time.sleep(5)\n        self.assertGreaterEqual',
            )
        )
        self.run_git("add", "tests", cwd=self.workspace)
        self.run_git(
            "commit", "-qm", "Long registered verification", cwd=self.workspace
        )
        self.run_git("push", str(self.remote), "main", cwd=self.workspace)
        original = ServiceAgent.propose

        def propose(agent, proposal, **kwargs):
            if agent.job["kind"] == "ci_triage":
                proposal = json.dumps(
                    {
                        "classification": "infrastructure",
                        "next_action": "defer",
                        "reason": "The test was intentionally stopped for inspection.",
                        "plan": "Keep the cancellation receipt.",
                    }
                )
            return original(agent, proposal, **kwargs)

        with patch.object(ServiceAgent, "propose", propose):
            service = self.start(merge={"mode": "manual", "poll_seconds": 0.1})
            work = service.work.submit("Implement the registered fixture value.")
            row = eventually(
                lambda: next(
                    (
                        v
                        for v in work.executions()["executions"]
                        if v["details"].get("name") == "candidate_validation"
                        and v["execution_pending"]
                    ),
                    None,
                ),
                30,
            )
            identity = row["ref"]["id"]
            output = eventually(
                lambda: (
                    work.execution_output(identity)
                    if "TEST_STARTED" in work.execution_output(identity)["content"]
                    else None
                ),
                15,
            )
            self.assertIn("TEST_STARTED", output["content"])
            waited = work.wait_execution(identity, 0.01)
            self.assertTrue(waited["execution_pending"])
            work.stop_execution(identity, request_id="stop-one-test")
            final = eventually(
                lambda: (
                    work.execution(identity)
                    if not work.execution(identity)["execution_pending"]
                    else None
                ),
                20,
            )
            self.assertEqual(final["state"], "cancelled")
            self.assertTrue(final["cleanup"]["complete"])
            self.assertFalse(self.peer.pulls)

    def test_work_execution_reference_cannot_be_read_from_another_work(self):
        service = self.start()
        service.pause("all")
        first = service.work.submit("First scope")
        second = service.work.submit("Other scope")
        result = service.runtime.work_executions.invoke(
            first.id,
            "fixture",
            {},
            lambda: {"ok": True},
            execution={"mode": "interactive", "wait_seconds": 0},
        )
        eventually(
            lambda: not first.execution(result["execution_id"])["execution_pending"], 10
        )
        with self.assertRaises(Exception):
            second.execution(result["execution_id"])

    def test_slow_refresh_has_http_handle_and_wait_does_not_block_stop(self):
        import threading
        from homebrew_harness import RemoteService
        from homebrew_harness.execution.execution_control import check
        from homebrew_harness.execution.operations import emit_output

        service = self.start()
        service.pause("all")
        entered = threading.Event()
        stopped = threading.Event()

        def refresh(*args):
            emit_output("output", "REFRESH_STARTED\n")
            entered.set()
            while not stopped.wait(0.02):
                check()
            return {"id": "fixture"}

        self.addCleanup(stopped.set)
        with patch(
            "homebrew_harness.services.delivery.merge_flow.refresh", refresh
        ), patch(
            "homebrew_harness.services.delivery.controls.status",
            return_value={"id": "fixture", "state": "awaiting_merge"},
        ), RemoteService.from_root(
            self.control, control=True
        ) as remote:
            result = remote.refresh_delivery(
                "fixture",
                request_id="refresh-once",
                execution={"mode": "interactive", "wait_seconds": 0},
            )
            identity = result["execution_id"]
            self.assertTrue(entered.wait(5))
            self.assertIn(
                "REFRESH_STARTED", remote.execution_output(identity)["content"]
            )
            wait_started = threading.Event()
            wait_finished = threading.Event()

            def wait():
                wait_started.set()
                remote.wait_execution(identity, 8)
                wait_finished.set()

            waiter = threading.Thread(target=wait)
            waiter.start()
            self.addCleanup(waiter.join, 10)
            self.assertTrue(wait_started.wait(3))
            remote.snapshot()
            remote.stop_execution(identity, request_id="stop-refresh")
            self.assertTrue(wait_finished.wait(4))
            self.assertEqual(remote.execution(identity)["state"], "cancelled")
            duplicate = remote.refresh_delivery(
                "fixture",
                request_id="refresh-once",
                execution={"mode": "interactive", "wait_seconds": 0},
            )
            self.assertEqual(duplicate["execution_id"], identity)
            with self.assertRaises(Exception):
                remote.work.submit("Other source").execution(identity)
        stopped.set()

    def test_explicit_one_shot_refresh_returns_legacy_result(self):
        service = self.start()
        service.pause("all")
        with patch(
            "homebrew_harness.services.delivery.merge_flow.refresh",
            return_value={"id": "fixture"},
        ), patch(
            "homebrew_harness.services.delivery.controls.status",
            return_value={"id": "fixture", "state": "awaiting_merge"},
        ):
            self.assertEqual(
                service.refresh_delivery(
                    "fixture", request_id="one-shot", execution={"mode": "one_shot"}
                ),
                {"id": "fixture", "state": "awaiting_merge"},
            )

    def test_closed_service_execution_registry_keeps_views_without_new_attempts(self):
        service = self.start()
        service.pause("all")
        work = service.work.submit("Retained source")
        registry = service.runtime.work_executions
        registry.request_close()
        registry.manager(work.id)
        self.assertTrue(registry.manager(work.id).closed)
        with self.assertRaisesRegex(ValueError, "executions are closing"):
            registry.invoke(
                work.id,
                "must-not-run",
                {},
                lambda: self.fail("Cannot start after close"),
            )
        self.assertTrue(registry.wait_closed(1))

    def test_execution_views_remain_available_during_concurrent_admission(self):
        import threading

        service = self.start()
        service.pause("all")
        registry = service.runtime.work_executions
        errors = []
        done = threading.Event()

        def admit():
            try:
                for number in range(12):
                    work = service.work.submit("Observation scope " + str(number))
                    registry.invoke(
                        work.id,
                        "local-observation",
                        {},
                        lambda: {"ok": True},
                        request_id="one",
                        execution={"mode": "interactive", "wait_seconds": 0},
                    )
            except Exception as error:
                errors.append(error)
            finally:
                done.set()

        writer = threading.Thread(target=admit)
        writer.start()
        self.addCleanup(writer.join, 10)
        while not done.wait(0.001):
            registry.active("candidate_validation")
            registry.manager_snapshot()
        self.assertEqual(errors, [])
