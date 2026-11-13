import unittest
from unittest.mock import patch
from tests.services import test_maintenance as fixture
from tests.services.p2_fixture import issue
from tests.runtime.test_interaction import eventually
from homebrew_harness.services.sources import RepositorySource
from homebrew_harness.execution.model_gateway import Deadline


class MaintenanceBoundaries(unittest.TestCase):
    setUp = fixture.MaintenanceTests.setUp
    start = fixture.MaintenanceTests.start

    def test_source_page_commit_rolls_back_work_event_and_cursor_together(self):
        service = self.start()
        service.pause("all")
        source = RepositorySource(
            {
                "name": "project",
                "provider": "github",
                "instance": "primary",
                "project": "example/project",
                "api_url_env": "E",
                "token_env": "T",
                "poll_seconds": 10,
            },
            {"E": self.peer.url, "T": "synthetic"},
        )
        self.addCleanup(source.close)
        self.peer.issues = [issue()]
        page = source.poll({}, Deadline.after(5))
        store = service.runtime.maintenance.store
        original = store.db._put

        def fail(conn, section, key, value):
            if section == "service_events":
                raise OSError("simulated disk failure")
            return original(conn, section, key, value)

        with patch.object(store.db, "_put", fail), self.assertRaises(OSError):
            store.source_page("project", page, source.scope)
        self.assertEqual(service.work.list()["items"], [])
        self.assertEqual(service.records("events")["items"], [])
        self.assertEqual(service.records("sources")["items"], [])
        store.source_page("project", page, source.scope)
        self.assertEqual(len(service.work.list()["items"]), 1)

    def test_page_cursor_retains_unread_objects_and_comment_identity(self):
        source = RepositorySource(
            {
                "name": "project",
                "provider": "github",
                "instance": "primary",
                "project": "example/project",
                "api_url_env": "E",
                "token_env": "T",
                "poll_seconds": 10,
            },
            {"E": self.peer.url, "T": "synthetic"},
        )
        self.addCleanup(source.close)
        self.peer.issues = [issue(n) for n in range(1, 56)]
        page = source.poll({}, Deadline.after(5))
        first, cursor = page.events, page.cursor
        self.assertEqual(len(first), 50)
        self.assertEqual(cursor["position"]["page"], 2)
        page = source.poll(cursor, Deadline.after(5))
        second, cursor = page.events, page.cursor
        self.assertEqual(len(second), 5)
        self.assertEqual(cursor["position"]["stream"], "comments")
        self.peer.comments = [
            {
                "id": 91,
                "issue_url": "https://example.invalid/repos/example/project/issues/1",
                "body": "Clarification",
                "updated_at": "2026-01-02T00:00:00Z",
                "user": {"id": 11},
            }
        ]
        page = source.poll(cursor, Deadline.after(5))
        comments, cursor = page.events, page.cursor
        self.assertEqual(comments[0].subject.number, 1)
        self.assertEqual(comments[0].object.kind, "comment")
        self.assertIn("sweep", cursor["position"])
        for _ in range(3):
            cursor = source.poll(cursor, Deadline.after(5)).cursor
        self.assertNotIn("sweep", cursor["position"])

    def test_information_answer_amends_work_without_granting_permission(self):
        self.peer.triage.update(
            disposition="needs_info", plan="Which behavior should be preserved?"
        )
        service = self.start(auto=False)
        work = service.work.submit("Clarify the requested behavior.")
        request = eventually(lambda: service.requests() and service.requests()[0], 15)
        self.assertEqual(request.kind, "work_information")
        with self.assertRaises(Exception):
            self.host.requests.approve(request.ref, decision_id="wrong")
        self.peer.triage.update(
            disposition="decide",
            plan="Preserve current behavior and add regression coverage.",
        )
        self.host.requests.answer(
            request.ref, text="Preserve the existing public API.", decision_id="answer"
        )
        current = eventually(
            lambda: next(
                (r for r in service.requests() if r.kind == "work_authority"), None
            ),
            15,
        )
        self.assertIn(
            "Preserve the existing public API.", service.store.work(work.id)["prompt"]
        )
        self.assertIsNone(service.store.work(work.id)["authority"])
        self.assertEqual(current.arguments["work_id"], work.id)

    def test_unread_reports_are_per_reader_and_acknowledgment_is_monotonic(self):
        service = self.start()
        service.pause("all")
        store = service.runtime.maintenance.store
        with store.db.write() as conn:
            store.changed(conn, detail={"event": "one"})
            one = store.report(conn, force=True)
        store.finish(store.claim_job(), {"text": "One"})
        with store.db.write() as conn:
            store.changed(conn, detail={"event": "two"})
            two = store.report(conn, force=True)
        store.finish(store.claim_job(), {"text": "Two"})
        service.acknowledge_report(two["id"], context="first")
        service.acknowledge_report(one["id"], context="first")
        self.assertEqual(service.reports(context="first")["items"], [])
        self.assertEqual(len(service.reports(context="second")["items"]), 2)

    def test_failed_report_can_be_explicitly_retried_without_erasing_attempts(self):
        service = self.start(job_attempts=1)
        service.pause("all")
        store = service.runtime.maintenance.store
        with store.db.write() as conn:
            store.changed(conn, detail={"event": "one"})
            receipt = store.report(conn, force=True)
        job = store.claim_job()
        store.finish(job, error=ValueError("Invalid output"))
        self.assertEqual(service.report(receipt["id"])["state"], "failed")
        first = service.retry_job(job["id"], request_id="retry")
        self.assertEqual(first, service.retry_job(job["id"], request_id="retry"))
        retry = store.claim_job()
        self.assertEqual(retry["attempts"], 2)
        store.finish(retry, {"text": "Recovered report."})
        self.assertEqual(service.report(receipt["id"])["state"], "ready")

    def test_service_keeps_real_ownership_until_slow_reasoning_returns(self):
        import threading
        from homebrew_harness.services.launcher import live

        service = self.start()
        entered, release = threading.Event(), threading.Event()

        def hold(job):
            entered.set()
            release.wait(10)
            return {"text": "Late result."}

        with patch.object(service.runtime.maintenance.agent, "run", hold):
            receipt = service.converse("Discuss future work.")
            self.assertTrue(entered.wait(5))
            service.stop()
            self.assertFalse(service.wait_closed(0.1))
            self.assertTrue(live(self.control))
            release.set()
            self.assertTrue(service.wait_closed(10))
        self.assertFalse(
            service.store.runtime.get("service_messages", receipt["id"]).get("result")
        )

    def test_expired_work_request_can_be_refreshed_without_rerunning_triage(self):
        import time

        service = self.start(auto=False)
        work = service.work.submit("Plan the bounded feature.")
        request = eventually(lambda: service.requests() and service.requests()[0], 15)
        store = service.runtime.maintenance.store
        with store.db.write() as conn:
            record = store.get("decisions", request.ref.id, conn)
            record["expires_at"] = time.time() - 1
            store.put(conn, "decisions", record)
        self.assertEqual(service.requests()[0].state, "expired")
        refreshed = work.request_authority(request_id="renew-request")
        self.assertGreater(refreshed["revision"], request.ref.revision)
        current = service.requests()[0]
        self.assertEqual(current.state, "pending")
        self.assertIsNone(service.store.work(work.id)["authority"])
        self.assertEqual(len(self.peer.model_requests), 1)

    def test_service_code_discovery_excludes_runtime_and_credentials(self):
        import json

        service = self.start()
        actor = service.runtime.maintenance.agent
        (self.workspace / "module.py").write_text("unique_project_marker")
        (self.workspace / ".env").write_text("unique_project_marker")
        runtime = self.workspace / ".harness"
        runtime.mkdir(exist_ok=True)
        (runtime / "secret.txt").write_text("unique_project_marker")
        result = json.loads(actor.search("unique_project_marker"))
        self.assertEqual([m["path"] for m in result["matches"]], ["module.py"])
        self.assertNotIn("secret.txt", actor.glob("**/*"))

    def test_conversation_preserves_unmatched_quotes_and_whitespace(self):
        import io
        from contextlib import redirect_stdout
        from homebrew_harness.services.cli import attach

        service = self.start()
        raw = "  What's the plan for this project?  "
        lines = iter([raw, "exit"])
        with redirect_stdout(io.StringIO()):
            attach(service, input_fn=lambda _: next(lines))
        message = service.records("messages")["items"][0]
        self.assertEqual(service.conversation(message["id"])["text"], raw)

    def test_structured_goal_survives_work_authorization(self):
        service = self.start()
        service.pause("execution")
        goal = {
            "condition": "The requested marker exists",
            "judge": False,
            "predicates": [
                {"id": "marker", "type": "file_exists", "path": "src/proof.txt"}
            ],
        }
        work = service.work.submit("Create the requested marker.", goal=goal)
        value = eventually(
            lambda: (
                service.store.work(work.id)
                if service.store.work(work.id).get("authority")
                else None
            ),
            15,
        )
        self.assertEqual(
            value["run_spec"]["goal"]["predicates"][0]["path"], "src/proof.txt"
        )
        self.assertEqual(value["run_spec"]["goal"]["condition"], goal["condition"])
        self.assertIsNone(value["run_id"])

    def test_background_request_lookup_does_not_refresh_expired_authority(self):
        import time

        service = self.start(auto=False)
        work = service.work.submit("Consider the existing plan.")
        request = eventually(lambda: service.requests() and service.requests()[0], 15)
        store = service.runtime.maintenance.store
        with store.db.write() as conn:
            record = store.get("decisions", request.ref.id, conn)
            record["expires_at"] = time.time() - 10
            store.put(conn, "decisions", record)
            unchanged = store.work_request(
                conn, service.store.work(work.id), record["candidate"]
            )
        self.assertEqual(unchanged["revision"], request.ref.revision)
        self.assertLess(unchanged["expires_at"], time.time())
        self.assertEqual(service.requests()[0].state, "expired")

    def test_cleanup_failure_retains_owner_and_can_be_retried(self):
        from homebrew_harness.services.launcher import live

        service = self.start()
        actor = service.runtime.maintenance.agent
        with patch.object(
            actor, "close", side_effect=OSError("Simulated client closure failure")
        ):
            service.stop()
            eventually(lambda: service.runtime.shutdown.error, 5)
            self.assertFalse(service.wait_closed(0))
            self.assertTrue(live(self.control))
        service.close()
        self.assertTrue(service.wait_closed(10))
        self.assertFalse(live(self.control))

    def test_old_run_completion_does_not_complete_updated_requirements(self):
        import threading

        self.peer.native_hold = threading.Event()
        self.peer.issues = [issue()]
        service = self.start(sources=True)
        identity = eventually(
            lambda: (
                service.work.list()["items"] and service.work.list()["items"][0]["id"]
            ),
            10,
        )
        work = service.work.get(identity)
        native_id = eventually(lambda: service.store.work(identity)["run_id"], 15)
        with patch.object(service.runtime, "tick", return_value=None):
            self.peer.issues = [
                issue(
                    body="Also retain the new API behavior.",
                    updated="2026-01-02T00:00:00Z",
                )
            ]
            eventually(lambda: work.snapshot().input_revision == 1, 5)
            self.peer.native_hold.set()
            eventually(
                lambda: service.runtime.child.run_store.get(native_id).get(
                    "finalized_at"
                ),
                15,
            )
        eventually(lambda: work.snapshot().state == "proposed", 15)
        snapshot = work.snapshot()
        self.assertFalse(snapshot.output_available)
        self.assertEqual(snapshot.details["verification"]["requirements_revision"], 0)
        self.assertEqual(snapshot.input_revision, 1)
        with self.assertRaises(Exception):
            work.result()

    def test_old_triage_cannot_request_authority_for_a_new_revision(self):
        service = self.start(auto=False)
        work = service.work.submit("Assess the first requirement.")
        eventually(lambda: service.requests(), 15)
        service.pause("all")
        work.revise("Add the new acceptance requirement.")
        with self.assertRaises(Exception):
            work.request_authority()
        self.assertIsNone(service.store.work(work.id)["authority"])

    def test_reconciliation_finds_objects_moved_across_pages(self):
        source = RepositorySource(
            {
                "name": "project",
                "provider": "github",
                "instance": "primary",
                "project": "example/project",
                "api_url_env": "E",
                "token_env": "T",
                "poll_seconds": 10,
            },
            {"E": self.peer.url, "T": "synthetic"},
        )
        self.addCleanup(source.close)
        self.peer.issues = [issue(n) for n in range(1, 56)]
        page = source.poll({}, Deadline.after(5))
        events, cursor = page.events, page.cursor
        seen = {int(e.object.id) for e in events}
        moved = self.peer.issues.pop(0)
        moved["updated_at"] = "2026-01-03T00:00:00Z"
        self.peer.issues.append(moved)
        for _ in range(12):
            page = source.poll(cursor, Deadline.after(5))
            events, cursor = page.events, page.cursor
            seen.update(int(e.object.id) for e in events)
            if not cursor["position"].get("sweep"):
                break
        self.assertEqual(seen, set(range(1, 56)))
        self.assertNotIn("sweep", cursor["position"])
        self.assertFalse(cursor["position"].get("rescan_required"))

    def test_unstable_sweep_keeps_the_old_watermark(self):
        source = RepositorySource(
            {
                "name": "project",
                "provider": "github",
                "instance": "primary",
                "project": "example/project",
                "api_url_env": "E",
                "token_env": "T",
                "poll_seconds": 10,
            },
            {"E": self.peer.url, "T": "synthetic"},
        )
        self.addCleanup(source.close)
        cursor = {"binding": source.identity, "position": {"watermark": 1}}
        for n in range(4):
            self.peer.issues = [
                issue(
                    body="Version " + str(n),
                    updated="2026-01-0" + str(n + 1) + "T00:00:00Z",
                )
            ]
            cursor = source.poll(cursor, Deadline.after(5)).cursor
            cursor = source.poll(cursor, Deadline.after(5)).cursor
        self.assertTrue(cursor["position"]["rescan_required"])
        self.assertEqual(cursor["position"]["watermark"], 1)

    def test_exhausted_recovery_marks_the_conversation_failed(self):
        service = self.start(job_attempts=1)
        service.pause("all")
        accepted = service.converse("Discuss the future work.")
        store = service.runtime.maintenance.store
        job = store.claim_job()
        with store.db.write() as conn:
            job["generation"] -= 1
            store.db._put(conn, "service_jobs_model", job["id"], job)
        self.assertIsNone(store.claim_job())
        self.assertEqual(service.conversation(accepted["id"])["state"], "failed")
        self.assertEqual(service.job(job["id"])["error"], "attempts_exhausted")

    def test_text_ui_focus_does_not_silently_follow_new_requirements(self):
        import io
        from contextlib import redirect_stdout
        from homebrew_harness.services.cli import attach

        service = self.start(auto=False)
        service.pause("all")
        work = service.work.submit("Original requirement.")
        service.focus(work.id)
        work.revise("The requirement changed.")
        lines = iter(["Discuss this work.", "exit"])
        output = io.StringIO()
        with redirect_stdout(output):
            attach(service, input_fn=lambda _: next(lines))
        self.assertEqual(service.records("messages")["items"], [])
        self.assertIn("requirements changed", output.getvalue())

    def test_operator_steering_retains_scope_and_expiry_on_the_same_run(self):
        import threading

        self.peer.native_hold = threading.Event()
        service = self.start()
        work = service.work.submit("Perform the scoped local work.")
        native_id = eventually(lambda: service.store.work(work.id)["run_id"], 15)
        self.assertTrue(self.peer.native_started.wait(15))
        original = service.store.work(work.id)["authority"]
        work.steer("Also retain the existing API behavior.", request_id="amendment")
        updated = service.store.work(work.id)["authority"]
        self.assertEqual(updated["scope"], original["scope"])
        self.assertEqual(updated["expires_at"], original["expires_at"])
        self.assertEqual(updated["requirements_revision"], 1)
        self.assertTrue(
            service.runtime.maintenance.store.valid_authority(
                service.store.work(work.id)
            )
        )
        self.peer.native_hold.set()
        eventually(lambda: work.snapshot().state == "review", 20)
        self.assertEqual(service.store.work(work.id)["run_id"], native_id)
        self.assertEqual(
            work.input(amendment=0)["text"], "Also retain the existing API behavior."
        )
