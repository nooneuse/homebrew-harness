import os
import subprocess
import time
from unittest.mock import patch

from tests.runs import test_human_control as support
from tests.support.run_services import RunServiceFixture
from homebrew_harness.runs import RunSpec
from homebrew_harness.runs.services import RunServices
from homebrew_harness.platforms.github.feedback import stamp as iso_time


class SourceTests(support.ManagedRunFixture):
    def setup_source(self):
        (self.root / "input.txt").write_text("fixture")
        for command in (
            ("init", "-q"),
            ("add", "input.txt"),
            ("commit", "-qm", "fixture"),
        ):
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
                    *command,
                ],
                cwd=self.root,
                check=True,
                capture_output=True,
            )
        self.head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.root, text=True
        ).strip()
        server = RunServiceFixture(self.head)
        self.addCleanup(server.close)
        env = patch.dict(
            os.environ,
            {
                "HARNESS_GH_API": server.url,
                "HARNESS_GH_TOKEN": "fixture-source-credential",
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.config = {
            "version": 1,
            "change_request": {
                "provider": "github",
                "instance": "primary",
                "project": "fixture/project",
                "number": 1,
                "actor_ids": [7],
                "app_ids": [11],
                "api_url_env": "HARNESS_GH_API",
                "token_env": "HARNESS_GH_TOKEN",
                "poll_seconds": 0.1,
            },
        }
        services = RunServices(self.store, self.config)
        self.addCleanup(services.close)
        run = self.store.create(RunSpec("Handle current pull feedback"))
        services.bind(run["id"])
        self.run_id, self.server, self.services = run["id"], server, services
        return server, services

    def comment(self, identity, actor=7, stamp=None):
        return {
            "id": identity,
            "user": {"id": actor},
            "body": "Review existing requirements",
            "updated_at": stamp or iso_time(time.time() + 1),
        }

    def due(self):
        for key, value in self.store.runtime.items("run_sources").items():
            self.store.runtime.put("run_sources", key, {**value, "next_poll_at": 0})

    def test_real_http_source_admits_current_actor_and_head_then_deduplicates(self):
        server, services = self.setup_source()
        server.comments = [
            self.comment(1),
            self.comment(2, actor=99),
            self.comment(3, actor=True),
        ]
        server.checks = [
            {
                "id": 4,
                "app": {"id": 11},
                "head_sha": "0" * 40,
                "status": "completed",
                "conclusion": "failure",
                "output": {"summary": "old commit"},
            },
            {
                "id": 5,
                "app": {"id": 11},
                "head_sha": self.head,
                "status": "completed",
                "conclusion": "success",
                "output": {"summary": "current commit"},
            },
        ]
        services.tick(self.run_id)
        events = self.store.runtime.items("run_events")
        self.assertEqual(len(events), 5)
        self.assertEqual(sum(e["state"] == "queued" for e in events.values()), 2)
        self.assertEqual(
            len(self.store.runtime.mailbox("inspect", "reference_" + self.run_id)), 2
        )
        self.due()
        services.tick(self.run_id)
        self.assertEqual(len(self.store.runtime.items("run_events")), 5)
        self.assertEqual(
            len(self.store.runtime.mailbox("inspect", "reference_" + self.run_id)), 2
        )
        self.assertTrue(
            all(
                r["headers"]["Authorization"] == "Bearer fixture-source-credential"
                for r in server.requests
            )
        )

    def test_same_second_new_comment_is_not_lost_at_timestamp_cursor(self):
        server, services = self.setup_source()
        stamp = iso_time(time.time() + 1)
        server.comments = [self.comment(1, stamp=stamp)]
        services.tick(self.run_id)
        server.comments.append(self.comment(2, stamp=stamp))
        self.due()
        services.tick(self.run_id)
        self.assertEqual(len(self.store.runtime.items("run_events")), 2)

    def test_page_failure_does_not_advance_cursor_or_partially_deliver(self):
        server, services = self.setup_source()
        server.pages = {1: [self.comment(1)], 2: [self.comment(2)]}
        server.fail_page = ("comments", 2)
        services.tick(self.run_id)
        source = next(iter(self.store.runtime.items("run_sources").values()))
        self.assertEqual(source["cursor"], {})
        self.assertFalse(self.store.runtime.items("run_events"))
        self.assertFalse(
            self.store.runtime.mailbox("inspect", "reference_" + self.run_id)
        )
        server.fail_page = None
        self.due()
        services.tick(self.run_id)
        self.assertEqual(len(self.store.runtime.items("run_events")), 2)

    def test_pause_accepts_reference_without_starting_business(self):
        server, services = self.setup_source()
        self.store.control(self.run_id, "pause")
        server.comments = [self.comment(1)]
        services.tick(self.run_id)
        run = self.store.get(self.run_id)
        self.assertEqual(run["status"], "paused")
        self.assertEqual(run["generation"], 0)
        self.assertEqual(
            len(self.store.runtime.mailbox("inspect", "reference_" + self.run_id)), 1
        )

    def test_source_credential_can_rotate_without_changing_subscription_scope(self):
        _, services = self.setup_source()
        with patch.dict(os.environ, {"HARNESS_GH_TOKEN": "rotated-fixture-credential"}):
            replacement = RunServices(self.store, self.config)
            self.addCleanup(replacement.close)
        self.assertEqual(replacement.identity, services.identity)
        replacement.bind(self.run_id)

    def test_model_session_change_does_not_change_source_event_identity(self):
        import uuid

        server, services = self.setup_source()
        server.comments = [self.comment(1)]
        services.tick(self.run_id)
        first = set(self.store.runtime.items("run_events"))
        self.assertEqual(len(first), 1)
        current = self.store.get(self.run_id)
        current["session_id"] = uuid.uuid4().hex
        self.store.runtime.put("runs", self.run_id, current)
        self.due()
        services.tick(self.run_id)
        self.assertEqual(set(self.store.runtime.items("run_events")), first)
        self.assertEqual(
            len(self.store.runtime.mailbox("inspect", "reference_" + self.run_id)), 1
        )

    def test_empty_review_and_in_progress_check_are_normalized(self):
        server, services = self.setup_source()
        server.reviews = [
            {
                "id": 20,
                "user": {"id": 7},
                "commit_id": self.head,
                "state": "APPROVED",
                "body": None,
                "submitted_at": iso_time(time.time()),
            }
        ]
        server.checks = [
            {
                "id": 21,
                "app": {"id": 11},
                "head_sha": self.head,
                "status": "in_progress",
                "conclusion": None,
                "completed_at": None,
                "started_at": iso_time(time.time()),
                "output": {},
            }
        ]
        services.tick(self.run_id)
        events = list(self.store.runtime.items("run_events").values())
        self.assertEqual(len(events), 2)
        self.assertTrue(all(e["state"] == "queued" for e in events))
        self.assertEqual(next(e for e in events if e["kind"] == "review")["body"], "")
        self.assertTrue(all(e["subject"]["kind"] == "change_request" for e in events))
