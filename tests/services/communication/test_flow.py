"""Community rounds and public effects over actual model/platform HTTP."""

import io
import tempfile
import time
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from homebrew_harness import Harness, HostConfig, ServiceSpec
from tests.runtime.test_interaction import eventually
from .peer import Peer


class CommunicationFlow(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.control = self.root / "control"
        self.control.mkdir(mode=0o700)
        self.project = self.root / "project"
        self.project.mkdir()
        self.peer = Peer()
        self.addCleanup(self.peer.close)
        for context in (redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)
        environment = patch.dict("os.environ", {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        env = self.root / "model.env"
        env.write_text(
            "OPENAI_API_KEY=synthetic-model\nOPENAI_MODEL=fixture\nOPENAI_BASE_URL="
            + self.peer.url
            + "/v1\nPROJECT_API="
            + self.peer.url
            + "\nREAD_TOKEN=synthetic\nWRITE_TOKEN=synthetic\nHARNESS_MODEL_TRANSPORT=http\n"
        )
        self.host = Harness(
            workdir=self.control,
            config=HostConfig.from_files(env_file=env, shutdown_seconds=3),
        )
        self.addCleanup(self.host.close)

    def start(self, auto=False, *, operations=None, provider="github", **overrides):
        source = {
            "name": "project",
            "provider": provider,
            "instance": "primary",
            "project": "example/project",
            "api_url_env": "PROJECT_API",
            "token_env": "READ_TOKEN",
            "poll_seconds": 0.1,
            "max_pages": 5,
        }
        communication = {
            "mode": "local",
            "projects": [{**source, "token_env": "WRITE_TOKEN"}],
            "poll_seconds": 0.1,
            "debounce_seconds": 0.1,
            "max_debounce_seconds": 0.2,
            "auto_intents": ["clarify"] if auto else [],
            **overrides,
        }
        spec = ServiceSpec(
            str(self.project),
            profile="repository_maintenance",
            sources=(source,),
            maintenance={
                "scopes": {
                    "code": {
                        "write_paths": ["src"],
                        "tools": ["read_file", "write_file"],
                        "allow_tests": [],
                    }
                },
                "report_seconds": 3600,
            },
            communication=communication,
            operations=operations or {},
        )
        service = self.host.services.start(spec)

        def stop_service():
            if not service.wait_closed(0):
                service.stop()
                self.assertTrue(service.wait_closed(8))

        self.addCleanup(stop_service)
        return service

    def discussion(self, service):
        identity = eventually(
            lambda: (
                service.discussions()["items"]
                and service.discussions()["items"][0]["id"]
            ),
            10,
        )
        return identity

    def test_multiple_issue_rounds_require_acceptance_and_do_not_echo(self):
        self.peer.issue()
        self.peer.proposals = [
            {
                "next_step": "clarify",
                "reason": "Missing offline requirements.",
                "alignment": "Keep offline support.",
                "questions": [
                    {"id": "offline", "text": "Must it work offline?", "sources": []}
                ],
                "communication": {
                    "kind": "post_comment",
                    "intent": "clarify",
                    "body": "Must it work offline?",
                },
            }
        ]
        service = self.start(auto=True)
        did = self.discussion(service)
        eventually(lambda: len(self.peer.writes) == 1, 15)
        calls = len(self.peer.models)
        time.sleep(0.4)
        self.assertEqual(len(self.peer.models), calls)
        self.assertEqual(
            self.host.services.live[service.id].runtime.child.run_store.runtime.items(
                "runs"
            ),
            {},
        )
        self.peer.proposals = [
            {
                "next_step": "clarify",
                "reason": "Need file compatibility.",
                "alignment": "Preserve formats.",
                "facts": [
                    {"id": "offline", "text": "Must work offline.", "sources": []}
                ],
                "questions": [
                    {"id": "compat", "text": "Keep old files readable?", "sources": []}
                ],
                "communication": {
                    "kind": "post_comment",
                    "intent": "clarify",
                    "body": "Keep old files readable?",
                },
            }
        ]
        self.peer.answer("Yes, offline is required.")
        eventually(lambda: len(self.peer.writes) == 2, 15)
        self.peer.proposals = [
            {
                "next_step": "develop",
                "reason": "The scope is clear.",
                "alignment": "Fits offline goals.",
                "draft": {
                    "text": "Add offline export.",
                    "scope": "code",
                    "acceptance": "Existing files remain readable.",
                },
            }
        ]
        self.peer.answer("Yes, preserve old files.")
        request = eventually(
            lambda: next(
                (r for r in service.requests() if r.kind == "discussion_requirements"),
                None,
            ),
            15,
        )
        self.assertFalse(service.work.list()["items"])
        service.decide(request, "approve", decision_id="accept")
        authority = eventually(
            lambda: next(
                (r for r in service.requests() if r.kind == "work_authority"), None
            ),
            5,
        )
        self.assertTrue(authority)
        self.assertEqual(len(service.work.list()["items"]), 1)
        self.assertEqual(service.discussion(did)["draft_revision"], 1)

    def test_unapproved_message_is_independent_of_changesets(self):
        self.peer.issue()
        self.peer.proposals = [
            {
                "next_step": "clarify",
                "reason": "Unclear.",
                "alignment": "Within scope.",
                "communication": {
                    "kind": "post_comment",
                    "intent": "clarify",
                    "body": "Which behavior do you need?",
                },
            }
        ]
        service = self.start()
        did = self.discussion(service)
        request = eventually(
            lambda: next(
                (r for r in service.requests() if r.kind == "communication_authority"),
                None,
            ),
            15,
        )
        self.assertFalse(self.peer.writes)
        batch = service.batch([request], request_id="messages")
        service.decide_batch(
            batch["id"],
            [{"id": request.ref.id, "decision": "approve"}],
            request_id="approve",
        )
        eventually(lambda: len(self.peer.writes) == 1, 10)
        self.assertFalse(service.work.list()["items"])
        eventually(lambda: service.discussion(did)["state"] == "waiting_participant", 5)

    def test_lost_comment_receipt_is_reconciled_without_resending(self):
        self.peer.issue()
        self.peer.drop = "comment"
        self.peer.proposals = [
            {
                "next_step": "clarify",
                "reason": "Unclear.",
                "alignment": "Within scope.",
                "communication": {
                    "kind": "post_comment",
                    "intent": "clarify",
                    "body": "Which behavior?",
                },
            }
        ]
        service = self.start(auto=True)
        self.discussion(service)
        action = eventually(
            lambda: next(
                (
                    a
                    for a in service.communication_actions()["items"]
                    if a["state"] == "unknown"
                ),
                None,
            ),
            15,
        )
        self.peer.drop = None
        result = service.inspect_communication(action["id"], request_id="inspect")
        self.assertEqual(result["state"], "confirmed")
        self.assertEqual(len(self.peer.writes), 1)

    def test_pr_snapshot_review_then_author_explanation_reuses_snapshot(self):
        self.peer.issue(pr=True)
        self.peer.proposals = [
            {
                "next_step": "wait",
                "reason": "Review needs an explanation.",
                "alignment": "Fits the project.",
                "findings": [
                    {
                        "id": "behavior",
                        "text": "Explain the behavior change.",
                        "sources": [],
                        "status": "open",
                    }
                ],
                "communication": {
                    "kind": "submit_review",
                    "intent": "review_feedback",
                    "event": "COMMENT",
                    "body": "Explain this behavior change.",
                    "comments": [
                        {
                            "path": "src/value.py",
                            "line": 2,
                            "side": "RIGHT",
                            "body": "Please explain.",
                        }
                    ],
                },
            }
        ]
        service = self.start()
        did = self.discussion(service)
        request = eventually(
            lambda: next(
                (r for r in service.requests() if r.kind == "communication_authority"),
                None,
            ),
            20,
        )
        review = service.discussion(did)["review_id"]
        self.assertTrue(review)
        service.decide(request, "approve", decision_id="review")
        eventually(lambda: len(self.peer.reviews) == 1, 10)
        self.peer.proposals = [
            {
                "next_step": "wait",
                "reason": "Explanation is sufficient.",
                "alignment": "Fits the project.",
                "findings": [
                    {
                        "id": "behavior",
                        "text": "The explanation is supported.",
                        "sources": [],
                        "status": "withdrawn",
                    }
                ],
            }
        ]
        self.peer.answer("It preserves the documented behavior.")
        eventually(
            lambda: (
                service.discussion(did)["projection"]["findings"][0]["status"]
                == "withdrawn"
            ),
            15,
        )
        self.assertEqual(service.discussion(did)["review_id"], review)
        self.peer.head = "4" * 40
        self.peer.files["src/value.py"] = "def save(value):\n    return str(value)\n"
        self.peer.proposals = [
            {
                "next_step": "wait",
                "reason": "New version needs review.",
                "alignment": "Fits the project.",
            }
        ]
        eventually(lambda: service.discussion(did)["review_id"] != review, 15)

    def test_thread_reply_is_scoped_and_does_not_claim_a_merge(self):
        self.peer.issue(pr=True)
        self.peer.inline = [
            {
                "id": 9,
                "body": "Please explain this branch.",
                "user": {"id": 11},
                "updated_at": "2026-11-11T00:00:02Z",
                "pull_request_url": self.peer.url + "/repos/example/project/pulls/1",
                "commit_id": self.peer.head,
                "path": "src/value.py",
                "line": 2,
                "side": "RIGHT",
            }
        ]
        self.peer.proposals = [
            {
                "next_step": "wait",
                "reason": "Explain the known thread.",
                "alignment": "The change is within scope.",
                "communication": {
                    "kind": "reply_thread",
                    "intent": "followup",
                    "thread_id": "9",
                    "body": "This branch preserves the documented result.",
                },
            }
        ]
        service = self.start()
        did = self.discussion(service)
        request = eventually(
            lambda: next(
                (r for r in service.requests() if r.kind == "communication_authority"),
                None,
            ),
            15,
        )
        service.decide(request, "approve", decision_id="reply")
        eventually(lambda: any(kind == "reply" for kind, _ in self.peer.writes), 10)
        self.assertEqual(self.peer.inline[-1]["in_reply_to_id"], 9)
        eventually(lambda: service.discussion(did)["state"] == "waiting_participant", 5)

    def test_new_reply_invalidates_old_approval_and_preserves_round_history(self):
        self.peer.issue()
        self.peer.proposals = [
            {
                "next_step": "clarify",
                "reason": "Need details.",
                "alignment": "Within scope.",
                "communication": {
                    "kind": "post_comment",
                    "intent": "clarify",
                    "body": "What compatibility do you need?",
                },
            }
        ]
        service = self.start()
        did = self.discussion(service)
        old = eventually(
            lambda: next(
                (r for r in service.requests() if r.kind == "communication_authority"),
                None,
            ),
            15,
        )
        self.peer.proposals = [
            {
                "next_step": "wait",
                "reason": "The author already answered.",
                "alignment": "Preserves compatibility.",
            }
        ]
        self.peer.answer("Keep old files readable.")
        eventually(lambda: service.discussion(did)["input_revision"] > 1, 10)
        with self.assertRaises(Exception):
            service.decide(old, "approve", decision_id="old")
        self.assertFalse(self.peer.writes)
        actions = service.communication_actions()["items"]
        self.assertEqual(actions[0]["state"], "superseded")

    def test_restart_keeps_unapproved_message_and_can_publish_once(self):
        self.peer.issue()
        self.peer.proposals = [
            {
                "next_step": "clarify",
                "reason": "Need details.",
                "alignment": "Within scope.",
                "communication": {
                    "kind": "post_comment",
                    "intent": "clarify",
                    "body": "What compatibility?",
                },
            }
        ]
        service = self.start()
        did = self.discussion(service)
        request = eventually(
            lambda: next(
                (r for r in service.requests() if r.kind == "communication_authority"),
                None,
            ),
            15,
        )
        service.stop()
        self.assertTrue(service.wait_closed(8))
        restarted = self.host.services.start(identity=service.id)

        def stop_restarted():
            if not restarted.wait_closed(0):
                restarted.stop()
                self.assertTrue(restarted.wait_closed(8))

        self.addCleanup(stop_restarted)
        current = next(r for r in restarted.requests() if r.ref.id == request.ref.id)
        self.assertEqual(current.ref, request.ref)
        restarted.decide(current, "approve", decision_id="restart-approve")
        eventually(lambda: len(self.peer.writes) == 1, 10)
        eventually(
            lambda: restarted.discussion(did)["state"] == "waiting_participant", 5
        )

    def test_review_preparation_returns_durable_number_and_fixed_base_files(self):
        self.peer.issue(pr=True)
        service = self.start()
        did = self.discussion(service)
        eventually(lambda: service.discussion(did).get("review_id"), 15)
        operation = service.control_discussion(
            did, "prepare_review", request_id="prepare"
        )
        self.assertEqual(operation["state"], "queued")
        row = eventually(
            lambda: (
                snapshot
                if (
                    snapshot := service.runtime.maintenance.communication.store.get(
                        "review_operations", operation["id"]
                    )
                )["state"]
                == "completed"
                else None
            ),
            10,
        )
        snapshot = service.runtime.maintenance.communication.store.get(
            "review_snapshots", row["snapshot_id"]
        )
        self.assertEqual(
            (Path(snapshot["root"]) / "base/src/value.py").read_text(),
            self.peer.base_files["src/value.py"],
        )

    def test_head_changes_while_review_waits_for_approval(self):
        self.peer.issue(pr=True)
        self.peer.proposals = [
            {
                "next_step": "wait",
                "reason": "Review the current version.",
                "alignment": "Within scope.",
                "communication": {
                    "kind": "submit_review",
                    "intent": "review_feedback",
                    "event": "COMMENT",
                    "body": "Please clarify the result.",
                },
            }
        ]
        service = self.start()
        did = self.discussion(service)
        request = eventually(
            lambda: next(
                (r for r in service.requests() if r.kind == "communication_authority"),
                None,
            ),
            15,
        )
        old = service.discussion(did)["review_id"]
        self.peer.proposals = [
            {
                "next_step": "wait",
                "reason": "Need to review the new version.",
                "alignment": "Within scope.",
            }
        ]
        self.peer.head = "5" * 40
        eventually(lambda: service.discussion(did)["review_id"] not in {None, old}, 10)
        with self.assertRaises(Exception):
            service.decide(request, "approve", decision_id="stale-head")
        self.assertFalse(self.peer.reviews)
