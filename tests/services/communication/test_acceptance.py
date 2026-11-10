"""Shared controls, publication identity, maintenance and external PR execution."""

import json
import os
import sys
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from homebrew_harness import RemoteService
from homebrew_harness.platforms.contracts import ObjectRef, SourceEvent
from homebrew_harness.platforms.registry import PROVIDERS
from homebrew_harness.services.cli import attach
from tests.runtime.test_interaction import eventually
from tests.services.communication import test_boundaries as boundary_fixture


CONTAINER_CLI = os.environ.get("HARNESS_TEST_CONTAINER_CLI")
CONTAINER_IMAGE = os.environ.get("HARNESS_TEST_CONTAINER_IMAGE")


class CommunicationAcceptance(unittest.TestCase):
    setUp = boundary_fixture.CommunicationBoundaries.setUp
    start = boundary_fixture.CommunicationBoundaries.start
    discussion = boundary_fixture.CommunicationBoundaries.discussion
    managed = boundary_fixture.CommunicationBoundaries.managed
    source = boundary_fixture.CommunicationBoundaries.source

    def test_text_and_remote_controls_share_revision_and_replay(self):
        service, did, _ = self.managed()
        remote = RemoteService.from_root(self.control, control=True)
        self.addCleanup(remote.close)
        self.assertEqual(remote.discussion(did), service.discussion(did))
        inputs = iter(["discussion show " + did, "discussion pause " + did, "exit"])
        attach(remote, input_fn=lambda _prompt: next(inputs))
        self.assertEqual(service.discussion(did)["state"], "paused")
        revision = service.discussion(did)["revision"]
        operation = remote.control_discussion(
            did, "reopen", revision=revision, request_id="reopen"
        )
        self.assertEqual(
            remote.control_discussion(
                did, "reopen", revision=revision, request_id="reopen"
            ),
            operation,
        )

    def test_second_provider_uses_same_discussion_and_action_contract(self):
        with patch.dict(PROVIDERS, {"fixture": dict(PROVIDERS["github"])}):
            self.peer.issue()
            self.peer.proposals = [
                {
                    "next_step": "clarify",
                    "reason": "Missing compatibility.",
                    "alignment": "Preserve compatibility.",
                    "communication": {
                        "kind": "post_comment",
                        "intent": "clarify",
                        "body": "Which format?",
                    },
                }
            ]
            service = self.start(auto=True, provider="fixture")
            did = self.discussion(service)
            action = eventually(
                lambda: next(
                    (
                        a
                        for a in service.communication_actions()["items"]
                        if a["state"] == "confirmed"
                    ),
                    None,
                ),
                15,
            )
            self.assertEqual(service.discussion(did)["subject"]["provider"], "fixture")
            self.assertEqual(
                service.communication(action["id"])["result"]["object"]["provider"],
                "fixture",
            )
            service.stop()
            self.assertTrue(service.wait_closed(8))

    def test_confirmed_body_copy_from_same_actor_is_new_input(self):
        service, did, communication = self.managed()
        store = communication.store
        with store.db.write() as conn:
            row = store.get("discussions", did, conn)
            action = store.plan(
                conn,
                row,
                {"kind": "post_comment", "intent": "clarify", "body": "Question?"},
            )
            action.update(
                state="confirmed",
                result={
                    "confirmed": True,
                    "subject": row["subject"],
                    "actor_id": 7,
                    "object": ObjectRef(
                        "github", "primary", "example/project", "comment", "201"
                    ).data(),
                },
            )
            store.put(conn, "communication_actions", action)
        before = store.get("discussions", did)["input_revision"]
        body = "Question?\n\n<!-- harness-communication:" + action["id"] + " -->"
        self.source(store, row["subject"], 202, body, actor=7)
        self.assertEqual(store.get("discussions", did)["input_revision"], before + 1)

    def test_provisional_echo_is_reclassified_after_receipt(self):
        service, did, communication = self.managed()
        store = communication.store
        with store.db.write() as conn:
            row = store.get("discussions", did, conn)
            action = store.plan(
                conn,
                row,
                {"kind": "post_comment", "intent": "clarify", "body": "Question?"},
            )
            action.update(
                state="started",
                attempts=1,
                generation=service.runtime.header["generation"],
            )
            store.put(conn, "communication_actions", action)
        before = row["input_revision"]
        body = "Question?\n\n<!-- harness-communication:" + action["id"] + " -->"
        self.source(store, row["subject"], 203, body, actor=7)
        self.assertEqual(store.get("discussions", did)["input_revision"], before)
        store.finish_action(
            action,
            "confirmed",
            {
                "confirmed": True,
                "subject": row["subject"],
                "actor_id": 7,
                "object": ObjectRef(
                    "github", "primary", "example/project", "comment", "204"
                ).data(),
            },
        )
        after = store.get("discussions", did)
        self.assertEqual(after["input_revision"], before + 1)
        self.assertEqual(after["state"], "ready_to_assess")
        self.assertEqual(
            store.get("communication_actions", action["id"])["applicability"],
            "published_basis_stale",
        )

    def test_delivery_subject_and_comments_link_original_work(self):
        service, did, communication = self.managed()
        store = communication.store
        subject = ObjectRef(
            "github", "primary", "example/project", "change_request", "2", 2
        )
        body = "Candidate description\n\n<!-- harness-delivery:delivery_fixture -->"
        action = {
            "id": "delivery_fixture",
            "kind": "ensure_change_request",
            "state": "confirmed",
            "work_id": "work_fixture",
            "plan": {"body": "Candidate description"},
            "result": {
                "confirmed": True,
                "subject": subject.data(),
                "object": subject.data(),
                "actor_id": 7,
            },
        }
        with store.db.write() as conn:
            store.ms.put(conn, "actions", action)
        event = SourceEvent(
            subject,
            subject,
            "v1",
            {"title": "Candidate", "body": body, "state": "open", "actor_id": 7},
        )
        store.ingest("project", [event], subject.scope())
        row = next(r for r in store.page("discussions")["items"] if r["id"] != did)
        self.assertEqual(row["work_id"], "work_fixture")
        self.assertEqual(row["input_revision"], 0)
        with store.db.connection() as conn:
            saved = store.db._get(conn, "service_events", store.event_identity(event))
        self.assertEqual(saved["echo"], "echo")

    def test_backup_and_retention_preserve_pending_reviews_and_receipts(self):
        folder = self.root / "backups"
        folder.mkdir(mode=0o700)
        self.peer.issue(pr=True)
        self.peer.proposals = [
            {
                "next_step": "wait",
                "reason": "Review completed.",
                "alignment": "Within scope.",
                "communication": {
                    "kind": "submit_review",
                    "intent": "review_feedback",
                    "event": "COMMENT",
                    "body": "Please explain the behavior.",
                },
            }
        ]
        service = self.start(
            operations={"mode": "local", "backup_directory": str(folder)}
        )
        did = self.discussion(service)
        eventually(lambda: service.communication_actions()["items"], 20)

        def wait_operation(row):
            return eventually(
                lambda: (
                    service.operation(row["id"])
                    if service.operation(row["id"])["state"] in {"completed", "blocked"}
                    else None
                ),
                20,
            )

        self.assertEqual(wait_operation(service.quiesce())["state"], "completed")
        review_id = service.discussion(did)["review_id"]
        review = service.runtime.maintenance.communication.store.get(
            "review_snapshots", review_id
        )
        backup = wait_operation(service.backup())
        self.assertEqual(backup["state"], "completed", backup)
        manifest = json.loads((folder / backup["id"] / "manifest.json").read_text())
        self.assertTrue(
            any(
                "communication_reviews/" + review_id + "/head/src/value.py" in p
                for p in manifest["files"]
            )
        )
        self.assertEqual(
            wait_operation(service.retention_preview())["state"], "completed"
        )
        restored = wait_operation(service.restore_backup(backup["id"]))
        self.assertEqual(restored["state"], "completed", restored)
        self.assertTrue((Path(review["root"]) / "head/src/value.py").is_file())
        self.assertTrue(service.requests())

    @unittest.skipUnless(
        CONTAINER_CLI and CONTAINER_IMAGE,
        "requires a declared Docker CLI and immutable image",
    )
    def test_fixed_external_pr_runs_in_real_container_and_reuses_evidence(self):
        command = (
            "import os,pathlib; assert os.getenv('OPENAI_API_KEY') is None; "
            "assert not pathlib.Path('.harness/runtime').exists(); "
            "ns={}; exec(pathlib.Path('src/value.py').read_text(),ns); "
            "assert ns['save'](7)==7; print('fixed-pr-tests-passed')"
        )
        registry = self.root / "tests.json"
        registry.write_text(
            json.dumps(
                {
                    "version": 1,
                    "recipes": [
                        {
                            "name": "fixed-pr",
                            "argv": [sys.executable, "-c", command],
                            "authorization": "confirm",
                            "timeout": 30,
                        }
                    ],
                    "execution": {
                        "backend": "container",
                        "policy": {
                            "cli": CONTAINER_CLI,
                            "image": CONTAINER_IMAGE,
                            "argv": ["/usr/local/bin/python", "-c", command],
                            "native_argv": [sys.executable, "-c", command],
                        },
                    },
                }
            )
        )
        self.host.config = replace(
            self.host.config,
            settings=replace(self.host.config.settings, test_config=registry),
        )
        self.peer.issue(pr=True)
        service = self.start(required_tests=["fixed-pr"], prepare_seconds=60)
        did = self.discussion(service)
        review_id = eventually(lambda: service.discussion(did)["review_id"], 60)
        review = service.runtime.maintenance.communication.store.get(
            "review_snapshots", review_id
        )
        self.assertTrue(review["tests_passed"], review)
        receipt = review["tests"][0]
        self.assertEqual(receipt["execution_backend"], "container")
        self.assertTrue(receipt["isolation"]["os_isolation_verified"])
        self.assertTrue(receipt["isolation"]["cleanup_complete"])
        eventually(
            lambda: service.discussion(did)["state"] == "waiting_participant", 20
        )
        service.pause("all")
        operation = service.control_discussion(did, "prepare_review")
        communication = service.runtime.maintenance.communication
        result = communication.prepare_review(
            communication.store.get("discussions", did)
        )
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["id"], operation["id"])
        same = service.runtime.maintenance.communication.store.get(
            "review_snapshots", review_id
        )
        self.assertEqual(same["tests"][0]["run_id"], receipt["run_id"])

    def test_pending_test_cleanup_retains_owner_and_service_close(self):
        released = threading.Event()
        waiting = threading.Event()
        self.addCleanup(released.set)

        class PendingRunner:
            def _recipe(self, _name):
                pass

            def approve(self, _name):
                pass

            def run(self, _name):
                return {"status": "failed", "source_stable": True}

            def close(self):
                pass

            def wait_closed(self, _timeout):
                waiting.set()
                return released.wait(15)

        with patch(
            "homebrew_harness.execution.test_runner.load_test_runner",
            return_value=PendingRunner(),
        ):
            self.peer.issue(pr=True)
            service = self.start(required_tests=["fixture"])
            self.assertTrue(waiting.wait(10))
            communication = service.runtime.maintenance.communication
            self.assertTrue(communication.leases.held("communication-review"))
            service.stop()
            self.assertFalse(service.wait_closed(0.1))
            self.assertTrue(communication.leases.held("communication-review"))
            released.set()
            self.assertTrue(service.wait_closed(10))
            self.assertFalse(communication.leases.held("communication-review"))
